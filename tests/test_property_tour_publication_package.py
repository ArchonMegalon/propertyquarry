from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import materialize_property_tour_publication_package as publisher


SLUG = "generated-layout-tour"
USER_INSTRUCTION_SHA256 = publisher.AUTHORIZED_USER_INSTRUCTION_SHA256


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@dataclass
class PublicationFixture:
    root: Path
    repo: Path
    bundle: Path
    final_receipt: Path
    browser_receipt: Path
    artifact_commit: str
    monkeypatch: pytest.MonkeyPatch
    final_digest: str = ""
    browser_digest: str = ""

    def refresh_receipts(self) -> None:
        viewer = (self.bundle / "generated-reconstruction/viewer.html").read_bytes()
        reconstruction = (
            self.bundle / "generated-reconstruction/reconstruction.json"
        ).read_bytes()
        tour = (self.bundle / "tour.json").read_bytes()
        floorplan = (
            self.bundle / "generated-reconstruction/source-floorplan.png"
        ).read_bytes()
        browser = {
            "schema": publisher.BROWSER_RECEIPT_SCHEMA,
            "slug": SLUG,
            "generated_at": "2026-07-14T02:24:49Z",
            "viewer_sha256": _sha256(viewer),
            "reconstruction_sha256": _sha256(reconstruction),
            "url": f"http://127.0.0.1:18107/{SLUG}/viewer.html",
            "status": "pass",
            "failures": [],
            "surfaces": {
                name: _browser_surface(name)
                for name in ("desktop", "mobile", "reduced-motion", "webgl-fallback")
            },
        }
        self.browser_receipt.write_bytes(_json_bytes(browser))
        self.browser_digest = _sha256(self.browser_receipt.read_bytes())
        final = {
            "schema": publisher.REVIEW_RECEIPT_SCHEMA,
            "generated_at": "2026-07-14T02:28:41Z",
            "slug": SLUG,
            "status": "polished_review_candidate_pass_guarded_not_published",
            "source": {
                "repo": str(self.repo.resolve()),
                "branch": "test",
                "commit": self.artifact_commit,
                "worktree_clean": True,
            },
            "review_bundle": {
                "root": str(self.bundle.resolve()),
                "viewer": str(
                    self.bundle / "generated-reconstruction/viewer.html"
                ),
                "viewer_sha256": _sha256(viewer),
                "reconstruction": str(
                    self.bundle / "generated-reconstruction/reconstruction.json"
                ),
                "reconstruction_sha256": _sha256(reconstruction),
                "tour_manifest_sha256": _sha256(tour),
                "floorplan_sha256": _sha256(floorplan),
                "runtime_publish": {"status": "skipped_not_requested", "slug": SLUG},
                "runtime_publish_required": False,
                "runtime_publish_ok": True,
                "verified_provider_capture": False,
                "satisfies_verified_tour_gate": False,
            },
            "visual_verification": {
                "route_receipt": str(self.root / "route.json"),
                "route_receipt_sha256": "1" * 64,
                "route_status": "pass",
                "route_failures": [],
                "route_stop_count": 9,
                "contact_sheet": str(self.root / "sheet.png"),
                "contact_sheet_sha256": "2" * 64,
                "browser_receipt": str(self.browser_receipt),
                "browser_receipt_sha256": self.browser_digest,
                "browser_status": "pass",
                "browser_failures": [],
                "surfaces": [
                    "desktop",
                    "mobile",
                    "reduced-motion",
                    "webgl-fallback",
                ],
            },
            "verification": {
                "property_generated_reconstruction": {
                    "result": "pass",
                    "tests_passed": 35,
                },
                "property_tour_control_and_importers": {
                    "result": "pass",
                    "tests_passed": 77,
                },
                "python_compile": {"result": "pass"},
                "diff_check": {"result": "pass"},
                "independent_camera_geometry_accessibility_review": {
                    "result": "approved"
                },
                "independent_runtime_publish_safety_review": {
                    "result": "approved"
                },
            },
            "live_guard": {
                "runtime_mutation_detected": False,
                "all_observed_product_routes_guarded_404": True,
            },
            "release_blockers": [
                "No target-bound authorized provider capture or export is present.",
                "verified_provider_capture is false.",
            ],
        }
        self.final_receipt.write_bytes(_json_bytes(final))
        self.final_digest = _sha256(self.final_receipt.read_bytes())
        self.monkeypatch.setattr(
            publisher,
            "AUTHORIZED_FINAL_REVIEW_RECEIPT_SHA256",
            self.final_digest,
        )
        self.monkeypatch.setattr(
            publisher,
            "AUTHORIZED_BROWSER_REVIEW_RECEIPT_SHA256",
            self.browser_digest,
        )

    def kwargs(self, output_root: Path) -> dict[str, object]:
        return {
            "source_repo": self.repo,
            "artifact_commit": self.artifact_commit,
            "review_bundle": self.bundle,
            "final_review_receipt": self.final_receipt,
            "expected_final_review_receipt_sha256": self.final_digest,
            "browser_review_receipt": self.browser_receipt,
            "expected_browser_review_receipt_sha256": self.browser_digest,
            "slug": SLUG,
            "user_instruction_sha256": USER_INSTRUCTION_SHA256,
            "allowed_public_origins": [
                "https://propertyquarry.com",
                "https://myexternalbrain.com",
            ],
            "output_root": output_root,
        }


def _browser_surface(name: str) -> dict[str, object]:
    fallback = name == "webgl-fallback"
    metrics: dict[str, object] = {
        "ready": not fallback,
        "routeStopCount": 9,
    }
    if not fallback:
        metrics["photoPanelCount"] = 0
    return {
        "http_status": 200,
        "page_errors": [],
        "console_errors": [],
        "undersizedTargets": [],
        "floorplanTargetOverlaps": [],
        "clippedVisibleHotspotLabels": [],
        "horizontalOverflowPx": 0,
        "alertVisible": fallback,
        "metrics": metrics,
    }


def _write_raw_bundle(bundle: Path) -> None:
    files = {
        "diorama-preview.png": b"diorama",
        "floorplan-apartment-crop.png": b"source-floorplan-original",
        "telegram-preview.png": b"telegram",
        "generated-reconstruction/model.glb": b"glb",
        "generated-reconstruction/model.mtl": b"mtl",
        "generated-reconstruction/model.obj": b"obj",
        "generated-reconstruction/source-floorplan.png": b"public-floorplan-png",
        "generated-reconstruction/viewer.html": (
            b"<!doctype html><title>Layout preview</title><canvas></canvas>"
        ),
        "generated-reconstruction/vendor/three.module.js": (
            b"export const Scene = class {};"
        ),
        "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js": (
            b"export class OrbitControls {}"
        ),
    }
    for relpath, content in files.items():
        target = bundle / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    route_labels = [
        "Entry",
        "Kitchen",
        "Room 1",
        "Hall",
        "Room 2",
        "Bath",
        "WC",
        "Storage",
        "Terrace",
    ]
    reconstruction = {
        "provider": publisher.GENERATED_PROVIDER,
        "slug": SLUG,
        "generated_at": "2026-07-14T02:20:58Z",
        "method": "floorplan_directional_wall_segments",
        "style_label": "Architectural dollhouse",
        "disclosure": "Planning preview built from the floor plan; not captured.",
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "room_dimensions_m": {"width": 10.0, "depth": 11.0, "height": 2.75},
        "floorplan": {
            "source_path": "/home/operator/private/floorplan-apartment-crop.png",
            "relpath": "source-floorplan.png",
            "sha256": _sha256(files["generated-reconstruction/source-floorplan.png"]),
            "size_bytes": len(files["generated-reconstruction/source-floorplan.png"]),
            "width": 1250,
            "height": 1400,
            "mode": "RGB",
        },
        "photos": [],
        "photo_reference_panels": [],
        "route_labels": route_labels,
        "walkthrough_route_labels": route_labels,
        "walkable_scene": {
            "kind": "generated_reconstruction_layout",
            "route": [
                {
                    "label": label,
                    "sequence": index,
                    "focus": {"x": index, "y": 1.3, "z": index},
                    "camera": {"x": index + 1, "y": 1.6, "z": index + 1},
                }
                for index, label in enumerate(route_labels, start=1)
            ],
        },
        "geometry": {"wall_rectangles": []},
        "viewer": {
            "relpath": "viewer.html",
            "version": publisher.VIEWER_VERSION,
            "photo_reference_panel_count": 0,
            "sha256": _sha256(files["generated-reconstruction/viewer.html"]),
            "vendor": {
                "name": "three",
                "version": "0.167.1",
                "license": "MIT",
                "emitted": {
                    "three_module": {
                        "relpath": "vendor/three.module.js",
                        "sha256": _sha256(
                            files["generated-reconstruction/vendor/three.module.js"]
                        ),
                    },
                    "orbit_controls": {
                        "relpath": "vendor/examples/jsm/controls/OrbitControls.js",
                        "sha256": _sha256(
                            files[
                                "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js"
                            ]
                        ),
                    },
                },
            },
        },
        "model": {"obj_relpath": "model.obj"},
        "bundle_preview_assets": {"diorama": {"status": "generated"}},
        "runtime_publish": {"status": "skipped_not_requested"},
        "runtime_publish_required": False,
        "runtime_publish_ok": True,
        "walkthrough": {"status": "skipped"},
    }
    reconstruction_path = bundle / "generated-reconstruction/reconstruction.json"
    reconstruction_path.write_bytes(_json_bytes(reconstruction))
    tour = {
        "slug": SLUG,
        "principal_id": "private identity intentionally removed by publication",
        "recipient_email": "private@example.test",
        "generated_reconstruction": {
            "provider": publisher.GENERATED_PROVIDER,
            "viewer_version": publisher.VIEWER_VERSION,
            "viewer_relpath": "generated-reconstruction/viewer.html",
            "manifest_relpath": "generated-reconstruction/reconstruction.json",
            "verified_provider_capture": False,
            "satisfies_verified_tour_gate": False,
            "photo_reference_panel_count": 0,
            "route_labels": route_labels,
        },
    }
    (bundle / "tour.json").write_bytes(_json_bytes(tour))


@pytest.fixture
def publication_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PublicationFixture:
    repo = tmp_path / "property"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Property Test")
    _git(repo, "config", "user.email", "property-test@example.invalid")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "fixture")
    artifact_commit = _git(repo, "rev-parse", "HEAD")
    bundle = tmp_path / "review" / SLUG
    bundle.mkdir(parents=True)
    _write_raw_bundle(bundle)
    fixture = PublicationFixture(
        root=tmp_path,
        repo=repo,
        bundle=bundle,
        final_receipt=tmp_path / "flagship-final.json",
        browser_receipt=tmp_path / "browser.json",
        artifact_commit=artifact_commit,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(publisher, "AUTHORIZED_SLUG", SLUG)
    monkeypatch.setattr(
        publisher,
        "AUTHORIZED_ARTIFACT_COMMIT",
        artifact_commit,
    )
    fixture.refresh_receipts()
    return fixture


def _tree_bytes(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for directory, _directory_names, file_names in os.walk(root):
        for name in file_names:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def test_materializer_emits_deterministic_property_owned_package(
    publication_fixture: PublicationFixture,
) -> None:
    first_root = publication_fixture.root / "package-one"
    second_root = publication_fixture.root / "package-two"

    first = publisher.materialize_publication_package(
        **publication_fixture.kwargs(first_root)
    )
    second = publisher.materialize_publication_package(
        **publication_fixture.kwargs(second_root)
    )

    assert first["status"] == "pass"
    assert first["owner"] == "PropertyQuarry"
    assert first["public_file_count"] == 6
    assert first["asset_binding_count"] == 5
    assert first["public_activation_authority"] is True
    assert first["verified_provider_capture"] is False
    assert _tree_bytes(first_root) == _tree_bytes(second_root)

    bundle = first_root / "public_property_tours" / SLUG
    authority_path = first_root / "publication-authority" / f"{SLUG}.json"
    assert stat.S_IMODE(authority_path.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o644
        for path in bundle.rglob("*")
        if path.is_file()
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o755
        for path in [bundle, *(path for path in bundle.rglob("*") if path.is_dir())]
    )

    tour = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    proof = json.loads(
        (bundle / "generated-reconstruction/reconstruction.json").read_text(
            encoding="utf-8"
        )
    )
    assert tour["schema"] == publisher.PUBLIC_TOUR_PACKAGE_SCHEMA
    assert set(tour) == {
        "schema",
        "slug",
        "display_title",
        "scene_strategy",
        "creation_mode",
        "source_commit",
        "synthetic",
        "generated_reconstruction",
        "generated_viewer_release",
        "route_labels",
    }
    assert "principal_id" not in tour
    assert "recipient_email" not in tour
    release = tour["generated_viewer_release"]
    assert release["publication_authority_verified"] is True
    assert release["public_activation_authority"] is True
    assert release["synthetic"] is True
    assert release["capture_mode"] is False
    assert release["verified_provider_capture"] is False
    assert release["satisfies_verified_tour_gate"] is False
    assert release["publication_authority_receipt_sha256"] == _sha256(
        authority_path.read_bytes()
    )
    assert len(release["asset_bindings"]) == 5
    for binding in release["asset_bindings"]:
        content = (bundle / binding["path"]).read_bytes()
        assert binding["sha256"] == _sha256(content)
        assert binding["size_bytes"] == len(content)
    pre_authority = copy.deepcopy(tour)
    pre_authority["generated_viewer_release"][
        "publication_authority_receipt_sha256"
    ] = None
    assert authority["package"]["pre_authority_manifest_canonical_sha256"] == _sha256(
        publisher._canonical_json_bytes(pre_authority)
    )
    assert authority["user_instruction_sha256"] == USER_INSTRUCTION_SHA256
    assert authority["repository"] == "ArchonMegalon/propertyquarry"
    assert authority["allowed_public_origins"] == [
        "https://myexternalbrain.com",
        "https://propertyquarry.com",
    ]
    assert proof["schema"] == publisher.PUBLIC_RECONSTRUCTION_SCHEMA
    assert proof["floorplan"]["source_path"] == (
        f"property://ArchonMegalon/propertyquarry/{publication_fixture.artifact_commit}/"
        "floorplan-apartment-crop.png"
    )
    assert proof["synthetic"] is True
    assert proof["capture_mode"] is False
    assert proof["verified_provider_capture"] is False
    assert tour["generated_reconstruction"]["capture_mode"] is False
    assert authority["classification"]["capture_mode"] is False
    assert "model" not in proof
    assert b"/home/" not in (bundle / "tour.json").read_bytes()
    assert b"@" not in (bundle / "tour.json").read_bytes()


def test_materializer_rejects_receipt_byte_drift(
    publication_fixture: PublicationFixture,
) -> None:
    publication_fixture.browser_receipt.write_bytes(
        publication_fixture.browser_receipt.read_bytes() + b" "
    )

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "browser_receipt_bytes_drift"


def test_materializer_rejects_raw_bundle_extra(
    publication_fixture: PublicationFixture,
) -> None:
    (publication_fixture.bundle / "unreviewed.txt").write_text(
        "extra", encoding="utf-8"
    )

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "review_bundle_inventory_invalid"


def test_materializer_rejects_symlinked_review_asset(
    publication_fixture: PublicationFixture,
) -> None:
    viewer = publication_fixture.bundle / "generated-reconstruction/viewer.html"
    viewer.unlink()
    viewer.symlink_to(publication_fixture.bundle / "tour.json")

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "source_symlink_forbidden"


def test_materializer_rejects_private_field_added_to_proof(
    publication_fixture: PublicationFixture,
) -> None:
    path = publication_fixture.bundle / "generated-reconstruction/reconstruction.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["recipient_email"] = "operator@example.test"
    path.write_bytes(_json_bytes(payload))
    publication_fixture.refresh_receipts()

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "private_reconstruction_field_present"


def test_materializer_rejects_dirty_source_repository(
    publication_fixture: PublicationFixture,
) -> None:
    (publication_fixture.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "source_worktree_dirty"


def test_materializer_detects_path_swap_during_secure_read(
    publication_fixture: PublicationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = (
        publication_fixture.bundle / "generated-reconstruction/viewer.html"
    ).resolve()
    replacement = publication_fixture.root / "replacement.html"
    replacement.write_text("replacement", encoding="utf-8")
    swapped = False

    def swap_after_read(path: Path) -> None:
        nonlocal swapped
        if path == viewer and not swapped:
            swapped = True
            os.replace(replacement, viewer)

    monkeypatch.setattr(publisher, "_after_secure_read_hook", swap_after_read)

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert swapped is True
    assert exc_info.value.code == "source_toctou_detected"


@pytest.mark.parametrize(
    "origin",
    [
        "http://propertyquarry.com",
        "https://user:secret@propertyquarry.com",
        "https://propertyquarry.com/private",
        "https://propertyquarry.com:8443",
    ],
)
def test_materializer_rejects_non_origin_public_authority(
    publication_fixture: PublicationFixture,
    origin: str,
) -> None:
    kwargs = publication_fixture.kwargs(publication_fixture.root / "package")
    kwargs["allowed_public_origins"] = [origin, "https://myexternalbrain.com"]

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(**kwargs)

    assert exc_info.value.code in {"allowed_origin_invalid", "allowed_origin_set_invalid"}


def test_materializer_rejects_valid_but_unauthorized_https_origin(
    publication_fixture: PublicationFixture,
) -> None:
    kwargs = publication_fixture.kwargs(publication_fixture.root / "package")
    kwargs["allowed_public_origins"] = [
        "https://propertyquarry.com",
        "https://attacker.example",
    ]

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(**kwargs)

    assert exc_info.value.code == "allowed_origin_set_invalid"


def test_materializer_rejects_unpinned_user_instruction_hash(
    publication_fixture: PublicationFixture,
) -> None:
    kwargs = publication_fixture.kwargs(publication_fixture.root / "package")
    kwargs["user_instruction_sha256"] = "5" * 64

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(**kwargs)

    assert exc_info.value.code == "user_instruction_hash_unauthorized"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("slug", "another-reviewed-tour", "slug_unauthorized"),
        ("artifact_commit", "a" * 40, "source_commit_unauthorized"),
        (
            "expected_final_review_receipt_sha256",
            "a" * 64,
            "final_receipt_hash_unauthorized",
        ),
        (
            "expected_browser_review_receipt_sha256",
            "b" * 64,
            "browser_receipt_hash_unauthorized",
        ),
    ],
)
def test_materializer_rejects_publication_scope_outside_pinned_wave(
    publication_fixture: PublicationFixture,
    field: str,
    value: str,
    code: str,
) -> None:
    kwargs = publication_fixture.kwargs(publication_fixture.root / "package")
    kwargs[field] = value

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(**kwargs)

    assert exc_info.value.code == code


def test_materializer_refuses_existing_output_even_when_it_contains_only_an_extra(
    publication_fixture: PublicationFixture,
) -> None:
    output = publication_fixture.root / "package"
    output.mkdir()
    (output / "rogue").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(output)
        )

    assert exc_info.value.code == "output_root_exists"
    assert (output / "rogue").read_text(encoding="utf-8") == "do not overwrite"


def test_materializer_exclusive_install_loses_destination_race_without_overwrite(
    publication_fixture: PublicationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = publication_fixture.root / "package"
    original = publisher._rename_noreplace

    def race(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        os.mkdir(destination_name, mode=0o755, dir_fd=destination_parent_fd)
        marker_fd = os.open(
            f"{destination_name}/racer-owned",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=destination_parent_fd,
        )
        os.write(marker_fd, b"preserve")
        os.close(marker_fd)
        original(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(publisher, "_rename_noreplace", race)

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(output)
        )

    assert exc_info.value.code == "output_root_exists"
    assert (output / "racer-owned").read_bytes() == b"preserve"
    assert not (output / "public_property_tours").exists()


def test_materializer_rejects_symlinked_review_bundle_ancestor(
    publication_fixture: PublicationFixture,
) -> None:
    alias = publication_fixture.root / "review-alias"
    alias.symlink_to(publication_fixture.bundle.parent, target_is_directory=True)
    kwargs = publication_fixture.kwargs(publication_fixture.root / "package")
    kwargs["review_bundle"] = alias / SLUG

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(**kwargs)

    assert exc_info.value.code in {
        "source_directory_open_failed",
        "source_symlink_forbidden",
    }


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_materializer_fails_closed_without_kernel_no_follow_traversal_flags(
    publication_fixture: PublicationFixture,
    monkeypatch: pytest.MonkeyPatch,
    missing_flag: str,
) -> None:
    monkeypatch.delattr(os, missing_flag)

    with pytest.raises(publisher.PublicationPackageError) as exc_info:
        publisher.materialize_publication_package(
            **publication_fixture.kwargs(publication_fixture.root / "package")
        )

    assert exc_info.value.code == "nofollow_traversal_unavailable"
