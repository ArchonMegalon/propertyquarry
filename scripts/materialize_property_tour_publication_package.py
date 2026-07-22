#!/usr/bin/env python3
"""Materialize a PropertyQuarry-owned generated-viewer publication package.

The public bundle is deliberately narrow.  Review receipts and publication
authority stay outside the served tree; EA may validate and transport the
result, but it does not mint or reinterpret this authority.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


PUBLICATION_AUTHORITY_SCHEMA = (
    "propertyquarry.generated-viewer-publication-authority.v1"
)
PUBLIC_TOUR_PACKAGE_SCHEMA = "propertyquarry.public-tour-generated-viewer-package.v1"
PUBLIC_RECONSTRUCTION_SCHEMA = (
    "propertyquarry.generated-reconstruction-publication.v1"
)
EA_GENERATED_VIEWER_RELEASE_CONTRACT = "ea.public-tour-generated-viewer-release.v1"
REVIEW_RECEIPT_SCHEMA = "propertyquarry.flagship_3d_review_receipt.v1"
BROWSER_RECEIPT_SCHEMA = "propertyquarry.exact_viewer_browser_audit.v3"
GENERATED_PROVIDER = "propertyquarry_generated_reconstruction"
VIEWER_VERSION = "propertyquarry_3d_tour_viewer_v3"
DISCLOSURE = (
    "Generated interactive reconstruction from the supplied floor plan. "
    "It is a layout aid, not a captured or provider-verified 3D scan."
)
PROPERTY_REPOSITORY = "ArchonMegalon/propertyquarry"
AUTHORIZED_SLUG = "360-tour-balkon-wohnung-in-neustift-layout-first-0146e6f9c6"
AUTHORIZED_ARTIFACT_COMMIT = "dd81d16421339d1ac4ca9f01d65f5ebcf607258f"
AUTHORIZED_FINAL_REVIEW_RECEIPT_SHA256 = (
    "08b79e6b69cdb6559339919bd9c9f414aa11cf747848e6a98565e3b59cef0c8d"
)
AUTHORIZED_BROWSER_REVIEW_RECEIPT_SHA256 = (
    "866bc0c59952d1000a34d0685d31b539cde96beea3ab6598604f371e47c894c3"
)
AUTHORIZED_PUBLIC_ORIGINS = frozenset(
    {"https://propertyquarry.com", "https://myexternalbrain.com"}
)
# Set to the exact operator instruction digest for this authorized publication wave.
# The CLI must match it; downstream consumers must pin the same value.
AUTHORIZED_USER_INSTRUCTION_SHA256 = (
    "4763872ed9080c1aae6fa6c16b923ed79ad8e776068a40fa960520d8e646e265"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_EMAIL_RE = re.compile(rb"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_RAW_ALLOWED_FILES = frozenset(
    {
        "tour.json",
        "diorama-preview.png",
        "floorplan-apartment-crop.png",
        "telegram-preview.png",
        "generated-reconstruction/model.glb",
        "generated-reconstruction/model.mtl",
        "generated-reconstruction/model.obj",
        "generated-reconstruction/reconstruction.json",
        "generated-reconstruction/source-floorplan.png",
        "generated-reconstruction/viewer.html",
        "generated-reconstruction/vendor/three.module.js",
        "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
    }
)

_ASSET_SPECS = (
    (
        "generated-reconstruction/viewer.html",
        "text/html",
        "viewer_document",
    ),
    (
        "generated-reconstruction/reconstruction.json",
        "application/json",
        "reconstruction_manifest",
    ),
    (
        "generated-reconstruction/source-floorplan.png",
        "image/png",
        "floorplan_texture",
    ),
    (
        "generated-reconstruction/vendor/three.module.js",
        "text/javascript",
        "viewer_module",
    ),
    (
        "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
        "text/javascript",
        "viewer_module",
    ),
)

_PUBLIC_FILE_RELPATHS = frozenset({"tour.json", *(row[0] for row in _ASSET_SPECS)})
_TEXT_PUBLIC_ASSETS = frozenset(
    {
        "generated-reconstruction/viewer.html",
        "generated-reconstruction/reconstruction.json",
        "generated-reconstruction/vendor/three.module.js",
        "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
    }
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "address",
        "address_lines",
        "crezlo_public_url",
        "editor_url",
        "exact_address",
        "external_id",
        "hosted_url",
        "listing_url",
        "map_lat",
        "map_lng",
        "principal_id",
        "property_url",
        "public_url",
        "recipient_email",
        "source_ref",
        "source_url",
        "source_virtual_tour_url",
    }
)
_RECONSTRUCTION_INPUT_KEYS = frozenset(
    {
        "bundle_preview_assets",
        "disclosure",
        "floorplan",
        "generated_at",
        "geometry",
        "method",
        "model",
        "photo_reference_panels",
        "photos",
        "provider",
        "room_dimensions_m",
        "route_labels",
        "runtime_publish",
        "runtime_publish_ok",
        "runtime_publish_required",
        "satisfies_verified_tour_gate",
        "slug",
        "style_label",
        "verified_provider_capture",
        "viewer",
        "walkable_scene",
        "walkthrough",
        "walkthrough_route_labels",
    }
)
_GEOMETRY_KEYS = frozenset(
    {
        "content_bbox_px",
        "content_size_px",
        "extraction_method",
        "floor_texture_crop",
        "mask_size_cells",
        "wall_rect_count",
        "wall_rectangles",
    }
)
_WALKABLE_SCENE_KEYS = frozenset(
    {
        "bounds",
        "kind",
        "rooms",
        "route",
        "route_anchor_method",
        "route_label_binding",
    }
)
_ROOM_DIMENSION_KEYS = frozenset({"width", "depth", "height"})
_FLOORPLAN_KEYS = frozenset(
    {"source_path", "relpath", "sha256", "size_bytes", "width", "height", "mode"}
)
_VIEWER_KEYS = frozenset(
    {"relpath", "version", "photo_reference_panel_count", "vendor", "sha256"}
)
_REQUIRED_BROWSER_SURFACES = frozenset(
    {"desktop", "mobile", "reduced-motion", "webgl-fallback"}
)


class PublicationPackageError(RuntimeError):
    """A fail-closed, secret-safe publication package error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SecureFile:
    path: Path
    content: bytes
    digest: str
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int


def _fail(code: str) -> None:
    raise PublicationPackageError(code)


def _require(condition: object, code: str) -> None:
    if not condition:
        _fail(code)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _json_file_bytes(value: object) -> bytes:
    return _canonical_json_bytes(value) + b"\n"


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("json_duplicate_key")
        result[key] = value
    return result


def _parse_json_object(content: bytes, *, code: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            content.decode("utf-8"), object_pairs_hook=_reject_duplicate_object
        )
    except PublicationPackageError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    _require(isinstance(decoded, dict), code)
    return dict(decoded)


def _path_stat_fingerprint(path_stat: os.stat_result) -> tuple[int, ...]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        stat.S_IMODE(path_stat.st_mode),
        path_stat.st_mtime_ns,
    )


def _identity(path_stat: os.stat_result) -> tuple[int, int]:
    return path_stat.st_dev, path_stat.st_ino


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _directory_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    _require(
        isinstance(nofollow, int)
        and nofollow > 0
        and isinstance(directory, int)
        and directory > 0,
        "nofollow_traversal_unavailable",
    )
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | directory
        | nofollow
    )


def _nofollow_open_flag() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    _require(
        isinstance(nofollow, int) and nofollow > 0,
        "nofollow_traversal_unavailable",
    )
    return nofollow


def _open_absolute_directory_no_follow(path: Path) -> int:
    absolute = _absolute_path(path)
    _require(absolute.is_absolute(), "source_path_invalid")
    try:
        descriptor = os.open("/", _directory_open_flags())
    except OSError:
        _fail("source_directory_open_failed")
    try:
        for part in absolute.parts[1:]:
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(
                    part,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
                entry_stat = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                child_stat = os.fstat(child_descriptor)
            except OSError as exc:
                if child_descriptor is not None:
                    os.close(child_descriptor)
                if exc.errno == errno.ELOOP:
                    _fail("source_symlink_forbidden")
                _fail("source_directory_open_failed")
            if not stat.S_ISDIR(entry_stat.st_mode) or _identity(entry_stat) != _identity(
                child_stat
            ):
                os.close(child_descriptor)
                _fail("source_symlink_forbidden")
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_absolute_file_no_follow(path: Path) -> int:
    absolute = _absolute_path(path)
    _require(absolute.name not in {"", ".", ".."}, "source_path_invalid")
    parent_descriptor = _open_absolute_directory_no_follow(absolute.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _nofollow_open_flag()
    try:
        descriptor: int | None = None
        try:
            descriptor = os.open(absolute.name, flags, dir_fd=parent_descriptor)
            entry_stat = os.stat(
                absolute.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            descriptor_stat = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno == errno.ELOOP:
                _fail("source_symlink_forbidden")
            _fail("source_file_open_failed")
        if (
            not stat.S_ISREG(entry_stat.st_mode)
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or _identity(entry_stat) != _identity(descriptor_stat)
        ):
            os.close(descriptor)
            _fail("source_symlink_forbidden")
        return descriptor
    finally:
        os.close(parent_descriptor)


def _after_secure_read_hook(_path: Path) -> None:
    """Test seam used to prove path-swap detection."""


def _secure_read_file(path: Path, *, max_bytes: int = 32 * 1024 * 1024) -> SecureFile:
    absolute = _absolute_path(path)
    descriptor = _open_absolute_file_no_follow(absolute)
    try:
        descriptor_before = os.fstat(descriptor)
        _require(stat.S_ISREG(descriptor_before.st_mode), "source_file_type_invalid")
        _require(descriptor_before.st_size <= max_bytes, "source_file_too_large")
        chunks: list[bytes] = []
        remaining = descriptor_before.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            _require(bool(chunk), "source_short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        descriptor_after = os.fstat(descriptor)
        _require(
            _path_stat_fingerprint(descriptor_after)
            == _path_stat_fingerprint(descriptor_before),
            "source_toctou_detected",
        )
    finally:
        os.close(descriptor)

    _after_secure_read_hook(absolute)
    try:
        verification_descriptor = _open_absolute_file_no_follow(absolute)
    except PublicationPackageError:
        _fail("source_toctou_detected")
    try:
        path_after = os.fstat(verification_descriptor)
        _require(
            _path_stat_fingerprint(path_after)
            == _path_stat_fingerprint(descriptor_before),
            "source_toctou_detected",
        )
    finally:
        os.close(verification_descriptor)
    return SecureFile(
        path=absolute,
        content=content,
        digest=_sha256(content),
        device=path_after.st_dev,
        inode=path_after.st_ino,
        size=path_after.st_size,
        mode=stat.S_IMODE(path_after.st_mode),
        mtime_ns=path_after.st_mtime_ns,
    )


def _verify_secure_file_unchanged(snapshot: SecureFile) -> None:
    current = _secure_read_file(snapshot.path, max_bytes=max(snapshot.size, 1))
    _require(
        (
            current.digest,
            current.device,
            current.inode,
            current.size,
            current.mode,
            current.mtime_ns,
        )
        == (
            snapshot.digest,
            snapshot.device,
            snapshot.inode,
            snapshot.size,
            snapshot.mode,
            snapshot.mtime_ns,
        ),
        "source_toctou_detected",
    )


def _inventory_regular_tree(root: Path) -> set[str]:
    absolute = _absolute_path(root)
    root_descriptor = _open_absolute_directory_no_follow(absolute)
    root_identity = _identity(os.fstat(root_descriptor))
    inventory: set[str] = set()

    def scan(descriptor: int, prefix: PurePosixPath) -> None:
        try:
            entries = list(os.scandir(descriptor))
        except OSError:
            _fail("review_bundle_scan_failed")
        for entry in entries:
            relative = prefix / entry.name
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                _fail("source_toctou_detected")
            _require(not stat.S_ISLNK(entry_stat.st_mode), "source_symlink_forbidden")
            if stat.S_ISDIR(entry_stat.st_mode):
                try:
                    child_descriptor = os.open(
                        entry.name,
                        _directory_open_flags(),
                        dir_fd=descriptor,
                    )
                except OSError:
                    _fail("source_toctou_detected")
                try:
                    _require(
                        _identity(os.fstat(child_descriptor)) == _identity(entry_stat),
                        "source_toctou_detected",
                    )
                    scan(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(entry_stat.st_mode):
                inventory.add(relative.as_posix())
            else:
                _fail("source_file_type_invalid")

    try:
        scan(root_descriptor, PurePosixPath())
        _require(
            _identity(os.fstat(root_descriptor)) == root_identity,
            "source_toctou_detected",
        )
        return inventory
    finally:
        os.close(root_descriptor)


def _run_git(repo: Path, *args: str, accepted_codes: tuple[int, ...] = (0,)) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("source_git_failed")
    _require(completed.returncode in accepted_codes, "source_git_failed")
    return completed.stdout.strip()


def _validate_source_repository(repo: Path, artifact_commit: str) -> str:
    _require(bool(_COMMIT_RE.fullmatch(artifact_commit)), "source_commit_invalid")
    resolved_repo = _absolute_path(repo)
    repository_descriptor = _open_absolute_directory_no_follow(resolved_repo)
    repository_identity = _identity(os.fstat(repository_descriptor))
    os.close(repository_descriptor)
    head = _run_git(resolved_repo, "rev-parse", "--verify", "HEAD^{commit}")
    _require(bool(_COMMIT_RE.fullmatch(head)), "source_head_invalid")
    status = _run_git(
        resolved_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    _require(not status, "source_worktree_dirty")
    resolved_artifact = _run_git(
        resolved_repo, "rev-parse", "--verify", f"{artifact_commit}^{{commit}}"
    )
    _require(resolved_artifact == artifact_commit, "source_commit_unresolved")
    _run_git(
        resolved_repo,
        "merge-base",
        "--is-ancestor",
        artifact_commit,
        head,
    )
    verification_descriptor = _open_absolute_directory_no_follow(resolved_repo)
    try:
        _require(
            _identity(os.fstat(verification_descriptor)) == repository_identity,
            "source_repo_toctou_detected",
        )
    finally:
        os.close(verification_descriptor)
    return head


def _origin(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None:
        return ""
    return f"https://{parsed.hostname.lower().rstrip('.')}"


def _validated_origins(values: list[str]) -> list[str]:
    supplied = [str(value or "").strip() for value in values]
    _require(
        len(supplied) == len(AUTHORIZED_PUBLIC_ORIGINS),
        "allowed_origin_set_invalid",
    )
    _require(
        all(_origin(value) == value for value in supplied),
        "allowed_origin_invalid",
    )
    _require(
        set(supplied) == AUTHORIZED_PUBLIC_ORIGINS,
        "allowed_origin_set_invalid",
    )
    return sorted(supplied)


def _get_dict(payload: dict[str, Any], key: str, code: str) -> dict[str, Any]:
    value = payload.get(key)
    _require(isinstance(value, dict), code)
    return dict(value)


def _assert_sha256(value: object, expected: str, code: str) -> None:
    _require(str(value or "").strip().lower() == expected, code)


def _validate_raw_reconstruction(
    reconstruction: dict[str, Any],
    *,
    slug: str,
    raw_assets: dict[str, SecureFile],
) -> None:
    _require(reconstruction.get("slug") == slug, "reconstruction_slug_mismatch")
    _require(
        reconstruction.get("provider") == GENERATED_PROVIDER,
        "reconstruction_provider_invalid",
    )
    _require(
        reconstruction.get("verified_provider_capture") is False,
        "provider_capture_must_remain_false",
    )
    _require(
        reconstruction.get("satisfies_verified_tour_gate") is False,
        "verified_tour_gate_must_remain_false",
    )
    _require(reconstruction.get("photos") == [], "layout_only_photos_invalid")
    _require(
        reconstruction.get("photo_reference_panels") == [],
        "layout_only_photo_panels_invalid",
    )
    viewer = _get_dict(reconstruction, "viewer", "reconstruction_viewer_missing")
    _require(viewer.get("version") == VIEWER_VERSION, "viewer_version_invalid")
    _require(viewer.get("photo_reference_panel_count") == 0, "layout_only_marker_invalid")
    _assert_sha256(
        viewer.get("sha256"),
        raw_assets["generated-reconstruction/viewer.html"].digest,
        "viewer_digest_drift",
    )
    floorplan = _get_dict(reconstruction, "floorplan", "floorplan_metadata_missing")
    floorplan_asset = raw_assets["generated-reconstruction/source-floorplan.png"]
    _require(floorplan.get("relpath") == "source-floorplan.png", "floorplan_relpath_invalid")
    _assert_sha256(floorplan.get("sha256"), floorplan_asset.digest, "floorplan_digest_drift")
    _require(floorplan.get("size_bytes") == floorplan_asset.size, "floorplan_size_drift")
    vendor = _get_dict(viewer, "vendor", "viewer_vendor_missing")
    emitted = _get_dict(vendor, "emitted", "viewer_vendor_emitted_missing")
    three = _get_dict(emitted, "three_module", "viewer_three_binding_missing")
    orbit = _get_dict(emitted, "orbit_controls", "viewer_orbit_binding_missing")
    _assert_sha256(
        three.get("sha256"),
        raw_assets["generated-reconstruction/vendor/three.module.js"].digest,
        "viewer_three_digest_drift",
    )
    _assert_sha256(
        orbit.get("sha256"),
        raw_assets[
            "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js"
        ].digest,
        "viewer_orbit_digest_drift",
    )
    route_labels = reconstruction.get("route_labels")
    _require(
        isinstance(route_labels, list)
        and bool(route_labels)
        and all(isinstance(row, str) and row.strip() for row in route_labels),
        "route_labels_invalid",
    )


def _validate_raw_tour(
    tour: dict[str, Any], *, slug: str, reconstruction: dict[str, Any]
) -> None:
    _require(tour.get("slug") == slug, "tour_slug_mismatch")
    generated = _get_dict(tour, "generated_reconstruction", "generated_metadata_missing")
    _require(generated.get("provider") == GENERATED_PROVIDER, "generated_provider_invalid")
    _require(
        generated.get("verified_provider_capture") is False,
        "provider_capture_must_remain_false",
    )
    _require(
        generated.get("satisfies_verified_tour_gate") is False,
        "verified_tour_gate_must_remain_false",
    )
    _require(generated.get("viewer_version") == VIEWER_VERSION, "viewer_version_invalid")
    _require(
        generated.get("viewer_relpath") == "generated-reconstruction/viewer.html",
        "viewer_relpath_invalid",
    )
    _require(
        generated.get("manifest_relpath")
        == "generated-reconstruction/reconstruction.json",
        "manifest_relpath_invalid",
    )
    _require(generated.get("photo_reference_panel_count") == 0, "layout_only_marker_invalid")
    _require(
        generated.get("route_labels") == reconstruction.get("route_labels"),
        "route_labels_drift",
    )


def _validate_browser_receipt(
    browser: dict[str, Any],
    *,
    slug: str,
    raw_assets: dict[str, SecureFile],
) -> None:
    _require(browser.get("schema") == BROWSER_RECEIPT_SCHEMA, "browser_schema_invalid")
    _require(browser.get("slug") == slug, "browser_slug_mismatch")
    _require(browser.get("status") == "pass", "browser_status_not_pass")
    _require(browser.get("failures") == [], "browser_failures_present")
    _assert_sha256(
        browser.get("viewer_sha256"),
        raw_assets["generated-reconstruction/viewer.html"].digest,
        "browser_viewer_digest_drift",
    )
    _assert_sha256(
        browser.get("reconstruction_sha256"),
        raw_assets["generated-reconstruction/reconstruction.json"].digest,
        "browser_reconstruction_digest_drift",
    )
    surfaces = _get_dict(browser, "surfaces", "browser_surfaces_missing")
    _require(set(surfaces) == _REQUIRED_BROWSER_SURFACES, "browser_surfaces_invalid")
    for name in sorted(_REQUIRED_BROWSER_SURFACES):
        row = surfaces.get(name)
        _require(isinstance(row, dict), "browser_surface_invalid")
        surface = dict(row)
        _require(surface.get("http_status") == 200, "browser_http_status_invalid")
        _require(surface.get("page_errors") == [], "browser_page_errors_present")
        _require(surface.get("console_errors") == [], "browser_console_errors_present")
        _require(surface.get("undersizedTargets") == [], "browser_target_size_failed")
        _require(
            surface.get("floorplanTargetOverlaps") == [],
            "browser_floorplan_overlap_failed",
        )
        _require(
            surface.get("clippedVisibleHotspotLabels") == [],
            "browser_hotspot_clipping_failed",
        )
        _require(surface.get("horizontalOverflowPx") == 0, "browser_overflow_failed")
        metrics = _get_dict(surface, "metrics", "browser_metrics_missing")
        if name == "webgl-fallback":
            _require(metrics.get("ready") is False, "browser_fallback_invalid")
            _require(surface.get("alertVisible") is True, "browser_fallback_alert_missing")
        else:
            _require(metrics.get("ready") is True, "browser_surface_not_ready")
            _require(metrics.get("routeStopCount") == 9, "browser_route_count_invalid")
            _require(metrics.get("photoPanelCount") == 0, "browser_photo_count_invalid")


def _validate_final_receipt(
    final: dict[str, Any],
    *,
    slug: str,
    artifact_commit: str,
    source_repo: Path,
    review_bundle: Path,
    raw_assets: dict[str, SecureFile],
    browser_file: SecureFile,
) -> None:
    _require(final.get("schema") == REVIEW_RECEIPT_SCHEMA, "final_schema_invalid")
    _require(final.get("slug") == slug, "final_slug_mismatch")
    _require(
        final.get("status") == "polished_review_candidate_pass_guarded_not_published",
        "final_status_invalid",
    )
    source = _get_dict(final, "source", "final_source_missing")
    _require(source.get("commit") == artifact_commit, "final_source_commit_mismatch")
    _require(source.get("worktree_clean") is True, "final_source_not_clean")
    receipt_repo = _absolute_path(Path(str(source.get("repo") or "")))
    _require(receipt_repo == source_repo, "final_source_repo_mismatch")
    review = _get_dict(final, "review_bundle", "final_review_bundle_missing")
    receipt_bundle = _absolute_path(Path(str(review.get("root") or "")))
    _require(receipt_bundle == review_bundle, "final_review_bundle_mismatch")
    _require(
        _absolute_path(Path(str(review.get("viewer") or "")))
        == review_bundle / "generated-reconstruction/viewer.html",
        "final_viewer_path_mismatch",
    )
    _require(
        _absolute_path(Path(str(review.get("reconstruction") or "")))
        == review_bundle / "generated-reconstruction/reconstruction.json",
        "final_reconstruction_path_mismatch",
    )
    _assert_sha256(
        review.get("viewer_sha256"),
        raw_assets["generated-reconstruction/viewer.html"].digest,
        "final_viewer_digest_drift",
    )
    _assert_sha256(
        review.get("reconstruction_sha256"),
        raw_assets["generated-reconstruction/reconstruction.json"].digest,
        "final_reconstruction_digest_drift",
    )
    _assert_sha256(
        review.get("tour_manifest_sha256"),
        raw_assets["tour.json"].digest,
        "final_tour_digest_drift",
    )
    _assert_sha256(
        review.get("floorplan_sha256"),
        raw_assets["generated-reconstruction/source-floorplan.png"].digest,
        "final_floorplan_digest_drift",
    )
    _require(review.get("runtime_publish_required") is False, "review_publish_state_invalid")
    _require(review.get("runtime_publish_ok") is True, "review_publish_state_invalid")
    _require(review.get("verified_provider_capture") is False, "provider_capture_must_remain_false")
    _require(review.get("satisfies_verified_tour_gate") is False, "verified_tour_gate_must_remain_false")

    visual = _get_dict(final, "visual_verification", "final_visual_missing")
    _require(
        _absolute_path(Path(str(visual.get("browser_receipt") or "")))
        == browser_file.path,
        "final_browser_receipt_path_mismatch",
    )
    _assert_sha256(
        visual.get("browser_receipt_sha256"),
        browser_file.digest,
        "final_browser_receipt_digest_drift",
    )
    _require(visual.get("browser_status") == "pass", "final_browser_status_invalid")
    _require(visual.get("browser_failures") == [], "final_browser_failures_present")
    _require(visual.get("route_status") == "pass", "final_route_status_invalid")
    _require(visual.get("route_failures") == [], "final_route_failures_present")
    _require(visual.get("route_stop_count") == 9, "final_route_count_invalid")
    _require(
        set(visual.get("surfaces") or []) == _REQUIRED_BROWSER_SURFACES,
        "final_browser_surfaces_invalid",
    )

    verification = _get_dict(final, "verification", "final_verification_missing")
    _require(
        _get_dict(
            verification,
            "property_generated_reconstruction",
            "final_reconstruction_verification_missing",
        ).get("result")
        == "pass",
        "final_reconstruction_verification_failed",
    )
    _require(
        _get_dict(
            verification,
            "property_tour_control_and_importers",
            "final_control_verification_missing",
        ).get("result")
        == "pass",
        "final_control_verification_failed",
    )
    for key in (
        "independent_camera_geometry_accessibility_review",
        "independent_runtime_publish_safety_review",
    ):
        _require(
            _get_dict(verification, key, "final_independent_review_missing").get("result")
            == "approved",
            "final_independent_review_failed",
        )
    live_guard = _get_dict(final, "live_guard", "final_live_guard_missing")
    _require(live_guard.get("runtime_mutation_detected") is False, "review_runtime_mutated")
    _require(
        live_guard.get("all_observed_product_routes_guarded_404") is True,
        "review_publication_guard_invalid",
    )
    blockers = final.get("release_blockers")
    _require(
        isinstance(blockers, list)
        and any("provider capture" in str(row).lower() for row in blockers),
        "provider_capture_blocker_missing",
    )


def _private_value_blockers(value: object, *, path: tuple[str, ...] = ()) -> list[str]:
    blockers: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            child_path = (*path, normalized)
            if normalized in _FORBIDDEN_PUBLIC_KEYS:
                blockers.append(".".join(child_path))
            blockers.extend(_private_value_blockers(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            blockers.extend(_private_value_blockers(child, path=(*path, str(index))))
    elif isinstance(value, str):
        lowered = value.lower().replace("\\", "/")
        if (
            "/home/" in lowered
            or "/tmp/" in lowered
            or "/var/tmp/" in lowered
            or "cf-email:" in lowered
            or _EMAIL_RE.search(value.encode("utf-8", errors="ignore"))
        ):
            blockers.append(".".join(path))
    return blockers


def _sanitize_reconstruction(
    raw: dict[str, Any], *, artifact_commit: str
) -> dict[str, Any]:
    normalized_input = copy.deepcopy(raw)
    normalized_floorplan = _get_dict(
        normalized_input, "floorplan", "floorplan_metadata_missing"
    )
    normalized_floorplan["source_path"] = (
        f"property://{PROPERTY_REPOSITORY}/{artifact_commit}/floorplan-apartment-crop.png"
    )
    normalized_input["floorplan"] = normalized_floorplan
    _require(
        not _private_value_blockers(normalized_input),
        "private_reconstruction_field_present",
    )
    _require(
        set(normalized_input) == _RECONSTRUCTION_INPUT_KEYS,
        "reconstruction_input_keys_invalid",
    )

    floorplan = _get_dict(normalized_input, "floorplan", "floorplan_metadata_missing")
    _require(set(floorplan) == _FLOORPLAN_KEYS, "floorplan_keys_invalid")
    viewer = _get_dict(normalized_input, "viewer", "reconstruction_viewer_missing")
    _require(set(viewer) == _VIEWER_KEYS, "viewer_keys_invalid")
    vendor = _get_dict(viewer, "vendor", "viewer_vendor_missing")
    emitted = _get_dict(vendor, "emitted", "viewer_vendor_emitted_missing")
    three = _get_dict(emitted, "three_module", "viewer_three_binding_missing")
    orbit = _get_dict(emitted, "orbit_controls", "viewer_orbit_binding_missing")
    geometry = _get_dict(normalized_input, "geometry", "geometry_missing")
    _require(set(geometry).issubset(_GEOMETRY_KEYS), "geometry_keys_invalid")
    walkable_scene = _get_dict(
        normalized_input, "walkable_scene", "walkable_scene_missing"
    )
    _require(
        set(walkable_scene).issubset(_WALKABLE_SCENE_KEYS),
        "walkable_scene_keys_invalid",
    )
    dimensions = _get_dict(
        normalized_input, "room_dimensions_m", "room_dimensions_missing"
    )
    _require(
        set(dimensions).issubset(_ROOM_DIMENSION_KEYS),
        "room_dimension_keys_invalid",
    )
    _require(
        all(str(vendor.get(key) or "").strip() for key in ("name", "version", "license")),
        "viewer_vendor_identity_invalid",
    )

    sanitized = {
        "schema": PUBLIC_RECONSTRUCTION_SCHEMA,
        "slug": normalized_input["slug"],
        "provider": GENERATED_PROVIDER,
        "generated_at": normalized_input["generated_at"],
        "method": normalized_input["method"],
        "style_label": normalized_input["style_label"],
        "source_commit": artifact_commit,
        "synthetic": True,
        "capture_mode": False,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "disclosure": DISCLOSURE,
        "room_dimensions_m": {
            key: copy.deepcopy(dimensions[key]) for key in sorted(dimensions)
        },
        "floorplan": {
            key: copy.deepcopy(floorplan[key]) for key in sorted(_FLOORPLAN_KEYS)
        },
        "photos": [],
        "photo_relpaths": [],
        "photo_reference_panels": [],
        "photo_reference_panel_count": 0,
        "route_labels": copy.deepcopy(normalized_input["route_labels"]),
        "walkthrough_route_labels": copy.deepcopy(
            normalized_input["walkthrough_route_labels"]
        ),
        "walkable_scene": {
            key: copy.deepcopy(walkable_scene[key]) for key in sorted(walkable_scene)
        },
        "geometry": {
            key: copy.deepcopy(geometry[key]) for key in sorted(geometry)
        },
        "viewer": {
            "relpath": viewer["relpath"],
            "version": viewer["version"],
            "photo_reference_panel_count": 0,
            "sha256": viewer["sha256"],
            "vendor": {
                "name": vendor["name"],
                "version": vendor["version"],
                "license": vendor["license"],
                "emitted": {
                    "three_module": {
                        "relpath": three["relpath"],
                        "sha256": three["sha256"],
                    },
                    "orbit_controls": {
                        "relpath": orbit["relpath"],
                        "sha256": orbit["sha256"],
                    },
                },
            },
        },
    }
    _require(not _private_value_blockers(sanitized), "private_reconstruction_field_present")
    return sanitized


def _asset_binding(
    *, relpath: str, content: bytes, mime_type: str, role: str
) -> dict[str, object]:
    return {
        "path": relpath,
        "sha256": _sha256(content),
        "size_bytes": len(content),
        "mime_type": mime_type,
        "role": role,
    }


def _build_manifest_without_authority(
    *,
    slug: str,
    artifact_commit: str,
    asset_bindings: list[dict[str, object]],
    final_receipt_sha256: str,
    browser_receipt_sha256: str,
) -> dict[str, Any]:
    route_labels: list[str] = []
    generated = {
        "provider": GENERATED_PROVIDER,
        "synthetic": True,
        "capture_mode": False,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "viewer_version": VIEWER_VERSION,
        "viewer_relpath": "generated-reconstruction/viewer.html",
        "manifest_relpath": "generated-reconstruction/reconstruction.json",
        "floorplan_relpath": "generated-reconstruction/source-floorplan.png",
        "photo_relpaths": [],
        "photo_reference_panel_count": 0,
        "disclosure": DISCLOSURE,
    }
    release = {
        "contract": EA_GENERATED_VIEWER_RELEASE_CONTRACT,
        "status": "ready",
        "provider": GENERATED_PROVIDER,
        "viewer_relpath": "generated-reconstruction/viewer.html",
        "asset_bindings": asset_bindings,
        "browser_receipt_sha256": browser_receipt_sha256,
        "source_provenance_receipt_sha256": final_receipt_sha256,
        "publication_authority_receipt_sha256": None,
        "security_review_receipt_sha256": final_receipt_sha256,
        "accessibility_review_receipt_sha256": final_receipt_sha256,
        "browser_interaction_verified": True,
        "visual_quality_review_passed": True,
        "security_review_passed": True,
        "accessibility_review_passed": True,
        "source_provenance_verified": True,
        "publication_authority_verified": True,
        "public_activation_authority": True,
        "release_revision": f"property-3d-{artifact_commit[:12]}",
        "disclosure": DISCLOSURE,
        "synthetic": True,
        "capture_mode": False,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "revoked": False,
        "disqualified": False,
    }
    return {
        "schema": PUBLIC_TOUR_PACKAGE_SCHEMA,
        "slug": slug,
        "display_title": "Interactive layout preview",
        "scene_strategy": "generated_layout_reconstruction",
        "creation_mode": "propertyquarry_governed_publication",
        "source_commit": artifact_commit,
        "synthetic": True,
        "generated_reconstruction": generated,
        "generated_viewer_release": release,
        "route_labels": route_labels,
    }


def _build_authority_receipt(
    *,
    slug: str,
    artifact_commit: str,
    packager_commit: str,
    user_instruction_sha256: str,
    origins: list[str],
    pre_authority_manifest_sha256: str,
    asset_bindings: list[dict[str, object]],
    final_receipt: dict[str, Any],
    final_receipt_sha256: str,
    browser_receipt: dict[str, Any],
    browser_receipt_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": PUBLICATION_AUTHORITY_SCHEMA,
        "status": "authorized",
        "owner": "PropertyQuarry",
        "repository": PROPERTY_REPOSITORY,
        "slug": slug,
        "public_activation_authority": True,
        "publication_authority_verified": True,
        "user_instruction_sha256": user_instruction_sha256,
        "allowed_public_origins": origins,
        "source": {
            "artifact_commit": artifact_commit,
            "packager_commit": packager_commit,
            "worktree_clean": True,
        },
        "classification": {
            "synthetic": True,
            "capture_mode": False,
            "verified_provider_capture": False,
            "satisfies_verified_tour_gate": False,
            "disclosure": DISCLOSURE,
        },
        "review_receipts": {
            "flagship_final": {
                "schema": final_receipt["schema"],
                "status": final_receipt["status"],
                "sha256": final_receipt_sha256,
            },
            "exact_viewer_browser": {
                "schema": browser_receipt["schema"],
                "status": browser_receipt["status"],
                "sha256": browser_receipt_sha256,
            },
        },
        "package": {
            "public_bundle_relpath": f"public_property_tours/{slug}",
            "public_file_relpaths": sorted(_PUBLIC_FILE_RELPATHS),
            "public_file_count": len(_PUBLIC_FILE_RELPATHS),
            "pre_authority_manifest_canonicalization": (
                "utf8_sorted_keys_compact_ensure_ascii_false_no_trailing_lf_"
                "with_publication_authority_receipt_sha256_null"
            ),
            "pre_authority_manifest_canonical_sha256": (
                pre_authority_manifest_sha256
            ),
            "asset_bindings": asset_bindings,
        },
    }


def _write_new_file(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | _nofollow_open_flag()
    )
    try:
        descriptor = os.open(path, flags, mode)
    except OSError:
        _fail("output_write_failed")
    try:
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            _require(count > 0, "output_write_failed")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    _require(
        source_name not in {"", ".", ".."}
        and destination_name not in {"", ".", ".."}
        and "/" not in source_name
        and "/" not in destination_name,
        "output_name_invalid",
    )
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    _require(renameat2 is not None, "rename_noreplace_unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        _fail("output_root_exists")
    _fail("output_install_failed")


def _set_directory_modes(root: Path, *, bundle: Path, authority_dir: Path) -> None:
    root.chmod(0o755)
    (root / "public_property_tours").chmod(0o755)
    for directory, directory_names, _file_names in os.walk(bundle):
        Path(directory).chmod(0o755)
        for name in directory_names:
            (Path(directory) / name).chmod(0o755)
    authority_dir.chmod(0o700)


def _validate_materialized_tree(
    root: Path,
    *,
    slug: str,
    authority_digest: str,
    expected_public_bytes: dict[str, bytes],
) -> None:
    bundle = root / "public_property_tours" / slug
    authority = root / "publication-authority" / f"{slug}.json"
    _require(_inventory_regular_tree(bundle) == _PUBLIC_FILE_RELPATHS, "output_extras_present")
    for relpath in sorted(_PUBLIC_FILE_RELPATHS):
        file_path = bundle / relpath
        file_stat = os.lstat(file_path)
        _require(stat.S_ISREG(file_stat.st_mode), "output_file_type_invalid")
        _require(stat.S_IMODE(file_stat.st_mode) == 0o644, "output_file_mode_invalid")
        _require(_secure_read_file(file_path).content == expected_public_bytes[relpath], "output_digest_drift")
    for directory, directory_names, _file_names in os.walk(bundle):
        _require(stat.S_IMODE(os.lstat(directory).st_mode) == 0o755, "output_directory_mode_invalid")
        for name in directory_names:
            _require(
                stat.S_IMODE(os.lstat(Path(directory) / name).st_mode) == 0o755,
                "output_directory_mode_invalid",
            )
    authority_stat = os.lstat(authority)
    _require(stat.S_ISREG(authority_stat.st_mode), "authority_file_type_invalid")
    _require(stat.S_IMODE(authority_stat.st_mode) == 0o600, "authority_file_mode_invalid")
    _require(_secure_read_file(authority).digest == authority_digest, "authority_digest_drift")
    root_inventory = _inventory_regular_tree(root)
    expected_root_files = {
        *(f"public_property_tours/{slug}/{path}" for path in _PUBLIC_FILE_RELPATHS),
        f"publication-authority/{slug}.json",
    }
    _require(root_inventory == expected_root_files, "output_extras_present")


def materialize_publication_package(
    *,
    source_repo: Path,
    artifact_commit: str,
    review_bundle: Path,
    final_review_receipt: Path,
    expected_final_review_receipt_sha256: str,
    browser_review_receipt: Path,
    expected_browser_review_receipt_sha256: str,
    slug: str,
    user_instruction_sha256: str,
    allowed_public_origins: list[str],
    output_root: Path,
) -> dict[str, object]:
    """Validate review evidence and atomically materialize one publication package."""

    artifact_commit = artifact_commit.strip().lower()
    expected_final_review_receipt_sha256 = (
        expected_final_review_receipt_sha256.strip().lower()
    )
    expected_browser_review_receipt_sha256 = (
        expected_browser_review_receipt_sha256.strip().lower()
    )
    user_instruction_sha256 = user_instruction_sha256.strip().lower()
    _require(bool(_SLUG_RE.fullmatch(slug)), "slug_invalid")
    _require(slug == AUTHORIZED_SLUG, "slug_unauthorized")
    _require(artifact_commit == AUTHORIZED_ARTIFACT_COMMIT, "source_commit_unauthorized")
    _require(bool(_SHA256_RE.fullmatch(user_instruction_sha256)), "user_instruction_hash_invalid")
    _require(
        user_instruction_sha256 == AUTHORIZED_USER_INSTRUCTION_SHA256,
        "user_instruction_hash_unauthorized",
    )
    _require(
        bool(_SHA256_RE.fullmatch(expected_final_review_receipt_sha256)),
        "expected_final_receipt_hash_invalid",
    )
    _require(
        bool(_SHA256_RE.fullmatch(expected_browser_review_receipt_sha256)),
        "expected_browser_receipt_hash_invalid",
    )
    _require(
        expected_final_review_receipt_sha256
        == AUTHORIZED_FINAL_REVIEW_RECEIPT_SHA256,
        "final_receipt_hash_unauthorized",
    )
    _require(
        expected_browser_review_receipt_sha256
        == AUTHORIZED_BROWSER_REVIEW_RECEIPT_SHA256,
        "browser_receipt_hash_unauthorized",
    )
    origins = _validated_origins(allowed_public_origins)
    source_repo = _absolute_path(source_repo)
    packager_commit = _validate_source_repository(source_repo, artifact_commit)

    review_bundle = _absolute_path(review_bundle)
    _require(
        _inventory_regular_tree(review_bundle) == _RAW_ALLOWED_FILES,
        "review_bundle_inventory_invalid",
    )
    raw_assets = {
        relpath: _secure_read_file(review_bundle / relpath)
        for relpath in sorted(_RAW_ALLOWED_FILES)
    }
    final_file = _secure_read_file(final_review_receipt)
    browser_file = _secure_read_file(browser_review_receipt)
    _require(
        final_file.digest == expected_final_review_receipt_sha256,
        "final_receipt_bytes_drift",
    )
    _require(
        browser_file.digest == expected_browser_review_receipt_sha256,
        "browser_receipt_bytes_drift",
    )

    raw_tour = _parse_json_object(raw_assets["tour.json"].content, code="tour_json_invalid")
    raw_reconstruction = _parse_json_object(
        raw_assets["generated-reconstruction/reconstruction.json"].content,
        code="reconstruction_json_invalid",
    )
    final_receipt = _parse_json_object(final_file.content, code="final_receipt_invalid")
    browser_receipt = _parse_json_object(browser_file.content, code="browser_receipt_invalid")
    _validate_raw_reconstruction(
        raw_reconstruction,
        slug=slug,
        raw_assets=raw_assets,
    )
    _validate_raw_tour(raw_tour, slug=slug, reconstruction=raw_reconstruction)
    _validate_browser_receipt(
        browser_receipt,
        slug=slug,
        raw_assets=raw_assets,
    )
    _validate_final_receipt(
        final_receipt,
        slug=slug,
        artifact_commit=artifact_commit,
        source_repo=source_repo,
        review_bundle=review_bundle,
        raw_assets=raw_assets,
        browser_file=browser_file,
    )

    sanitized_reconstruction = _sanitize_reconstruction(
        raw_reconstruction,
        artifact_commit=artifact_commit,
    )
    reconstruction_bytes = _json_file_bytes(sanitized_reconstruction)
    public_asset_bytes = {
        "generated-reconstruction/viewer.html": raw_assets[
            "generated-reconstruction/viewer.html"
        ].content,
        "generated-reconstruction/reconstruction.json": reconstruction_bytes,
        "generated-reconstruction/source-floorplan.png": raw_assets[
            "generated-reconstruction/source-floorplan.png"
        ].content,
        "generated-reconstruction/vendor/three.module.js": raw_assets[
            "generated-reconstruction/vendor/three.module.js"
        ].content,
        "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js": raw_assets[
            "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js"
        ].content,
    }
    asset_bindings = [
        _asset_binding(
            relpath=relpath,
            content=public_asset_bytes[relpath],
            mime_type=mime_type,
            role=role,
        )
        for relpath, mime_type, role in _ASSET_SPECS
    ]
    manifest = _build_manifest_without_authority(
        slug=slug,
        artifact_commit=artifact_commit,
        asset_bindings=asset_bindings,
        final_receipt_sha256=final_file.digest,
        browser_receipt_sha256=browser_file.digest,
    )
    manifest["route_labels"] = list(raw_reconstruction["route_labels"])
    generated_manifest = _get_dict(
        manifest, "generated_reconstruction", "generated_metadata_missing"
    )
    generated_manifest["route_labels"] = list(raw_reconstruction["route_labels"])
    generated_manifest["room_stop_count"] = len(raw_reconstruction["route_labels"])
    manifest["generated_reconstruction"] = generated_manifest
    _require(not _private_value_blockers(manifest), "private_tour_field_present")
    pre_authority_manifest_sha256 = _sha256(_canonical_json_bytes(manifest))
    authority_receipt = _build_authority_receipt(
        slug=slug,
        artifact_commit=artifact_commit,
        packager_commit=packager_commit,
        user_instruction_sha256=user_instruction_sha256,
        origins=origins,
        pre_authority_manifest_sha256=pre_authority_manifest_sha256,
        asset_bindings=asset_bindings,
        final_receipt=final_receipt,
        final_receipt_sha256=final_file.digest,
        browser_receipt=browser_receipt,
        browser_receipt_sha256=browser_file.digest,
    )
    authority_bytes = _json_file_bytes(authority_receipt)
    authority_digest = _sha256(authority_bytes)
    manifest["generated_viewer_release"][
        "publication_authority_receipt_sha256"
    ] = authority_digest
    tour_bytes = _json_file_bytes(manifest)
    public_bytes = {"tour.json": tour_bytes, **public_asset_bytes}
    for relpath in _TEXT_PUBLIC_ASSETS | {"tour.json"}:
        content = public_bytes[relpath]
        _require(b"/home/" not in content and b"/tmp/" not in content, "private_path_present")
        _require(b"cf-email:" not in content.lower(), "private_identity_present")
        _require(not _EMAIL_RE.search(content), "private_email_present")

    output_root = _absolute_path(output_root)
    _require(output_root.name not in {"", ".", ".."}, "output_name_invalid")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_parent_descriptor = _open_absolute_directory_no_follow(output_root.parent)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{output_root.name}.staging-", dir=output_root.parent
        ) as temporary:
            temporary_path = Path(temporary)
            try:
                temporary_descriptor = os.open(
                    temporary_path.name,
                    _directory_open_flags(),
                    dir_fd=output_parent_descriptor,
                )
            except OSError:
                _fail("output_staging_open_failed")
            try:
                _require(
                    _identity(os.fstat(temporary_descriptor))
                    == _identity(os.lstat(temporary_path)),
                    "output_parent_toctou_detected",
                )
                staging = temporary_path / "package"
                bundle = staging / "public_property_tours" / slug
                authority_dir = staging / "publication-authority"
                bundle.mkdir(parents=True, mode=0o755)
                authority_dir.mkdir(parents=True, mode=0o700)
                for relpath in sorted(public_bytes):
                    _write_new_file(bundle / relpath, public_bytes[relpath], mode=0o644)
                authority_path = authority_dir / f"{slug}.json"
                _write_new_file(authority_path, authority_bytes, mode=0o600)
                _set_directory_modes(staging, bundle=bundle, authority_dir=authority_dir)
                _validate_materialized_tree(
                    staging,
                    slug=slug,
                    authority_digest=authority_digest,
                    expected_public_bytes=public_bytes,
                )
                try:
                    staging_descriptor = os.open(
                        "package",
                        _directory_open_flags(),
                        dir_fd=temporary_descriptor,
                    )
                except OSError:
                    _fail("output_staging_open_failed")
                try:
                    _require(
                        _identity(os.fstat(staging_descriptor))
                        == _identity(os.lstat(staging)),
                        "output_parent_toctou_detected",
                    )
                    staging_identity = _identity(os.fstat(staging_descriptor))
                    for snapshot in [*raw_assets.values(), final_file, browser_file]:
                        _verify_secure_file_unchanged(snapshot)
                    _require(
                        _inventory_regular_tree(review_bundle) == _RAW_ALLOWED_FILES,
                        "review_bundle_inventory_drift",
                    )
                    _require(
                        _validate_source_repository(source_repo, artifact_commit)
                        == packager_commit,
                        "source_head_drift",
                    )
                    _rename_noreplace(
                        temporary_descriptor,
                        "package",
                        output_parent_descriptor,
                        output_root.name,
                    )
                    installed_descriptor = os.open(
                        output_root.name,
                        _directory_open_flags(),
                        dir_fd=output_parent_descriptor,
                    )
                    try:
                        _require(
                            _identity(os.fstat(installed_descriptor)) == staging_identity,
                            "output_install_identity_drift",
                        )
                    finally:
                        os.close(installed_descriptor)
                finally:
                    os.close(staging_descriptor)
            finally:
                os.close(temporary_descriptor)
    finally:
        os.close(output_parent_descriptor)

    _validate_materialized_tree(
        output_root,
        slug=slug,
        authority_digest=authority_digest,
        expected_public_bytes=public_bytes,
    )
    return {
        "schema": "propertyquarry.generated-viewer-publication-materialization.v1",
        "status": "pass",
        "owner": "PropertyQuarry",
        "slug": slug,
        "output_root": str(output_root),
        "bundle_dir": str(output_root / "public_property_tours" / slug),
        "authority_receipt": str(
            output_root / "publication-authority" / f"{slug}.json"
        ),
        "authority_receipt_sha256": authority_digest,
        "pre_authority_manifest_canonical_sha256": pre_authority_manifest_sha256,
        "tour_manifest_sha256": _sha256(tour_bytes),
        "artifact_commit": artifact_commit,
        "packager_commit": packager_commit,
        "public_file_count": len(public_bytes),
        "asset_binding_count": len(asset_bindings),
        "public_activation_authority": True,
        "publication_authority_verified": True,
        "synthetic": True,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate PropertyQuarry 3D review evidence and materialize an atomic, "
            "Property-owned public viewer package."
        )
    )
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--artifact-source-commit", required=True)
    parser.add_argument("--review-bundle", required=True)
    parser.add_argument("--final-review-receipt", required=True)
    parser.add_argument("--expected-final-review-receipt-sha256", required=True)
    parser.add_argument("--browser-review-receipt", required=True)
    parser.add_argument("--expected-browser-review-receipt-sha256", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--user-instruction-sha256", required=True)
    parser.add_argument(
        "--allowed-public-origin",
        action="append",
        required=True,
        dest="allowed_public_origins",
    )
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = materialize_publication_package(
            source_repo=Path(args.source_repo),
            artifact_commit=args.artifact_source_commit,
            review_bundle=Path(args.review_bundle),
            final_review_receipt=Path(args.final_review_receipt),
            expected_final_review_receipt_sha256=(
                args.expected_final_review_receipt_sha256
            ),
            browser_review_receipt=Path(args.browser_review_receipt),
            expected_browser_review_receipt_sha256=(
                args.expected_browser_review_receipt_sha256
            ),
            slug=args.slug,
            user_instruction_sha256=args.user_instruction_sha256,
            allowed_public_origins=list(args.allowed_public_origins),
            output_root=Path(args.output_root),
        )
    except PublicationPackageError as exc:
        receipt = {
            "schema": "propertyquarry.generated-viewer-publication-materialization.v1",
            "status": "blocked",
            "error": exc.code,
        }
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
