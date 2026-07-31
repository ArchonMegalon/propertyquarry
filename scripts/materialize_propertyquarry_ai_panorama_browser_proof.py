#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.api.routes import public_tours
from app.product import property_tour_ai_panorama_intake as installer_intake
from app.product import property_tour_hosting
from scripts.propertyquarry_playwright_runtime import playwright_engine_launch_browser


CONTRACT_NAME = "propertyquarry.ai_panorama_browser_proof.v1"
CANDIDATE_MARKER_CONTRACT = installer_intake.AI_PANORAMA_CANDIDATE_MARKER_CONTRACT
CANDIDATE_MARKER_RELPATH = installer_intake.AI_PANORAMA_CANDIDATE_MARKER_RELPATH
MATERIALIZATION_RECEIPT_CONTRACT = (
    installer_intake.AI_PANORAMA_MATERIALIZATION_RECEIPT_CONTRACT
)
CANONICAL_DISCLOSURE = (
    "AI-reconstructed from listing photos; not a captured 360 or measured survey."
)
FLOORPLAN_CANONICAL_DISCLOSURE = (
    "AI-reconstructed from an operator-provided architectural floorplan; "
    "not a captured 360 or measured survey."
)
MEASURED_FLOORPLAN_CANONICAL_DISCLOSURE = (
    "AI-reconstructed from an operator-provided architectural floorplan with "
    "room dimensions taken from its dimension lines; not a captured 360 or "
    "measured survey."
)
ANALYZED_FLOORPLAN_CANONICAL_DISCLOSURE = (
    "AI-reconstructed from a reviewed architectural floorplan analysis with "
    "source-linked room dimensions and a derived-plan round-trip check; not a "
    "captured 360 or measured survey."
)
CANONICAL_DISCLOSURES = frozenset(
    {
        CANONICAL_DISCLOSURE,
        FLOORPLAN_CANONICAL_DISCLOSURE,
        MEASURED_FLOORPLAN_CANONICAL_DISCLOSURE,
        ANALYZED_FLOORPLAN_CANONICAL_DISCLOSURE,
    }
)
RENDERER_MODULE_PATH = "/tours/runtime/three-0.167.1.module.js"
RENDERER_MODULE_SHA256 = (
    "5289ca2dfde8572bd7715b9fa2ca929db12bae87e9a2cb53e431662df7039506"
)
BROWSER_RECEIPT_RELPATH = "proof/browser-proof.json"
SCREENSHOT_RELPATHS = {
    "desktop": "proof/browser-desktop.png",
    "mobile": "proof/browser-mobile.png",
    "dollhouse": "proof/browser-dollhouse.png",
}
MAX_SOURCE_FILES = 1_000
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 1_048_576
MAX_BROWSER_ASSET_BYTES = 32 * 1024 * 1024
SLOW_NETWORK_LATENCY_SECONDS = 0.150
SLOW_NETWORK_BYTES_PER_SECOND = 500_000
SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class MaterializationError(RuntimeError):
    pass


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MaterializationError("canonical_json_invalid") from exc


def _candidate_representation_disclosure(candidate: "PreparedCandidate") -> str:
    walkable_scene = candidate.payload.get("walkable_scene")
    disclosure = (
        str(walkable_scene.get("representation_disclosure") or "").strip()
        if isinstance(walkable_scene, Mapping)
        else ""
    )
    if disclosure not in CANONICAL_DISCLOSURES:
        raise MaterializationError("representation_disclosure_invalid")
    return disclosure


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MaterializationError(f"json_duplicate_key:{key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise MaterializationError(f"json_nonfinite:{value}")


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MaterializationError(f"json_missing:{path.name}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_JSON_BYTES
    ):
        raise MaterializationError(f"json_file_invalid:{path.name}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise MaterializationError(f"json_payload_invalid:{path.name}") from exc
    if not isinstance(payload, dict):
        raise MaterializationError(f"json_object_required:{path.name}")
    return payload


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MaterializationError(f"regular_file_required:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise MaterializationError(f"file_changed_during_read:{path.name}")
    return digest.hexdigest()


def _safe_relpath(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if (
        not raw
        or "\x00" in raw
        or "://" in raw
        or raw.startswith("/")
        or any(character in raw for character in ":?#[]@!$&'()*+,;=%")
    ):
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        return ""
    return "/".join(path.parts)


def _confined_path(root: Path, relpath: object) -> Path:
    safe = _safe_relpath(relpath)
    if not safe:
        raise MaterializationError("candidate_relpath_invalid")
    resolved_root = root.resolve()
    candidate = (resolved_root / safe).resolve()
    if resolved_root not in candidate.parents:
        raise MaterializationError("candidate_relpath_escape")
    return candidate


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise MaterializationError(f"atomic_target_symlink:{path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _atomic_create(
    path: Path,
    content: bytes,
    *,
    mode: int = 0o600,
    label: str = "receipt_out",
) -> tuple[int, int]:
    try:
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise MaterializationError(f"{label}_parent_missing") from exc
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
    ):
        raise MaterializationError(f"{label}_parent_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    linked = False
    linked_identity: tuple[int, int] | None = None
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_metadata = temporary_path.lstat()
        linked_identity = (
            int(temporary_metadata.st_dev),
            int(temporary_metadata.st_ino),
        )
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise MaterializationError(f"{label}_exists") from exc
        linked = True
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino)) != linked_identity
        ):
            raise MaterializationError(f"{label}_create_invalid")
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        return linked_identity
    except Exception:
        if linked and linked_identity is not None:
            try:
                _unlink_created_file(path, linked_identity)
            except Exception as cleanup_exc:
                raise MaterializationError(
                    f"{label}_create_cleanup_failed"
                ) from cleanup_exc
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _unlink_created_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino)) != identity
    ):
        raise MaterializationError("receipt_out_cleanup_identity_changed")
    path.unlink()
    parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _remove_owned_directory(
    path: Path,
    *,
    parent: Path,
    identity: tuple[int, int],
) -> None:
    if path.parent != parent:
        raise MaterializationError("candidate_cleanup_path_invalid")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino)) != identity
    ):
        raise MaterializationError("candidate_cleanup_identity_changed")
    shutil.rmtree(path)


def _reject_symlink_components(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise MaterializationError("absolute_paths_required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise MaterializationError(f"{label}_path_invalid") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise MaterializationError(f"{label}_symlink_component")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _resolved_materialization_paths(
    *,
    source_bundle: Path,
    candidate_public_root: Path,
    receipt_out: Path | None,
) -> tuple[Path, Path, Path | None]:
    _reject_symlink_components(source_bundle, label="source_bundle")
    _reject_symlink_components(candidate_public_root, label="candidate_root")
    resolved_source = source_bundle.resolve(strict=False)
    resolved_candidate = candidate_public_root.resolve(strict=False)
    if _paths_overlap(resolved_source, resolved_candidate):
        raise MaterializationError("candidate_source_overlap")
    resolved_receipt: Path | None = None
    if receipt_out is not None:
        _reject_symlink_components(receipt_out, label="receipt_out")
        resolved_receipt = receipt_out.resolve(strict=False)
        if _paths_overlap(resolved_receipt, resolved_source):
            raise MaterializationError("receipt_out_source_overlap")
        if _paths_overlap(resolved_receipt, resolved_candidate):
            raise MaterializationError("receipt_out_candidate_overlap")
        if resolved_receipt.exists():
            raise MaterializationError("receipt_out_exists")
        try:
            parent_metadata = resolved_receipt.parent.lstat()
        except OSError as exc:
            raise MaterializationError("receipt_out_parent_missing") from exc
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
        ):
            raise MaterializationError("receipt_out_parent_invalid")
    return resolved_source, resolved_candidate, resolved_receipt


def _regular_tree_snapshot(root: Path) -> tuple[int, int, str]:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise MaterializationError("source_bundle_missing") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise MaterializationError("source_bundle_directory_invalid")
    rows: list[tuple[str, str, int, str]] = []
    file_count = 0
    total_bytes = 0
    def _walk_error(exc: OSError) -> None:
        raise MaterializationError("source_tree_walk_failed") from exc

    for current, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=_walk_error,
    ):
        current_path = Path(current)
        directory_names[:] = sorted(directory_names)
        for name in directory_names:
            child = current_path / name
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise MaterializationError(
                    f"source_tree_entry_changed:{name}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise MaterializationError(f"source_tree_entry_invalid:{name}")
            rows.append(
                (
                    "directory",
                    child.relative_to(root).as_posix(),
                    0,
                    "",
                )
            )
        for name in sorted(file_names):
            child = current_path / name
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise MaterializationError(
                    f"source_tree_entry_changed:{name}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise MaterializationError(f"source_tree_entry_invalid:{name}")
            file_count += 1
            total_bytes += int(metadata.st_size)
            if file_count > MAX_SOURCE_FILES or total_bytes > MAX_SOURCE_BYTES:
                raise MaterializationError("source_bundle_limits_exceeded")
            try:
                file_sha256 = _sha256_file(child)
            except (MaterializationError, OSError) as exc:
                raise MaterializationError(
                    f"source_tree_entry_changed:{name}"
                ) from exc
            rows.append(
                (
                    "file",
                    child.relative_to(root).as_posix(),
                    int(metadata.st_size),
                    file_sha256,
                )
            )
    identity = _sha256_bytes(_canonical_json_bytes(sorted(rows)))
    try:
        after_root_metadata = root.lstat()
    except OSError as exc:
        raise MaterializationError("source_bundle_changed_during_snapshot") from exc
    if (
        stat.S_ISLNK(after_root_metadata.st_mode)
        or not stat.S_ISDIR(after_root_metadata.st_mode)
        or (root_metadata.st_dev, root_metadata.st_ino)
        != (after_root_metadata.st_dev, after_root_metadata.st_ino)
    ):
        raise MaterializationError("source_bundle_changed_during_snapshot")
    return file_count, total_bytes, identity


def _directory_identity(path: Path, *, label: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MaterializationError(f"{label}_missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MaterializationError(f"{label}_directory_invalid")
    return int(metadata.st_dev), int(metadata.st_ino)


def _assert_tree_identity(
    path: Path,
    *,
    directory_identity: tuple[int, int],
    file_count: int,
    total_bytes: int,
    tree_sha256: str,
    reason: str,
) -> None:
    try:
        current_directory_identity = _directory_identity(
            path,
            label="source_bundle",
        )
        current_snapshot = _regular_tree_snapshot(path)
    except MaterializationError as exc:
        raise MaterializationError(reason) from exc
    if current_directory_identity != directory_identity or current_snapshot != (
        file_count,
        total_bytes,
        tree_sha256,
    ):
        raise MaterializationError(reason)


def _normalized_origin(value: object, *, label: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise MaterializationError(f"{label}_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise MaterializationError(f"{label}_invalid")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "", "", "")
    )


def _url_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _loopback_transport_required(origin: str, tested_origin: str) -> None:
    if origin == tested_origin:
        raise MaterializationError("transport_origin_must_be_loopback_replay")
    hostname = str(urllib.parse.urlsplit(origin).hostname or "").strip().lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise MaterializationError("transport_origin_not_loopback") from exc
    if not address.is_loopback:
        raise MaterializationError("transport_origin_not_loopback")


@dataclass(frozen=True)
class PreparedCandidate:
    source_bundle: Path
    public_root: Path
    bundle_dir: Path
    slug: str
    payload: dict[str, object]
    core_manifest_sha256: str
    scene_ids: tuple[str, ...]
    hotspot_edges: tuple[str, ...]
    hotspot_count: int
    spatial_room_count: int
    total_panorama_bytes: int
    largest_panorama_bytes: int
    bundle_material_sha256: str
    bundle_material_files: tuple[dict[str, object], ...]
    source_directory_identity: tuple[int, int]
    source_file_count: int
    source_size_bytes: int
    source_tree_sha256: str
    candidate_marker_pending_bytes: bytes
    candidate_marker_pending_sha256: str
    candidate_public_root_directory_identity: tuple[int, int]
    candidate_bundle_directory_identity: tuple[int, int]
    candidate_marker_identity: tuple[int, int]


def _scene_rows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, Mapping):
        return []
    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, Mapping):
        return [dict(value) for value in raw_scenes.values() if isinstance(value, Mapping)]
    if isinstance(raw_scenes, list):
        return [dict(value) for value in raw_scenes if isinstance(value, Mapping)]
    return []


def _hotspot_edges(payload: Mapping[str, object]) -> tuple[str, ...]:
    scene_rows = _scene_rows(payload)
    scene_ids = {
        str(scene.get("id") or scene.get("scene_id") or "").strip()
        for scene in scene_rows
    }
    edges: set[str] = set()
    for scene in scene_rows:
        scene_id = str(scene.get("id") or scene.get("scene_id") or "").strip()
        for hotspot in scene.get("hotspots") or []:
            if not isinstance(hotspot, Mapping):
                continue
            target = str(
                hotspot.get("target_scene_id")
                or hotspot.get("target")
                or hotspot.get("target_scene")
                or ""
            ).strip()
            if target in scene_ids and target != scene_id:
                edges.add(f"{scene_id}->{target}")
    return tuple(sorted(edges))


def _candidate_manifest_pending(payload: dict[str, object]) -> dict[str, object]:
    candidate = json.loads(_canonical_json_bytes(payload))
    walkable_scene = candidate.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        raise MaterializationError("walkable_scene_missing")
    acceptance = walkable_scene.get("acceptance")
    if not isinstance(acceptance, dict):
        raise MaterializationError("acceptance_missing")
    acceptance["proof_status"] = "pending"
    for key in (
        "browser_receipt_relpath",
        "browser_receipt_sha256",
        "core_manifest_sha256",
    ):
        acceptance.pop(key, None)
    return candidate


def _bundle_material_file_rows(
    *,
    bundle_dir: Path,
    payload: Mapping[str, object],
) -> list[dict[str, object]]:
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, Mapping):
        raise MaterializationError("walkable_scene_missing")
    acceptance = walkable_scene.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise MaterializationError("acceptance_missing")
    material_relpaths: set[str] = set()
    for scene in _scene_rows(payload):
        for key in (
            "asset_relpath",
            "panorama_relpath",
            "equirect_relpath",
            "image_relpath",
        ):
            relpath = str(scene.get(key) or "").strip()
            if relpath:
                material_relpaths.add(relpath)
                break
    for relpath in (
        str(walkable_scene.get("floorplan_relpath") or "").strip(),
        str(walkable_scene.get("derived_floorplan_relpath") or "").strip(),
        str(acceptance.get("provenance_relpath") or "").strip(),
    ):
        if relpath:
            material_relpaths.add(relpath)
    rows: list[dict[str, object]] = []
    for relpath in sorted(material_relpaths):
        path = _confined_path(bundle_dir, relpath)
        rows.append(
            {
                "relpath": relpath,
                "sha256": _sha256_file(path),
                "size_bytes": int(path.stat(follow_symlinks=False).st_size),
            }
        )
    return rows


def _bundle_material_sha256(
    *,
    core_manifest_sha256: str,
    material_files: list[dict[str, object]],
) -> str:
    return _sha256_bytes(
        _canonical_json_bytes(
            {
                "core_manifest_sha256": core_manifest_sha256,
                "files": material_files,
            }
        )
    )


def _prepare_candidate_copy(
    *,
    source_bundle: Path,
    candidate_public_root: Path,
    expected_slug: str = "",
    expected_core_manifest_sha256: str = "",
    expected_source_tree_sha256: str = "",
    expected_bundle_material_sha256: str = "",
    receipt_out: Path | None = None,
) -> PreparedCandidate:
    source_bundle, candidate_public_root, receipt_out = _resolved_materialization_paths(
        source_bundle=source_bundle,
        candidate_public_root=candidate_public_root,
        receipt_out=receipt_out,
    )
    for value, reason in (
        (expected_core_manifest_sha256, "expected_core_manifest_sha256_invalid"),
        (expected_source_tree_sha256, "expected_source_tree_sha256_invalid"),
        (expected_bundle_material_sha256, "expected_bundle_material_sha256_invalid"),
    ):
        if value and DIGEST_PATTERN.fullmatch(value) is None:
            raise MaterializationError(reason)
    source_directory_identity = _directory_identity(
        source_bundle,
        label="source_bundle",
    )
    source_file_count, source_bytes, source_tree_sha256 = _regular_tree_snapshot(
        source_bundle
    )
    if expected_source_tree_sha256 and source_tree_sha256 != expected_source_tree_sha256:
        raise MaterializationError("source_tree_sha256_mismatch")
    source_manifest_path = source_bundle / "tour.json"
    source_payload = _load_json_object(source_manifest_path)
    slug = str(source_payload.get("slug") or "").strip()
    if SLUG_PATTERN.fullmatch(slug) is None:
        raise MaterializationError("tour_slug_invalid")
    if expected_slug and slug != expected_slug:
        raise MaterializationError("tour_slug_mismatch")
    pending_payload = _candidate_manifest_pending(source_payload)
    original_core_manifest_sha256 = (
        property_tour_hosting._hosted_property_tour_ai_panorama_core_manifest_sha256(
            source_payload
        )
    )
    pending_core_manifest_sha256 = (
        property_tour_hosting._hosted_property_tour_ai_panorama_core_manifest_sha256(
            pending_payload
        )
    )
    if (
        DIGEST_PATTERN.fullmatch(original_core_manifest_sha256) is None
        or pending_core_manifest_sha256 != original_core_manifest_sha256
    ):
        raise MaterializationError("core_manifest_identity_invalid")
    if (
        expected_core_manifest_sha256
        and original_core_manifest_sha256 != expected_core_manifest_sha256
    ):
        raise MaterializationError("core_manifest_sha256_mismatch")
    source_preflight = property_tour_hosting._hosted_property_tour_ai_panorama_contract(
        bundle_dir=source_bundle,
        payload=pending_payload,
        mode="preflight",
    )
    if source_preflight.get("preflight_ready") is not True:
        raise MaterializationError(
            f"source_preflight_failed:{source_preflight.get('reason') or 'unknown'}"
        )
    if source_preflight.get("core_manifest_sha256") != original_core_manifest_sha256:
        raise MaterializationError("source_preflight_core_mismatch")
    material_files = _bundle_material_file_rows(
        bundle_dir=source_bundle,
        payload=pending_payload,
    )
    bundle_material_sha256 = _bundle_material_sha256(
        core_manifest_sha256=original_core_manifest_sha256,
        material_files=material_files,
    )
    if (
        expected_bundle_material_sha256
        and bundle_material_sha256 != expected_bundle_material_sha256
    ):
        raise MaterializationError("bundle_material_sha256_mismatch")
    _assert_tree_identity(
        source_bundle,
        directory_identity=source_directory_identity,
        file_count=source_file_count,
        total_bytes=source_bytes,
        tree_sha256=source_tree_sha256,
        reason="source_bundle_mutated_before_copy",
    )
    rechecked_source, rechecked_candidate, rechecked_receipt = (
        _resolved_materialization_paths(
        source_bundle=source_bundle,
        candidate_public_root=candidate_public_root,
            receipt_out=receipt_out,
        )
    )
    if (
        rechecked_source != source_bundle
        or rechecked_candidate != candidate_public_root
        or rechecked_receipt != receipt_out
    ):
        raise MaterializationError("materialization_path_identity_changed")
    if candidate_public_root.exists():
        metadata = candidate_public_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MaterializationError("candidate_root_invalid")
        if any(candidate_public_root.iterdir()):
            raise MaterializationError("candidate_root_not_empty")
        created_root = False
    else:
        try:
            parent_metadata = candidate_public_root.parent.lstat()
        except OSError as exc:
            raise MaterializationError("candidate_root_parent_missing") from exc
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
        ):
            raise MaterializationError("candidate_root_parent_invalid")
        candidate_public_root.mkdir(mode=0o700)
        created_root = True
    try:
        candidate_root_identity = _directory_identity(
            candidate_public_root,
            label="candidate_root",
        )
    except Exception:
        if created_root and candidate_public_root.exists() and not any(
            candidate_public_root.iterdir()
        ):
            candidate_public_root.rmdir()
        raise
    final_bundle_dir = candidate_public_root / slug
    walkable_scene = pending_payload.get("walkable_scene")
    assert isinstance(walkable_scene, dict)
    marker = {
        "contract_name": CANDIDATE_MARKER_CONTRACT,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "slug": slug,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": source_file_count,
        "source_size_bytes": source_bytes,
        "core_manifest_sha256": original_core_manifest_sha256,
        "bundle_material_sha256": bundle_material_sha256,
    }
    marker_bytes = _canonical_json_bytes(marker)
    marker_sha256 = _sha256_bytes(marker_bytes)
    partial_parent: Path | None = None
    partial_identity: tuple[int, int] | None = None
    final_identity: tuple[int, int] | None = None
    marker_identity: tuple[int, int] | None = None
    marker_path = candidate_public_root / CANDIDATE_MARKER_RELPATH
    try:
        if final_bundle_dir.exists() or final_bundle_dir.is_symlink():
            raise MaterializationError("candidate_bundle_exists")
        partial_parent = Path(
            tempfile.mkdtemp(prefix=f".{slug}.partial-", dir=candidate_public_root)
        )
        partial_identity = _directory_identity(
            partial_parent,
            label="candidate_partial",
        )
        bundle_dir = partial_parent / "bundle"
        shutil.copytree(
            source_bundle,
            bundle_dir,
            copy_function=shutil.copy2,
            symlinks=True,
        )
        copied_file_count, copied_bytes, copied_tree_sha256 = _regular_tree_snapshot(
            bundle_dir
        )
        if (copied_file_count, copied_bytes, copied_tree_sha256) != (
            source_file_count,
            source_bytes,
            source_tree_sha256,
        ):
            raise MaterializationError("candidate_copy_identity_mismatch")
        _assert_tree_identity(
            source_bundle,
            directory_identity=source_directory_identity,
            file_count=source_file_count,
            total_bytes=source_bytes,
            tree_sha256=source_tree_sha256,
            reason="source_bundle_mutated_during_copy",
        )
        copied_payload = _load_json_object(bundle_dir / "tour.json")
        if copied_payload != source_payload:
            raise MaterializationError("candidate_manifest_copy_mismatch")
        _atomic_write(bundle_dir / "tour.json", _canonical_json_bytes(pending_payload))
        for relpath in (
            BROWSER_RECEIPT_RELPATH,
            *SCREENSHOT_RELPATHS.values(),
        ):
            candidate_path = _confined_path(bundle_dir, relpath)
            if candidate_path.is_symlink():
                raise MaterializationError("candidate_proof_path_symlink")
            candidate_path.unlink(missing_ok=True)
        candidate_preflight = (
            property_tour_hosting._hosted_property_tour_ai_panorama_contract(
                bundle_dir=bundle_dir,
                payload=pending_payload,
                mode="preflight",
            )
        )
        if candidate_preflight != source_preflight:
            raise MaterializationError("candidate_preflight_identity_mismatch")
        copied_material_files = _bundle_material_file_rows(
            bundle_dir=bundle_dir,
            payload=pending_payload,
        )
        if copied_material_files != material_files:
            raise MaterializationError("candidate_material_identity_mismatch")
        final_identity = _directory_identity(bundle_dir, label="candidate_bundle")
        marker_identity = _atomic_create(
            marker_path,
            marker_bytes,
            label="candidate_marker",
        )
        os.replace(bundle_dir, final_bundle_dir)
        partial_parent.rmdir()
        partial_parent = None
        partial_identity = None
        root_descriptor = os.open(
            candidate_public_root,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        if _directory_identity(candidate_public_root, label="candidate_root") != candidate_root_identity:
            raise MaterializationError("candidate_root_identity_changed")
        bundle_dir = final_bundle_dir
        if final_identity is None or marker_identity is None:
            raise MaterializationError("candidate_identity_unbound")
        spatial_model = walkable_scene.get("spatial_model")
        spatial_rooms = (
            spatial_model.get("rooms")
            if isinstance(spatial_model, Mapping)
            and isinstance(spatial_model.get("rooms"), list)
            else []
        )
        return PreparedCandidate(
            source_bundle=source_bundle,
            public_root=candidate_public_root,
            bundle_dir=bundle_dir,
            slug=slug,
            payload=pending_payload,
            core_manifest_sha256=original_core_manifest_sha256,
            scene_ids=tuple(
                str(value) for value in source_preflight.get("scene_ids") or []
            ),
            hotspot_edges=_hotspot_edges(pending_payload),
            hotspot_count=int(source_preflight.get("hotspot_count") or 0),
            spatial_room_count=len(spatial_rooms),
            total_panorama_bytes=int(
                source_preflight.get("total_panorama_bytes") or 0
            ),
            largest_panorama_bytes=int(
                source_preflight.get("largest_panorama_bytes") or 0
            ),
            bundle_material_sha256=bundle_material_sha256,
            bundle_material_files=tuple(material_files),
            source_directory_identity=source_directory_identity,
            source_file_count=source_file_count,
            source_size_bytes=source_bytes,
            source_tree_sha256=source_tree_sha256,
            candidate_marker_pending_bytes=marker_bytes,
            candidate_marker_pending_sha256=marker_sha256,
            candidate_public_root_directory_identity=candidate_root_identity,
            candidate_bundle_directory_identity=final_identity,
            candidate_marker_identity=marker_identity,
        )
    except Exception:
        cleanup_error: Exception | None = None
        try:
            if final_identity is not None:
                _remove_owned_directory(
                    final_bundle_dir,
                    parent=candidate_public_root,
                    identity=final_identity,
                )
            if partial_parent is not None and partial_identity is not None:
                _remove_owned_directory(
                    partial_parent,
                    parent=candidate_public_root,
                    identity=partial_identity,
                )
            if marker_identity is not None:
                _unlink_created_file(marker_path, marker_identity)
            if created_root:
                if (
                    _directory_identity(candidate_public_root, label="candidate_root")
                    != candidate_root_identity
                    or any(candidate_public_root.iterdir())
                ):
                    raise MaterializationError("candidate_root_cleanup_unsafe")
                candidate_public_root.rmdir()
        except Exception as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise MaterializationError("candidate_cleanup_failed") from cleanup_error
        raise


def _viewer_implementation_projection_sha256(body: bytes) -> str:
    if not body or len(body) > 2 * MAX_JSON_BYTES:
        raise MaterializationError("viewer_implementation_body_invalid")
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterializationError("viewer_implementation_body_invalid") from exc
    styles = re.findall(r"<style\b[^>]*>(.*?)</style>", document, flags=re.I | re.S)
    modules = re.findall(
        r"<script\b(?=[^>]*\btype=[\"']module[\"'])[^>]*>(.*?)</script>",
        document,
        flags=re.I | re.S,
    )
    if len(styles) != 1 or len(modules) != 1:
        raise MaterializationError("viewer_implementation_projection_invalid")
    return _sha256_bytes(
        _canonical_json_bytes(
            {
                "module_script": modules[0],
                "style": styles[0],
            }
        )
    )


@dataclass
class BrowserAudit:
    tested_origin: str
    transport_origin: str
    allowed_urls: frozenset[str]
    expected_assets: Mapping[str, tuple[str, int]]
    control_url: str
    expected_viewer_implementation_sha256: str
    slow_network: bool = False
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    bad_responses: list[dict[str, object]] = field(default_factory=list)
    external_script_requests: list[str] = field(default_factory=list)
    renderer_status: int = 0
    renderer_sha256: str = ""
    viewer_implementation_sha256: str = ""
    immutable_asset_digests: set[str] = field(default_factory=set)

    def install(self, context: object) -> None:
        context.route("**/*", self._handle_route)

    def watch(self, page: object) -> None:
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: self.console_errors.append(str(message.text))
            if str(message.type) == "error"
            else None,
        )
        page.on(
            "requestfailed",
            lambda request: self.failed_requests.append(
                f"{request.method} {request.url}: {request.failure or 'failed'}"
            ),
        )

    def _handle_route(self, route: object) -> None:
        request = route.request
        request_url = str(request.url)
        parsed = urllib.parse.urlsplit(request_url)
        if parsed.scheme in {"data", "blob"}:
            route.continue_()
            return
        if parsed.scheme not in {"http", "https"}:
            self.failed_requests.append(
                f"{request.method} {request_url}: blocked_scheme"
            )
            route.abort("blockedbyclient")
            return
        normalized_request_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )
        if _url_origin(normalized_request_url) != self.tested_origin:
            if str(request.resource_type) == "script":
                self.external_script_requests.append(request_url)
            self.failed_requests.append(
                f"{request.method} {request_url}: blocked_origin"
            )
            route.abort("blockedbyclient")
            return
        if normalized_request_url not in self.allowed_urls:
            self.failed_requests.append(
                f"{request.method} {request_url}: blocked_unapproved_url"
            )
            route.abort("blockedbyclient")
            return
        expected_asset = self.expected_assets.get(normalized_request_url)
        if expected_asset is not None and (
            expected_asset[1] <= 0 or expected_asset[1] > MAX_BROWSER_ASSET_BYTES
        ):
            self.bad_responses.append(
                {
                    "status": 0,
                    "url": request_url,
                    "resource_type": request.resource_type,
                    "reason": "asset_expected_size_invalid",
                }
            )
            route.abort("blockedbyclient")
            return
        target_url = urllib.parse.urlunsplit(
            (
                urllib.parse.urlsplit(self.transport_origin).scheme,
                urllib.parse.urlsplit(self.transport_origin).netloc,
                parsed.path,
                parsed.query,
                "",
            )
        )
        try:
            response = route.fetch(
                url=target_url,
                timeout=120_000,
                max_redirects=0,
            )
            body = response.body()
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            status = int(response.status)
            response_url = str(getattr(response, "url", target_url))
            if response_url != target_url:
                self.bad_responses.append(
                    {
                        "status": status,
                        "url": request_url,
                        "resource_type": request.resource_type,
                        "reason": "transport_response_url_changed",
                    }
                )
                route.abort("blockedbyclient")
                return
            if 300 <= status < 400:
                self.bad_responses.append(
                    {
                        "status": status,
                        "url": request_url,
                        "resource_type": request.resource_type,
                        "reason": "redirect_blocked",
                        "location": str(headers.get("location") or ""),
                    }
                )
                route.abort("blockedbyclient")
                return
            if normalized_request_url == self.control_url:
                try:
                    self.viewer_implementation_sha256 = (
                        _viewer_implementation_projection_sha256(body)
                    )
                except MaterializationError as exc:
                    self.bad_responses.append(
                        {
                            "status": status,
                            "url": request_url,
                            "resource_type": request.resource_type,
                            "reason": str(exc),
                        }
                    )
                    route.abort("blockedbyclient")
                    return
                if (
                    self.viewer_implementation_sha256
                    != self.expected_viewer_implementation_sha256
                ):
                    self.bad_responses.append(
                        {
                            "status": status,
                            "url": request_url,
                            "resource_type": request.resource_type,
                            "reason": "viewer_implementation_binding_invalid",
                        }
                    )
                    route.abort("blockedbyclient")
                    return
            if expected_asset is not None:
                expected_digest, expected_size = expected_asset
                body_digest = _sha256_bytes(body)
                if (
                    expected_size <= 0
                    or expected_size > MAX_BROWSER_ASSET_BYTES
                    or len(body) != expected_size
                    or body_digest != expected_digest
                ):
                    self.bad_responses.append(
                        {
                            "status": status,
                            "url": request_url,
                            "resource_type": request.resource_type,
                            "reason": "asset_body_binding_invalid",
                            "expected_size": expected_size,
                            "observed_size": len(body),
                            "expected_sha256": expected_digest,
                            "observed_sha256": body_digest,
                        }
                    )
                    route.abort("blockedbyclient")
                    return
            if status >= 400:
                self.bad_responses.append(
                    {"status": status, "url": request_url, "resource_type": request.resource_type}
                )
            if parsed.path == RENDERER_MODULE_PATH:
                self.renderer_status = status
                self.renderer_sha256 = _sha256_bytes(body)
            cache_control = str(headers.get("cache-control") or "").lower()
            asset_digest = str(headers.get("x-propertyquarry-asset-sha256") or "").lower()
            if (
                expected_asset is not None
                and asset_digest == expected_asset[0]
                and "public" in cache_control
                and "max-age=31536000" in cache_control
                and "immutable" in cache_control
            ):
                self.immutable_asset_digests.add(_sha256_bytes(body))
            if self.slow_network:
                time.sleep(
                    SLOW_NETWORK_LATENCY_SECONDS
                    + min(len(body) / SLOW_NETWORK_BYTES_PER_SECOND, 10.0)
                )
            route.fulfill(status=status, headers=headers, body=body)
        except Exception as exc:
            self.failed_requests.append(
                f"{request.method} {request_url}: replay:{type(exc).__name__}:{exc}"
            )
            route.abort("failed")


def _assert_audit_clean(audit: BrowserAudit, *, label: str) -> None:
    if audit.page_errors:
        raise MaterializationError(f"{label}_page_errors:{audit.page_errors[:3]}")
    if audit.console_errors:
        raise MaterializationError(f"{label}_console_errors:{audit.console_errors[:3]}")
    if audit.failed_requests:
        raise MaterializationError(f"{label}_failed_requests:{audit.failed_requests[:3]}")
    if audit.bad_responses:
        raise MaterializationError(f"{label}_bad_responses:{audit.bad_responses[:3]}")
    if audit.external_script_requests:
        raise MaterializationError(
            f"{label}_external_scripts:{audit.external_script_requests[:3]}"
        )


def _wait_for_panorama(page: object, *, timeout_ms: int) -> None:
    page.locator("#viewer canvas").wait_for(state="visible", timeout=timeout_ms)
    page.locator("#status").wait_for(state="hidden", timeout=timeout_ms)
    page.wait_for_function(
        "() => document.body.dataset.viewer === 'propertyquarry-ai-panorama'",
        timeout=timeout_ms,
    )


def _wait_for_scene(page: object, scene_id: str, *, timeout_ms: int) -> None:
    page.wait_for_function(
        "sceneId => document.querySelector(`.scene-button[data-scene-id=\"${CSS.escape(sceneId)}\"]`)?.classList.contains('active') === true",
        arg=scene_id,
        timeout=timeout_ms,
    )
    page.locator("#status").wait_for(state="hidden", timeout=timeout_ms)


def _canvas_size(page: object) -> str:
    value = page.locator("#viewer canvas").evaluate(
        "canvas => `${canvas.width}x${canvas.height}`"
    )
    return str(value)


def _assert_csp(headers: Mapping[str, str]) -> None:
    raw_csp = str(headers.get("content-security-policy") or "").strip()
    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in raw_csp.split(";"):
        tokens = raw_directive.strip().split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in directives:
            raise MaterializationError("ai_panorama_csp_duplicate_directive")
        directives[name] = tuple(tokens[1:])

    def _exact(name: str, expected: set[str]) -> bool:
        values = directives.get(name)
        return values is not None and {value.lower() for value in values} == expected

    def _self_nonce_or_hash(name: str, *, require_nonce: bool) -> bool:
        values = directives.get(name)
        if not values or "'self'" not in {value.lower() for value in values}:
            return False
        nonce_found = False
        for value in values:
            lowered = value.lower()
            if lowered == "'self'":
                continue
            if re.fullmatch(r"'nonce-[A-Za-z0-9+/_=-]+'", value):
                nonce_found = True
                continue
            if re.fullmatch(r"'sha(256|384|512)-[A-Za-z0-9+/=]+'", value):
                continue
            return False
        return nonce_found or not require_nonce

    valid = (
        _exact("default-src", {"'none'"})
        and _exact("object-src", {"'none'"})
        and _exact("frame-src", {"'none'"})
        and _exact("script-src-attr", {"'none'"})
        and _exact("connect-src", {"'self'"})
        and _exact("img-src", {"'self'", "data:", "blob:"})
        and _self_nonce_or_hash("script-src", require_nonce=True)
    )
    if not valid:
        raise MaterializationError("ai_panorama_csp_invalid")


def _scene_metadata(candidate: PreparedCandidate) -> dict[str, dict[str, object]]:
    return {
        str(scene.get("id") or scene.get("scene_id") or "").strip(): scene
        for scene in _scene_rows(candidate.payload)
    }


def _expected_browser_spec(candidate: PreparedCandidate) -> dict[str, object]:
    payload = json.loads(_canonical_json_bytes(candidate.payload))
    payload["_ai_panorama_asset_sha256"] = {
        str(row["relpath"]): str(row["sha256"])
        for row in candidate.bundle_material_files
    }
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        raise MaterializationError("walkable_scene_missing")
    spec = public_tours._tour_control_panorama_spec(payload, walkable_scene)
    if not spec:
        raise MaterializationError("expected_browser_spec_missing")
    return spec


def _expected_viewer_implementation_sha256(candidate: PreparedCandidate) -> str:
    html_document = public_tours._tour_control_panorama_html(
        candidate.payload,
        panorama_spec=_expected_browser_spec(candidate),
        provider_label="PropertyQuarry AI 360",
        viewer_name="ai-panorama",
        nonce="propertyquarry-proof-nonce",
    )
    return _viewer_implementation_projection_sha256(html_document.encode("utf-8"))


def _browser_allowed_urls(
    *,
    candidate: PreparedCandidate,
    tested_origin: str,
    tested_url: str,
) -> frozenset[str]:
    spec = _expected_browser_spec(candidate)
    relative_urls = [
        RENDERER_MODULE_PATH,
        str(spec.get("floorplan_url") or ""),
        str(spec.get("derived_floorplan_url") or ""),
    ]
    for scene in spec.get("scenes") or []:
        if isinstance(scene, Mapping):
            relative_urls.append(str(scene.get("image_url") or ""))
    allowed = {tested_url}
    for relative_url in relative_urls:
        parsed = urllib.parse.urlsplit(relative_url)
        if (
            not relative_url
            or parsed.scheme
            or parsed.netloc
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            raise MaterializationError("browser_resource_url_invalid")
        absolute_url = urllib.parse.urljoin(f"{tested_origin}/", relative_url)
        if _url_origin(absolute_url) != tested_origin:
            raise MaterializationError("browser_resource_origin_invalid")
        allowed.add(absolute_url)
    return frozenset(allowed)


def _browser_expected_assets(
    *,
    candidate: PreparedCandidate,
    tested_origin: str,
) -> dict[str, tuple[str, int]]:
    material_by_relpath = {
        str(row["relpath"]): (str(row["sha256"]), int(row["size_bytes"]))
        for row in candidate.bundle_material_files
    }
    spec = _expected_browser_spec(candidate)
    spec_scenes = spec.get("scenes")
    source_scenes = _scene_rows(candidate.payload)
    if not isinstance(spec_scenes, list) or len(spec_scenes) != len(source_scenes):
        raise MaterializationError("browser_asset_scene_binding_invalid")
    expected: dict[str, tuple[str, int]] = {}
    for source_scene, browser_scene in zip(source_scenes, spec_scenes, strict=True):
        if not isinstance(browser_scene, Mapping):
            raise MaterializationError("browser_asset_scene_binding_invalid")
        relpath = ""
        for key in (
            "asset_relpath",
            "panorama_relpath",
            "equirect_relpath",
            "image_relpath",
        ):
            relpath = str(source_scene.get(key) or "").strip()
            if relpath:
                break
        binding = material_by_relpath.get(relpath)
        image_url = str(browser_scene.get("image_url") or "")
        if binding is None or not image_url:
            raise MaterializationError("browser_asset_scene_binding_invalid")
        expected[urllib.parse.urljoin(f"{tested_origin}/", image_url)] = binding
    walkable_scene = candidate.payload.get("walkable_scene")
    floorplan_relpath = (
        str(walkable_scene.get("floorplan_relpath") or "").strip()
        if isinstance(walkable_scene, Mapping)
        else ""
    )
    floorplan_url = str(spec.get("floorplan_url") or "")
    floorplan_binding = material_by_relpath.get(floorplan_relpath)
    if floorplan_binding is None or not floorplan_url:
        raise MaterializationError("browser_floorplan_asset_binding_invalid")
    expected[urllib.parse.urljoin(f"{tested_origin}/", floorplan_url)] = (
        floorplan_binding
    )
    derived_floorplan_relpath = (
        str(walkable_scene.get("derived_floorplan_relpath") or "").strip()
        if isinstance(walkable_scene, Mapping)
        else ""
    )
    derived_floorplan_url = str(spec.get("derived_floorplan_url") or "")
    derived_floorplan_binding = material_by_relpath.get(derived_floorplan_relpath)
    if derived_floorplan_relpath and derived_floorplan_binding and derived_floorplan_url:
        expected[urllib.parse.urljoin(f"{tested_origin}/", derived_floorplan_url)] = (
            derived_floorplan_binding
        )
    return expected


def _assert_browser_spec_binding(
    candidate: PreparedCandidate,
    observed_spec: object,
) -> str:
    if not isinstance(observed_spec, dict):
        raise MaterializationError("desktop_panorama_spec_invalid")
    expected_spec = _expected_browser_spec(candidate)
    if observed_spec != expected_spec:
        raise MaterializationError("desktop_panorama_spec_binding_mismatch")
    return _sha256_bytes(_canonical_json_bytes(observed_spec))


def _normalized_angle_radians(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


def _dollhouse_floor_screen_point(
    *,
    rooms: list[Mapping[str, object]],
    target_room: Mapping[str, object],
    viewport_width: float,
    viewport_height: float,
) -> tuple[float, float]:
    min_x = min(float(room.get("x") or 0.0) for room in rooms)
    min_z = min(float(room.get("z") or 0.0) for room in rooms)
    max_x = max(
        float(room.get("x") or 0.0) + float(room.get("width") or 0.0)
        for room in rooms
    )
    max_z = max(
        float(room.get("z") or 0.0) + float(room.get("depth") or 0.0)
        for room in rooms
    )
    look_at = ((min_x + max_x) / 2.0, 0.3, (min_z + max_z) / 2.0)
    distance = max(13.0, math.hypot(max_x - min_x, max_z - min_z) * 1.22)
    # Keep the proof projection in lockstep with the viewer's default
    # plan-aligned dollhouse camera so the raycast check hits measured rooms.
    azimuth = 0.0
    elevation = 1.08
    horizontal = math.cos(elevation) * distance
    camera = (
        look_at[0] + math.sin(azimuth) * horizontal,
        look_at[1] + math.sin(elevation) * distance,
        look_at[2] + math.cos(azimuth) * horizontal,
    )

    def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
        length = math.sqrt(sum(value * value for value in vector))
        if length <= 0:
            raise MaterializationError("dollhouse_projection_invalid")
        return (
            vector[0] / length,
            vector[1] / length,
            vector[2] / length,
        )

    def _cross(
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        return (
            left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0],
        )

    forward = _normalize(tuple(look_at[index] - camera[index] for index in range(3)))
    right = _normalize(_cross(forward, (0.0, 1.0, 0.0)))
    upward = _normalize(_cross(right, forward))
    raw_measurement = target_room.get("measurement")
    raw_components = (
        raw_measurement.get("components")
        if isinstance(raw_measurement, Mapping)
        else None
    )
    if isinstance(raw_components, list) and raw_components:
        component = max(
            (row for row in raw_components if isinstance(row, Mapping)),
            key=lambda row: float(row.get("width") or 0.0) * float(row.get("depth") or 0.0),
            default=None,
        )
    else:
        component = None
    footprint = component or target_room
    world = (
        float(footprint.get("x") or 0.0)
        + float(footprint.get("width") or 0.0) / 2.0,
        0.1,
        float(footprint.get("z") or 0.0)
        + float(footprint.get("depth") or 0.0) / 2.0,
    )
    relative = tuple(world[index] - camera[index] for index in range(3))
    camera_x = sum(relative[index] * right[index] for index in range(3))
    camera_y = sum(relative[index] * upward[index] for index in range(3))
    camera_depth = sum(relative[index] * forward[index] for index in range(3))
    if camera_depth <= 0:
        raise MaterializationError("dollhouse_projection_behind_camera")
    tangent = math.tan(math.radians(38.0) / 2.0)
    aspect = viewport_width / viewport_height
    ndc_x = camera_x / (camera_depth * tangent * aspect)
    ndc_y = camera_y / (camera_depth * tangent)
    screen_x = (ndc_x + 1.0) * 0.5 * viewport_width
    screen_y = (1.0 - ndc_y) * 0.5 * viewport_height
    if not (1.0 <= screen_x < viewport_width - 1.0 and 1.0 <= screen_y < viewport_height - 1.0):
        raise MaterializationError("dollhouse_projection_outside_viewport")
    return screen_x, screen_y


def _align_hotspot_with_keyboard(
    page: object,
    *,
    scene: Mapping[str, object],
    hotspot: Mapping[str, object],
) -> None:
    start_yaw = math.radians(float(scene.get("start_yaw") or 0.0))
    hotspot_yaw = math.radians(float(hotspot.get("yaw") or 0.0))
    delta = _normalized_angle_radians(hotspot_yaw - start_yaw)
    key = "ArrowLeft" if delta >= 0 else "ArrowRight"
    page.locator("#viewer").focus()
    for _ in range(int(round(abs(delta) / 0.08))):
        page.keyboard.press(key)
    page.wait_for_timeout(100)


def _assert_viewer_layout(
    page: object,
    *,
    require_expanded_floorplan: bool,
) -> None:
    result = page.evaluate(
        """requireExpandedFloorplan => {
          const visible = node => {
            if (!node || node.hidden) return false;
            const style = getComputedStyle(node);
            const box = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0 && box.width > 0 && box.height > 0;
          };
          const rect = node => {
            const box = node.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
              width: box.width, height: box.height};
          };
          const intersects = (left, right, gap = 0) =>
            left.left < right.right + gap && left.right > right.left - gap
            && left.top < right.bottom + gap && left.bottom > right.top - gap;
          const failures = [];
          const protectedSelectors = {
            identity: '.identity',
            top_actions: '.top-actions',
            zoom_controls: '.zoom-controls',
            scene_rail: '.scene-rail',
            floorplan: '#floorplan:not(.collapsed)',
          };
          const protectedRows = [];
          for (const [name, selector] of Object.entries(protectedSelectors)) {
            const node = document.querySelector(selector);
            if (!visible(node)) {
              if (name !== 'floorplan' || requireExpandedFloorplan) failures.push(`${name}:not_visible`);
              continue;
            }
            const box = rect(node);
            if (box.left < 0 || box.top < 0 || box.right > innerWidth || box.bottom > innerHeight) {
              failures.push(`${name}:outside_viewport`);
            }
            protectedRows.push({name, box});
          }
          const floorplanVisible = protectedRows.some(row => row.name === 'floorplan');
          if (floorplanVisible !== requireExpandedFloorplan) failures.push('floorplan:state_mismatch');
          for (let index = 0; index < protectedRows.length; index += 1) {
            for (const other of protectedRows.slice(index + 1)) {
              if (intersects(protectedRows[index].box, other.box, 0)) {
                failures.push(`protected_collision:${protectedRows[index].name}:${other.name}`);
              }
            }
          }
          const buttons = Array.from(document.querySelectorAll(
            '#dollhouse-toggle,#map-toggle,#fullscreen,#zoom-in,#zoom-out,.scene-button,.floorplan-pin'
          )).filter(visible);
          for (const button of buttons) {
            const box = rect(button);
            const name = (button.getAttribute('aria-label') || button.getAttribute('title') || button.textContent || '').trim();
            if (button.tagName !== 'BUTTON' || !name) failures.push('control:semantics_missing');
            if (!button.classList.contains('floorplan-pin') && (box.width < 40 || box.height < 40)) {
              failures.push(`control:target_too_small:${button.id || button.className}`);
            }
            const isScrollableSceneButton = button.classList.contains('scene-button');
            const isFloorplanPin = button.classList.contains('floorplan-pin');
            if (!isScrollableSceneButton && !isFloorplanPin
                && (box.left < 0 || box.top < 0 || box.right > innerWidth || box.bottom > innerHeight)) {
              failures.push(`control:outside_viewport:${button.id || button.className}`);
            }
            if (isScrollableSceneButton && button.classList.contains('active')) {
              const railBox = rect(document.querySelector('.scene-rail'));
              if (box.left < railBox.left || box.right > railBox.right
                  || box.top < railBox.top || box.bottom > railBox.bottom) {
                failures.push('control:active_scene_not_fully_visible');
              }
            }
          }
          const identityStrong = document.querySelector('.identity strong');
          const identityDisclosure = document.querySelector('.identity span');
          if (!identityStrong?.textContent?.trim() || !identityDisclosure?.textContent?.trim()) {
            failures.push('identity:semantics_missing');
          }
          if (requireExpandedFloorplan) {
            const image = document.querySelector('#floorplan-image');
            if (!visible(image) || !String(image.getAttribute('alt') || '').trim()) {
              failures.push('floorplan:semantics_missing');
            }
          }
          const hotspots = Array.from(document.querySelectorAll('.hotspot')).filter(visible);
          const hotspotRows = hotspots.map(node => ({node, box: rect(node)}));
          for (let index = 0; index < hotspotRows.length; index += 1) {
            const row = hotspotRows[index];
            const target = String(row.node.dataset.targetSceneId || '').trim();
            const name = (row.node.getAttribute('aria-label') || row.node.textContent || '').trim();
            if (row.node.tagName !== 'BUTTON' || !target || !name) failures.push('hotspot:semantics_missing');
            if (row.box.width < 40 || row.box.height < 38) failures.push(`hotspot:target_too_small:${target}`);
            if (row.box.left < 8 || row.box.top < 8 || row.box.right > innerWidth - 8 || row.box.bottom > innerHeight - 8) {
              failures.push(`hotspot:outside_safe_viewport:${target}`);
            }
            for (const surface of protectedRows) {
              if (intersects(row.box, surface.box, 7)) failures.push(`hotspot_surface_collision:${target}:${surface.name}`);
            }
            for (const other of hotspotRows.slice(index + 1)) {
              if (intersects(row.box, other.box, 4)) {
                failures.push(`hotspot_collision:${target}:${String(other.node.dataset.targetSceneId || '')}`);
              }
            }
          }
          return {failures, hotspotCount: hotspots.length, floorplanVisible};
        }""",
        require_expanded_floorplan,
    )
    failures = result.get("failures") if isinstance(result, dict) else None
    if not isinstance(failures, list) or failures:
        raise MaterializationError(f"mobile_layout_invalid:{failures or result}")


def _verify_and_capture_desktop(
    *,
    browser: object,
    candidate: PreparedCandidate,
    tested_url: str,
    tested_origin: str,
    transport_origin: str,
    output_path: Path,
    timeout_ms: int,
) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 960},
        device_scale_factor=1,
        service_workers="block",
    )
    audit = BrowserAudit(
        tested_origin,
        transport_origin,
        _browser_allowed_urls(
            candidate=candidate,
            tested_origin=tested_origin,
            tested_url=tested_url,
        ),
        _browser_expected_assets(
            candidate=candidate,
            tested_origin=tested_origin,
        ),
        tested_url,
        _expected_viewer_implementation_sha256(candidate),
    )
    audit.install(context)
    page = context.new_page()
    audit.watch(page)
    try:
        started = time.monotonic()
        response = page.goto(tested_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            raise MaterializationError("desktop_control_http_not_200")
        _assert_csp(response.headers)
        _wait_for_panorama(page, timeout_ms=timeout_ms)
        initial_loaded_ms = round((time.monotonic() - started) * 1000.0, 1)
        try:
            spec = json.loads(
                page.locator("#panorama-data").text_content() or "{}",
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_nonfinite,
            )
        except (TypeError, ValueError) as exc:
            raise MaterializationError("desktop_panorama_spec_invalid") from exc
        browser_spec_sha256 = _assert_browser_spec_binding(candidate, spec)
        if page.locator(".scene-button").count() != len(candidate.scene_ids):
            raise MaterializationError("desktop_scene_rail_incomplete")
        if page.locator(".floorplan-pin").count() != len(candidate.scene_ids):
            raise MaterializationError("desktop_floorplan_pins_incomplete")
        if _candidate_representation_disclosure(candidate) not in page.locator(".identity span").inner_text():
            raise MaterializationError("desktop_disclosure_missing")
        _assert_viewer_layout(page, require_expanded_floorplan=True)

        metadata = _scene_metadata(candidate)
        drag_source = ""
        drag_target = ""
        drag_delta = 0.0
        for source_id, scene in metadata.items():
            for hotspot in scene.get("hotspots") or []:
                if not isinstance(hotspot, Mapping):
                    continue
                target = str(hotspot.get("target_scene_id") or hotspot.get("target") or "")
                delta = _normalized_angle_radians(
                    math.radians(float(hotspot.get("yaw") or 0.0))
                    - math.radians(float(scene.get("start_yaw") or 0.0))
                )
                if abs(delta) > abs(drag_delta):
                    drag_source, drag_target, drag_delta = source_id, target, delta
        if not drag_source or not drag_target:
            raise MaterializationError("desktop_drag_target_missing")
        page.locator(f'.scene-button[data-scene-id="{drag_source}"]').click()
        _wait_for_scene(page, drag_source, timeout_ms=timeout_ms)
        total_dx = -drag_delta / 0.0042
        while abs(total_dx) > 1:
            dx = max(-500.0, min(500.0, total_dx))
            page.mouse.move(720, 480)
            page.mouse.down()
            page.mouse.move(720 + dx, 480, steps=12)
            page.mouse.up()
            total_dx -= dx
        hotspot_locator = page.locator(
            f'.hotspot[data-target-scene-id="{drag_target}"]'
        )
        hotspot_locator.wait_for(state="visible", timeout=timeout_ms)
        drag_navigation_verified = hotspot_locator.is_visible()

        map_toggle = page.locator("#map-toggle")
        if map_toggle.get_attribute("aria-pressed") != "true":
            raise MaterializationError("desktop_floorplan_default_invalid")
        map_toggle.click()
        if map_toggle.get_attribute("aria-pressed") != "false":
            raise MaterializationError("desktop_map_close_failed")
        map_toggle.click()
        if map_toggle.get_attribute("aria-pressed") != "true":
            raise MaterializationError("desktop_map_open_failed")
        derived_toggle = page.locator("#floorplan-kind-toggle")
        if derived_toggle.is_visible():
            derived_toggle.click()
            page.wait_for_function(
                "() => document.querySelector('#floorplan-image')?.alt?.toLowerCase().includes('derived')",
                timeout=timeout_ms,
            )
            if "show source" not in str(derived_toggle.inner_text()).lower():
                raise MaterializationError("desktop_derived_floorplan_toggle_failed")
            derived_toggle.click()
            page.wait_for_function(
                "() => document.querySelector('#floorplan-image')?.alt?.toLowerCase().includes('original')",
                timeout=timeout_ms,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=False, animations="disabled")

        for scene_id in candidate.scene_ids:
            page.locator(f'.scene-button[data-scene-id="{scene_id}"]').click()
            _wait_for_scene(page, scene_id, timeout_ms=timeout_ms)
        dollhouse_toggle = page.locator("#dollhouse-toggle")
        if not dollhouse_toggle.is_visible():
            raise MaterializationError("dollhouse_control_missing")
        dollhouse_toggle.click()
        page.wait_for_function(
            "() => document.body.dataset.mode === 'dollhouse'",
            timeout=timeout_ms,
        )
        page.wait_for_function(
            """() => {
              const nodes = Array.from(document.querySelectorAll('.dollhouse-node'))
                .filter(node => !node.hidden && getComputedStyle(node).display !== 'none');
              return nodes.length > 0 && nodes.every(node => {
                const box = node.getBoundingClientRect();
                return node.style.left && node.style.top
                  && box.left >= 0 && box.top >= 0
                  && box.right <= innerWidth && box.bottom <= innerHeight;
              });
            }""",
            timeout=timeout_ms,
        )
        room_count = page.locator(".dollhouse-node").count()
        if room_count != candidate.spatial_room_count:
            raise MaterializationError("dollhouse_room_count_mismatch")
        if "approximate, not measured" not in page.locator("#dollhouse-note").inner_text().lower():
            raise MaterializationError("dollhouse_disclosure_missing")
        visible_boxes = page.locator(".dollhouse-node:visible").evaluate_all(
            "nodes => nodes.map(node => { const box = node.getBoundingClientRect(); return {left: box.left, right: box.right, top: box.top, bottom: box.bottom}; })"
        )
        for index, left in enumerate(visible_boxes):
            if left["left"] < 0 or left["top"] < 0 or left["right"] > 1440 or left["bottom"] > 960:
                raise MaterializationError("dollhouse_label_outside_viewport")
            for right in visible_boxes[index + 1 :]:
                if (
                    left["left"] < right["right"]
                    and left["right"] > right["left"]
                    and left["top"] < right["bottom"]
                    and left["bottom"] > right["top"]
                ):
                    raise MaterializationError("dollhouse_label_collision")
        source = page.content()
        for marker in (
            "function addDollhouseWall",
            "openingFor",
            "dollhouseRaycaster.intersectObjects",
        ):
            if marker not in source:
                raise MaterializationError("dollhouse_control_wiring_missing")
        dollhouse_output = output_path.with_name("browser-dollhouse.png")
        page.screenshot(
            path=str(dollhouse_output), full_page=False, animations="disabled"
        )
        walkable_scene = candidate.payload.get("walkable_scene")
        spatial_model = (
            walkable_scene.get("spatial_model")
            if isinstance(walkable_scene, Mapping)
            else {}
        )
        spatial_rooms = (
            spatial_model.get("rooms")
            if isinstance(spatial_model, Mapping)
            and isinstance(spatial_model.get("rooms"), list)
            else []
        )
        navigable_spatial_rooms = [
            room
            for room in spatial_rooms
            if isinstance(room, Mapping)
            and str(room.get("scene_id") or "") in candidate.scene_ids
        ]
        if not navigable_spatial_rooms:
            raise MaterializationError("dollhouse_raycast_target_missing")
        raycast_room = max(
            navigable_spatial_rooms,
            key=lambda room: float(room.get("width") or 0.0)
            * float(room.get("depth") or 0.0),
        )
        target_scene = str(raycast_room.get("scene_id") or "")
        raycast_x, raycast_y = _dollhouse_floor_screen_point(
            rooms=[room for room in spatial_rooms if isinstance(room, Mapping)],
            target_room=raycast_room,
            viewport_width=1440.0,
            viewport_height=960.0,
        )
        page.locator("#viewer").evaluate(
            """(viewer, point) => {
              const event = (type, buttons) => new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 41,
                pointerType: 'mouse', isPrimary: true, buttons,
                clientX: point.x, clientY: point.y,
              });
              viewer.dispatchEvent(event('pointerdown', 1));
              viewer.dispatchEvent(event('pointerup', 0));
            }""",
            {"x": raycast_x, "y": raycast_y},
        )
        _wait_for_scene(page, target_scene, timeout_ms=timeout_ms)
        if page.locator("body").get_attribute("data-mode") != "panorama":
            raise MaterializationError("dollhouse_canvas_raycast_navigation_failed")
        _assert_audit_clean(audit, label="desktop")
        if (
            audit.renderer_status != 200
            or audit.renderer_sha256 != RENDERER_MODULE_SHA256
        ):
            raise MaterializationError("renderer_module_binding_invalid")
        if (
            audit.viewer_implementation_sha256
            != audit.expected_viewer_implementation_sha256
        ):
            raise MaterializationError("viewer_implementation_binding_invalid")
        room_heights = {
            float(room.get("height") or 0.0)
            for room in spatial_rooms
            if isinstance(room, Mapping)
        }
        return {
            "initial_scene_loaded_ms": initial_loaded_ms,
            "browser_spec_sha256": browser_spec_sha256,
            "drag_navigation_verified": drag_navigation_verified,
            "renderer_status": audit.renderer_status,
            "renderer_sha256": audit.renderer_sha256,
            "viewer_implementation_sha256": audit.viewer_implementation_sha256,
            "immutable_asset_digests": set(audit.immutable_asset_digests),
            "desktop": {
                "viewport": "1440x960",
                "canvas": _canvas_size(page),
                "scene_rail_count": len(candidate.scene_ids),
                "floorplan_pin_count": len(candidate.scene_ids),
                "initial_scene": candidate.scene_ids[0],
                "drag_interaction": "pass",
                "map_control": "pass",
                "hud_collision_check": "pass",
                "control_semantics": "pass",
                "fullscreen_control_visible": page.locator("#fullscreen").is_visible(),
                "page_errors": [],
                "failed_requests": [],
                "console_errors": [],
            },
            "dollhouse": {
                "viewport": "1440x960",
                "canvas": _canvas_size(page),
                "room_count": room_count,
                "open_wall_geometry": True,
                "variable_room_height": len(room_heights) > 1,
                "doorway_gaps": True,
                "collision_managed_labels": True,
                "room_navigation": "pass",
                "raycast_wiring": "pass",
                "raycast_interaction": "canvas_pointer_pass",
                "raycast_target_scene": target_scene,
                "raycast_screen_point": [round(raycast_x, 1), round(raycast_y, 1)],
                "page_errors": [],
                "failed_requests": [],
                "console_errors": [],
            },
        }
    finally:
        context.close()


def _verify_and_capture_mobile(
    *,
    browser: object,
    candidate: PreparedCandidate,
    tested_url: str,
    tested_origin: str,
    transport_origin: str,
    output_path: Path,
    timeout_ms: int,
) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        service_workers="block",
    )
    audit = BrowserAudit(
        tested_origin,
        transport_origin,
        _browser_allowed_urls(
            candidate=candidate,
            tested_origin=tested_origin,
            tested_url=tested_url,
        ),
        _browser_expected_assets(
            candidate=candidate,
            tested_origin=tested_origin,
        ),
        tested_url,
        _expected_viewer_implementation_sha256(candidate),
    )
    audit.install(context)
    page = context.new_page()
    audit.watch(page)
    try:
        started = time.monotonic()
        response = page.goto(tested_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            raise MaterializationError("mobile_control_http_not_200")
        _assert_csp(response.headers)
        _wait_for_panorama(page, timeout_ms=timeout_ms)
        initial_loaded_ms = round((time.monotonic() - started) * 1000.0, 1)
        if page.locator(".scene-button").count() != len(candidate.scene_ids):
            raise MaterializationError("mobile_scene_rail_incomplete")
        if not page.locator("#floorplan").evaluate(
            "node => node.classList.contains('collapsed')"
        ):
            raise MaterializationError("mobile_floorplan_not_collapsed")
        overflow = page.evaluate(
            "() => ({viewport: innerWidth, html: document.documentElement.scrollWidth, body: document.body.scrollWidth})"
        )
        if overflow["html"] > overflow["viewport"] + 1 or overflow["body"] > overflow["viewport"] + 1:
            raise MaterializationError(f"mobile_horizontal_overflow:{overflow}")
        _assert_viewer_layout(page, require_expanded_floorplan=False)

        viewer = page.locator("#viewer")
        before_pinch = float(viewer.get_attribute("data-panorama-fov") or 0.0)
        viewer.evaluate(
            """element => {
              const fire = (type, id, x, y, primary) => element.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: id, pointerType: 'touch',
                isPrimary: primary, clientX: x, clientY: y, buttons: type === 'pointerup' ? 0 : 1,
              }));
              fire('pointerdown', 1, 135, 420, true);
              fire('pointerdown', 2, 235, 420, false);
              fire('pointermove', 2, 300, 420, false);
              fire('pointerup', 2, 300, 420, false);
              fire('pointerup', 1, 135, 420, true);
            }"""
        )
        after_pinch = float(viewer.get_attribute("data-panorama-fov") or 0.0)
        if not (after_pinch > 0 and abs(after_pinch - before_pinch) >= 1.0):
            raise MaterializationError("mobile_pinch_zoom_failed")
        before_zoom = after_pinch
        page.locator("#zoom-in").click()
        after_zoom_in = float(viewer.get_attribute("data-panorama-fov") or 0.0)
        page.locator("#zoom-out").click()
        after_zoom_out = float(viewer.get_attribute("data-panorama-fov") or 0.0)
        if not (after_zoom_in < before_zoom and after_zoom_out > after_zoom_in):
            raise MaterializationError("mobile_zoom_buttons_failed")

        metadata = _scene_metadata(candidate)
        verified_edges: set[str] = set()
        screenshot_source = max(
            candidate.scene_ids,
            key=lambda scene_id: len(metadata[scene_id].get("hotspots") or []),
        )
        screenshot_target = ""
        verified_hotspot_count = 0
        for source_id in candidate.scene_ids:
            scene = metadata[source_id]
            hotspots = [
                hotspot
                for hotspot in scene.get("hotspots") or []
                if isinstance(hotspot, Mapping)
            ]
            for hotspot_index, hotspot in enumerate(hotspots):
                target = str(
                    hotspot.get("target_scene_id") or hotspot.get("target") or ""
                ).strip()
                page.locator(f'.scene-button[data-scene-id="{source_id}"]').click()
                _wait_for_scene(page, source_id, timeout_ms=timeout_ms)
                _align_hotspot_with_keyboard(page, scene=scene, hotspot=hotspot)
                locator = page.locator(".hotspot").nth(hotspot_index)
                if locator.get_attribute("data-target-scene-id") != target:
                    raise MaterializationError("mobile_hotspot_dom_order_mismatch")
                locator.wait_for(state="visible", timeout=timeout_ms)
                _assert_viewer_layout(page, require_expanded_floorplan=False)
                bounds = locator.bounding_box()
                controls = page.locator(".zoom-controls").bounding_box()
                if bounds is None or controls is None:
                    raise MaterializationError("mobile_hotspot_bounds_missing")
                if (
                    bounds["x"] < 9
                    or bounds["y"] < 9
                    or bounds["x"] + bounds["width"] > 381
                    or bounds["y"] + bounds["height"] > 835
                ):
                    raise MaterializationError(
                        f"mobile_hotspot_outside_viewport:{source_id}->{target}:{bounds}"
                    )
                overlaps_controls = not (
                    bounds["x"] + bounds["width"] <= controls["x"] - 7
                    or bounds["x"] >= controls["x"] + controls["width"] + 7
                    or bounds["y"] + bounds["height"] <= controls["y"] - 7
                    or bounds["y"] >= controls["y"] + controls["height"] + 7
                )
                if overlaps_controls:
                    raise MaterializationError("mobile_hotspot_overlaps_zoom_controls")
                locator.tap(timeout=timeout_ms)
                _wait_for_scene(page, target, timeout_ms=timeout_ms)
                verified_edges.add(f"{source_id}->{target}")
                verified_hotspot_count += 1
                if source_id == screenshot_source and not screenshot_target:
                    screenshot_target = target
        if tuple(sorted(verified_edges)) != candidate.hotspot_edges:
            raise MaterializationError("mobile_hotspot_edge_coverage_incomplete")
        if verified_hotspot_count != candidate.hotspot_count:
            raise MaterializationError("mobile_hotspot_coverage_incomplete")

        page.locator(f'.scene-button[data-scene-id="{screenshot_source}"]').click()
        _wait_for_scene(page, screenshot_source, timeout_ms=timeout_ms)
        screenshot_hotspot = next(
            hotspot
            for hotspot in metadata[screenshot_source].get("hotspots") or []
            if isinstance(hotspot, Mapping)
            and str(hotspot.get("target_scene_id") or hotspot.get("target") or "")
            == screenshot_target
        )
        _align_hotspot_with_keyboard(
            page,
            scene=metadata[screenshot_source],
            hotspot=screenshot_hotspot,
        )
        page.locator(
            f'.hotspot[data-target-scene-id="{screenshot_target}"]'
        ).wait_for(state="visible", timeout=timeout_ms)
        _assert_viewer_layout(page, require_expanded_floorplan=False)
        map_toggle = page.locator("#map-toggle")
        map_toggle.click()
        page.wait_for_function(
            "() => document.querySelector('#map-toggle')?.getAttribute('aria-pressed') === 'true' && !document.querySelector('#floorplan')?.classList.contains('collapsed') && Number(getComputedStyle(document.querySelector('#floorplan')).opacity) > .95",
            timeout=timeout_ms,
        )
        _assert_viewer_layout(page, require_expanded_floorplan=True)
        map_toggle.click()
        page.wait_for_function(
            "() => document.querySelector('#map-toggle')?.getAttribute('aria-pressed') === 'false' && document.querySelector('#floorplan')?.classList.contains('collapsed') && Number(getComputedStyle(document.querySelector('#floorplan')).opacity) < .05",
            timeout=timeout_ms,
        )
        _assert_viewer_layout(page, require_expanded_floorplan=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=False, animations="disabled")
        _assert_audit_clean(audit, label="mobile")
        return {
            "mobile_initial_scene_loaded_ms": initial_loaded_ms,
            "verified_hotspot_edges": tuple(sorted(verified_edges)),
            "verified_hotspot_count": verified_hotspot_count,
            "immutable_asset_digests": set(audit.immutable_asset_digests),
            "mobile": {
                "viewport": "390x844",
                "canvas": _canvas_size(page),
                "scene_rail_count": len(candidate.scene_ids),
                "floorplan_default": "collapsed",
                "has_touch": True,
                "pinch_zoom": "pass",
                "zoom_buttons": "pass",
                "hotspot_containment": "pass",
                "hotspot_collision_check": "pass",
                "protected_surface_collision_check": "pass",
                "expanded_floorplan_collision_check": "pass",
                "control_semantics": "pass",
                "verified_scene": screenshot_source,
                "page_errors": [],
                "failed_requests": [],
                "console_errors": [],
            },
        }
    finally:
        context.close()


def _verify_slow_network(
    *,
    browser: object,
    candidate: PreparedCandidate,
    tested_url: str,
    tested_origin: str,
    transport_origin: str,
    timeout_ms: int,
) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 960},
        device_scale_factor=1,
        service_workers="block",
    )
    audit = BrowserAudit(
        tested_origin,
        transport_origin,
        _browser_allowed_urls(
            candidate=candidate,
            tested_origin=tested_origin,
            tested_url=tested_url,
        ),
        _browser_expected_assets(
            candidate=candidate,
            tested_origin=tested_origin,
        ),
        tested_url,
        _expected_viewer_implementation_sha256(candidate),
        slow_network=True,
    )
    audit.install(context)
    page = context.new_page()
    audit.watch(page)
    try:
        started = time.monotonic()
        response = page.goto(tested_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            raise MaterializationError("slow_network_control_http_not_200")
        _wait_for_panorama(page, timeout_ms=timeout_ms)
        initial_loaded_ms = round((time.monotonic() - started) * 1000.0, 1)
        for scene_id in candidate.scene_ids:
            page.locator(f'.scene-button[data-scene-id="{scene_id}"]').click()
            _wait_for_scene(page, scene_id, timeout_ms=timeout_ms)
        _assert_audit_clean(audit, label="slow_network")
        if initial_loaded_ms > 20_000.0:
            raise MaterializationError("slow_network_initial_budget_exceeded")
        return {
            "slow_network_initial_scene_loaded_ms": initial_loaded_ms,
            "immutable_asset_digests": set(audit.immutable_asset_digests),
        }
    finally:
        context.close()


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            return int(image.width), int(image.height)
    except Exception as exc:
        raise MaterializationError(f"screenshot_invalid:{path.name}") from exc


def _capture_browser_proof(
    *,
    candidate: PreparedCandidate,
    tested_origin: str,
    transport_origin: str,
    output_dir: Path,
    observed_at: str,
    timeout_ms: int,
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise MaterializationError("playwright_unavailable") from exc
    tested_path = f"/tours/{urllib.parse.quote(candidate.slug, safe='')}/control"
    tested_url = f"{tested_origin}{tested_path}"
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-proxy-server",
        "--autoplay-policy=no-user-gesture-required",
        "--use-angle=swiftshader",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--disable-gpu-sandbox",
        "--renderer-process-limit=2",
    ]
    with sync_playwright() as playwright:
        browser = playwright_engine_launch_browser(
            playwright,
            engine="chromium",
            args=launch_args,
            headless=True,
        )
        try:
            desktop = _verify_and_capture_desktop(
                browser=browser,
                candidate=candidate,
                tested_url=tested_url,
                tested_origin=tested_origin,
                transport_origin=transport_origin,
                output_path=output_dir / "browser-desktop.png",
                timeout_ms=timeout_ms,
            )
            mobile = _verify_and_capture_mobile(
                browser=browser,
                candidate=candidate,
                tested_url=tested_url,
                tested_origin=tested_origin,
                transport_origin=transport_origin,
                output_path=output_dir / "browser-mobile.png",
                timeout_ms=timeout_ms,
            )
            slow = _verify_slow_network(
                browser=browser,
                candidate=candidate,
                tested_url=tested_url,
                tested_origin=tested_origin,
                transport_origin=transport_origin,
                timeout_ms=max(timeout_ms, 120_000),
            )
        finally:
            browser.close()

    screenshot_files = {
        "desktop": output_dir / "browser-desktop.png",
        "mobile": output_dir / "browser-mobile.png",
        "dollhouse": output_dir / "browser-dollhouse.png",
    }
    expected_dimensions = {
        "desktop": (1440, 960),
        "mobile": (780, 1688),
        "dollhouse": (1440, 960),
    }
    screenshot_hashes: dict[str, str] = {}
    for surface, path in screenshot_files.items():
        if _png_dimensions(path) != expected_dimensions[surface]:
            raise MaterializationError(f"{surface}_screenshot_dimensions_invalid")
        screenshot_hashes[surface] = _sha256_file(path)
    if len(set(screenshot_hashes.values())) != 3:
        raise MaterializationError("screenshots_not_distinct")

    immutable_digests = (
        set(desktop["immutable_asset_digests"])
        | set(mobile["immutable_asset_digests"])
        | set(slow["immutable_asset_digests"])
    )
    walkable_scene = candidate.payload.get("walkable_scene")
    acceptance = (
        walkable_scene.get("acceptance")
        if isinstance(walkable_scene, Mapping)
        and isinstance(walkable_scene.get("acceptance"), Mapping)
        else {}
    )
    provenance_relpath = str(acceptance.get("provenance_relpath") or "")
    expected_asset_digests = {
        str(row["sha256"])
        for row in candidate.bundle_material_files
        if str(row["relpath"]) != provenance_relpath
    }
    if not expected_asset_digests.issubset(immutable_digests):
        raise MaterializationError("immutable_asset_cache_coverage_incomplete")

    desktop_receipt = dict(desktop["desktop"])
    mobile_receipt = dict(mobile["mobile"])
    dollhouse_receipt = dict(desktop["dollhouse"])
    for surface, receipt in (
        ("desktop", desktop_receipt),
        ("mobile", mobile_receipt),
        ("dollhouse", dollhouse_receipt),
    ):
        receipt["screenshot_relpath"] = SCREENSHOT_RELPATHS[surface]
        receipt["screenshot_sha256"] = screenshot_hashes[surface]
    return {
        "contract_name": CONTRACT_NAME,
        "proof_status": "pass",
        "observed_at": observed_at,
        "core_manifest_sha256": candidate.core_manifest_sha256,
        "bundle_material_sha256": candidate.bundle_material_sha256,
        "bundle_material_file_count": len(candidate.bundle_material_files),
        "browser_spec_sha256": desktop["browser_spec_sha256"],
        "tested_url": tested_url,
        "tested_origin": tested_origin,
        "tour_path": tested_path,
        "test_transport": (
            "direct_origin"
            if tested_origin == transport_origin
            else "canonical_hostname_replay_over_loopback"
        ),
        "route_stack": "fastapi_public_route",
        "viewer_implementation": "app.api.routes.public_tours._tour_control_panorama_html",
        "viewer_implementation_sha256": desktop[
            "viewer_implementation_sha256"
        ],
        "representation_disclosure": _candidate_representation_disclosure(candidate),
        "scene_ids": list(candidate.scene_ids),
        "anonymous_http_200": True,
        "drag_navigation_verified": desktop["drag_navigation_verified"] is True,
        "scene_navigation_verified": True,
        "all_hotspots_verified": tuple(mobile["verified_hotspot_edges"])
        == candidate.hotspot_edges
        and int(mobile["verified_hotspot_count"]) == candidate.hotspot_count,
        "dollhouse_verified": True,
        "desktop_verified": True,
        "mobile_verified": True,
        "touch_verified": True,
        "pinch_zoom_verified": True,
        "zoom_controls_verified": True,
        "dollhouse_raycast_verified": True,
        "first_party_viewer_verified": True,
        "first_party_renderer_verified": True,
        "slow_network_verified": True,
        "performance_budget_verified": True,
        "immutable_asset_cache_verified": True,
        "self_only_csp_verified": True,
        "canonical_host_replay_verified": tested_origin != transport_origin,
        "disclosure_verified": True,
        "hud_collision_verified": True,
        "renderer_module_path": RENDERER_MODULE_PATH,
        "renderer_module_sha256": str(desktop["renderer_sha256"]),
        "renderer_http_status": int(desktop["renderer_status"]),
        "external_script_requests": [],
        "verified_hotspot_edges": list(candidate.hotspot_edges),
        "verified_hotspot_count": candidate.hotspot_count,
        "dollhouse_room_count": candidate.spatial_room_count,
        "performance": {
            "initial_scene_loaded_ms": desktop["initial_scene_loaded_ms"],
            "mobile_initial_scene_loaded_ms": mobile["mobile_initial_scene_loaded_ms"],
            "slow_network_initial_scene_loaded_ms": slow[
                "slow_network_initial_scene_loaded_ms"
            ],
            "total_panorama_bytes": candidate.total_panorama_bytes,
            "largest_panorama_bytes": candidate.largest_panorama_bytes,
            "slow_network_profile": "150ms-latency-4mbps",
            "slow_network_all_scenes_loaded": True,
        },
        "desktop": desktop_receipt,
        "mobile": mobile_receipt,
        "dollhouse": dollhouse_receipt,
        "_temporary_screenshot_paths": {
            surface: str(path) for surface, path in screenshot_files.items()
        },
    }


@contextmanager
def _public_tour_base_environment(tested_origin: str) -> Iterator[None]:
    key = "PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL"
    previous = os.environ.get(key)
    os.environ[key] = f"{tested_origin}/tours"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _installer_source_identity(
    bundle_dir: Path,
    *,
    reason: str,
) -> installer_intake.AiPanoramaInstallerSourceIdentity:
    try:
        return installer_intake.snapshot_ai_panorama_installer_source_bundle(bundle_dir)
    except installer_intake.AiPanoramaIntakeError as exc:
        raise MaterializationError(reason) from exc


def _installer_identity_receipt_fields(
    identity: installer_intake.AiPanoramaInstallerSourceIdentity,
) -> dict[str, object]:
    return {
        "installer_source_identity_contract": identity.contract_name,
        "installer_source_tree_algorithm": identity.tree_algorithm,
        "installer_source_relative_root": identity.relative_root,
        "installer_source_relative_path_semantics": identity.relative_path_semantics,
        "installer_source_tree_sha256": identity.tree_sha256,
        "installer_source_tour_sha256": identity.tour_sha256,
        "installer_source_file_count": identity.file_count,
        "installer_source_total_bytes": identity.total_bytes,
    }


def _assert_regular_file_identity(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_content: bytes,
    reason: str,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            metadata.st_size <= 0
            or metadata.st_size > MAX_JSON_BYTES
            or metadata.st_size != len(expected_content)
        ):
            raise MaterializationError(reason)
        chunks: list[bytes] = []
        remaining = int(metadata.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise MaterializationError(reason)
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after_open = os.fstat(descriptor)
    except OSError as exc:
        raise MaterializationError(reason) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino))
        != expected_identity
        or content != expected_content
        or (
            int(after_open.st_dev),
            int(after_open.st_ino),
            int(after_open.st_size),
            int(after_open.st_mtime_ns),
        )
        != (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    ):
        raise MaterializationError(reason)
    try:
        after = path.lstat()
    except OSError as exc:
        raise MaterializationError(reason) from exc
    if (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
    ) != (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    ):
        raise MaterializationError(reason)


def _assert_candidate_marker_identity(
    candidate: PreparedCandidate,
    *,
    reason: str,
) -> None:
    _assert_regular_file_identity(
        candidate.public_root / CANDIDATE_MARKER_RELPATH,
        expected_identity=candidate.candidate_marker_identity,
        expected_content=candidate.candidate_marker_pending_bytes,
        reason=reason,
    )


def _assert_sealed_candidate_identity(
    candidate: PreparedCandidate,
    receipt: Mapping[str, object],
    *,
    reason: str,
) -> None:
    try:
        if (
            _directory_identity(candidate.public_root, label="candidate_root")
            != candidate.candidate_public_root_directory_identity
            or _directory_identity(candidate.bundle_dir, label="candidate_bundle")
            != candidate.candidate_bundle_directory_identity
        ):
            raise MaterializationError(reason)
        candidate_snapshot = _regular_tree_snapshot(candidate.bundle_dir)
        installer_identity = _installer_source_identity(
            candidate.bundle_dir,
            reason=reason,
        )
        _assert_candidate_marker_identity(candidate, reason=reason)
    except MaterializationError as exc:
        if str(exc) == reason:
            raise
        raise MaterializationError(reason) from exc
    if candidate_snapshot != (
        receipt.get("candidate_file_count"),
        receipt.get("candidate_size_bytes"),
        receipt.get("candidate_tree_sha256"),
    ):
        raise MaterializationError(reason)
    if any(
        receipt.get(key) != value
        for key, value in _installer_identity_receipt_fields(installer_identity).items()
    ):
        raise MaterializationError(reason)
    if (
        receipt.get("tour_manifest_sha256") != installer_identity.tour_sha256
        or receipt.get("candidate_marker_sha256")
        != candidate.candidate_marker_pending_sha256
    ):
        raise MaterializationError(reason)
    try:
        if (
            _directory_identity(candidate.public_root, label="candidate_root")
            != candidate.candidate_public_root_directory_identity
            or _directory_identity(candidate.bundle_dir, label="candidate_bundle")
            != candidate.candidate_bundle_directory_identity
        ):
            raise MaterializationError(reason)
        final_candidate_snapshot = _regular_tree_snapshot(candidate.bundle_dir)
        final_installer_identity = _installer_source_identity(
            candidate.bundle_dir,
            reason=reason,
        )
        _assert_candidate_marker_identity(candidate, reason=reason)
        if (
            _directory_identity(candidate.public_root, label="candidate_root")
            != candidate.candidate_public_root_directory_identity
            or _directory_identity(candidate.bundle_dir, label="candidate_bundle")
            != candidate.candidate_bundle_directory_identity
        ):
            raise MaterializationError(reason)
    except MaterializationError as exc:
        if str(exc) == reason:
            raise
        raise MaterializationError(reason) from exc
    if (
        final_candidate_snapshot != candidate_snapshot
        or final_installer_identity != installer_identity
    ):
        raise MaterializationError(reason)


def _seal_candidate(
    *,
    candidate: PreparedCandidate,
    browser_proof: dict[str, object],
    tested_origin: str,
) -> dict[str, object]:
    temporary_paths = browser_proof.pop("_temporary_screenshot_paths", None)
    if not isinstance(temporary_paths, dict):
        raise MaterializationError("temporary_screenshot_paths_missing")
    if browser_proof.get("contract_name") != CONTRACT_NAME:
        raise MaterializationError("browser_proof_contract_invalid")
    if browser_proof.get("core_manifest_sha256") != candidate.core_manifest_sha256:
        raise MaterializationError("browser_proof_core_binding_invalid")
    if browser_proof.get("bundle_material_sha256") != candidate.bundle_material_sha256:
        raise MaterializationError("browser_proof_bundle_binding_invalid")
    browser_spec_sha256 = str(browser_proof.get("browser_spec_sha256") or "")
    viewer_implementation_sha256 = str(
        browser_proof.get("viewer_implementation_sha256") or ""
    )
    for value, reason in (
        (browser_spec_sha256, "browser_spec_binding_invalid"),
        (viewer_implementation_sha256, "viewer_implementation_binding_invalid"),
    ):
        if DIGEST_PATTERN.fullmatch(value) is None:
            raise MaterializationError(reason)

    for surface, relpath in SCREENSHOT_RELPATHS.items():
        source = Path(str(temporary_paths.get(surface) or ""))
        if not source.is_absolute() or not source.is_file() or source.is_symlink():
            raise MaterializationError(f"{surface}_temporary_screenshot_invalid")
        receipt = browser_proof.get(surface)
        if not isinstance(receipt, dict):
            raise MaterializationError(f"{surface}_receipt_invalid")
        if (
            receipt.get("screenshot_relpath") != relpath
            or receipt.get("screenshot_sha256") != _sha256_file(source)
        ):
            raise MaterializationError(f"{surface}_screenshot_binding_invalid")
        destination = _confined_path(candidate.bundle_dir, relpath)
        _atomic_write(destination, source.read_bytes())

    proof_bytes = _canonical_json_bytes(browser_proof)
    proof_sha256 = _sha256_bytes(proof_bytes)
    proof_path = _confined_path(candidate.bundle_dir, BROWSER_RECEIPT_RELPATH)
    _atomic_write(proof_path, proof_bytes)
    if _sha256_file(proof_path) != proof_sha256:
        raise MaterializationError("browser_proof_write_mismatch")

    sealed_payload = json.loads(_canonical_json_bytes(candidate.payload))
    walkable_scene = sealed_payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        raise MaterializationError("walkable_scene_missing")
    acceptance = walkable_scene.get("acceptance")
    if not isinstance(acceptance, dict):
        raise MaterializationError("acceptance_missing")
    acceptance.update(
        {
            "proof_status": "pass",
            "core_manifest_sha256": candidate.core_manifest_sha256,
            "browser_receipt_relpath": BROWSER_RECEIPT_RELPATH,
            "browser_receipt_sha256": proof_sha256,
        }
    )
    if (
        property_tour_hosting._hosted_property_tour_ai_panorama_core_manifest_sha256(
            sealed_payload
        )
        != candidate.core_manifest_sha256
    ):
        raise MaterializationError("sealed_core_manifest_changed")
    with _public_tour_base_environment(tested_origin):
        full_contract = (
            property_tour_hosting._hosted_property_tour_ai_panorama_contract(
                bundle_dir=candidate.bundle_dir,
                payload=sealed_payload,
                mode="full",
            )
        )
    if full_contract.get("ready") is not True:
        raise MaterializationError(
            f"sealed_contract_invalid:{full_contract.get('reason') or 'unknown'}"
        )

    manifest_path = candidate.bundle_dir / "tour.json"
    pending_manifest_bytes = _canonical_json_bytes(candidate.payload)
    _assert_tree_identity(
        candidate.source_bundle,
        directory_identity=candidate.source_directory_identity,
        file_count=candidate.source_file_count,
        total_bytes=candidate.source_size_bytes,
        tree_sha256=candidate.source_tree_sha256,
        reason="source_bundle_mutated",
    )
    try:
        _atomic_write(manifest_path, _canonical_json_bytes(sealed_payload))
        written_payload = _load_json_object(manifest_path)
        if written_payload != sealed_payload:
            raise MaterializationError("sealed_manifest_write_mismatch")
        _assert_tree_identity(
            candidate.source_bundle,
            directory_identity=candidate.source_directory_identity,
            file_count=candidate.source_file_count,
            total_bytes=candidate.source_size_bytes,
            tree_sha256=candidate.source_tree_sha256,
            reason="source_bundle_mutated",
        )
        candidate_file_count, candidate_size_bytes, candidate_tree_sha256 = (
            _regular_tree_snapshot(candidate.bundle_dir)
        )
        _assert_candidate_marker_identity(
            candidate,
            reason="candidate_marker_changed_before_seal",
        )
        marker_sha256 = candidate.candidate_marker_pending_sha256
        screenshot_receipts = {
            surface: {
                "relpath": relpath,
                "sha256": _sha256_file(
                    _confined_path(candidate.bundle_dir, relpath)
                ),
            }
            for surface, relpath in SCREENSHOT_RELPATHS.items()
        }
        tour_manifest_sha256 = _sha256_file(manifest_path)
        installer_identity = _installer_source_identity(
            candidate.bundle_dir,
            reason="candidate_installer_identity_invalid",
        )
        if (
            installer_identity.file_count != candidate_file_count
            or installer_identity.total_bytes != candidate_size_bytes
            or installer_identity.tour_sha256 != tour_manifest_sha256
        ):
            raise MaterializationError("candidate_installer_identity_mismatch")
        _assert_tree_identity(
            candidate.source_bundle,
            directory_identity=candidate.source_directory_identity,
            file_count=candidate.source_file_count,
            total_bytes=candidate.source_size_bytes,
            tree_sha256=candidate.source_tree_sha256,
            reason="source_bundle_mutated",
        )
    except Exception:
        try:
            _atomic_write(manifest_path, pending_manifest_bytes)
        except Exception as rollback_exc:
            raise MaterializationError("sealed_manifest_rollback_failed") from rollback_exc
        raise
    return {
        "contract_name": MATERIALIZATION_RECEIPT_CONTRACT,
        "status": "pass",
        "slug": candidate.slug,
        "candidate_public_root": str(candidate.public_root),
        "candidate_bundle_relpath": candidate.slug,
        "candidate_marker_relpath": CANDIDATE_MARKER_RELPATH,
        "candidate_marker_sha256": marker_sha256,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_file_count": candidate_file_count,
        "candidate_size_bytes": candidate_size_bytes,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "core_manifest_sha256": candidate.core_manifest_sha256,
        "bundle_material_sha256": candidate.bundle_material_sha256,
        "browser_proof_sha256": proof_sha256,
        "browser_spec_sha256": browser_spec_sha256,
        "viewer_implementation_sha256": viewer_implementation_sha256,
        "tour_manifest_sha256": tour_manifest_sha256,
        **_installer_identity_receipt_fields(installer_identity),
        "screenshots": screenshot_receipts,
        "source_identity": {
            "kind": "filesystem_tree_sha256",
            "identifier": f"filesystem-tree-sha256:{candidate.source_tree_sha256}",
            "bundle_name": candidate.source_bundle.name,
        },
        "source_tree_sha256": candidate.source_tree_sha256,
        "source_file_count": candidate.source_file_count,
        "source_size_bytes": candidate.source_size_bytes,
        "source_copy_identity_verified": True,
        "source_bundle_unchanged": True,
        "source_unchanged_after_candidate_seal": True,
        "production_mutation_performed": False,
        "controller_bypass_performed": False,
    }


def _rollback_candidate_to_pending(candidate: PreparedCandidate) -> None:
    if (
        _directory_identity(candidate.bundle_dir, label="candidate_bundle")
        != candidate.candidate_bundle_directory_identity
    ):
        raise MaterializationError("candidate_pending_rollback_identity_changed")
    _assert_candidate_marker_identity(
        candidate,
        reason="candidate_pending_rollback_marker_changed",
    )
    manifest_path = candidate.bundle_dir / "tour.json"
    _atomic_write(manifest_path, _canonical_json_bytes(candidate.payload))
    written = _load_json_object(manifest_path)
    if written != candidate.payload:
        raise MaterializationError("candidate_pending_rollback_mismatch")
    for relpath in (BROWSER_RECEIPT_RELPATH, *SCREENSHOT_RELPATHS.values()):
        path = _confined_path(candidate.bundle_dir, relpath)
        if path.is_symlink():
            raise MaterializationError("candidate_pending_cleanup_symlink")
        path.unlink(missing_ok=True)
    proof_directory = candidate.bundle_dir / "proof"
    if proof_directory.is_dir() and not proof_directory.is_symlink():
        descriptor = os.open(proof_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _assert_candidate_marker_identity(
        candidate,
        reason="candidate_pending_rollback_marker_changed",
    )


def materialize(
    *,
    source_bundle: Path,
    candidate_public_root: Path,
    base_url: str,
    transport_base_url: str = "",
    expected_slug: str = "",
    expected_core_manifest_sha256: str = "",
    expected_source_tree_sha256: str = "",
    expected_bundle_material_sha256: str = "",
    receipt_out: Path | None = None,
    timeout_ms: int = 60_000,
    observed_at: str = "",
    capture: Callable[..., dict[str, object]] = _capture_browser_proof,
) -> dict[str, object]:
    source_bundle, candidate_public_root, receipt_out = _resolved_materialization_paths(
        source_bundle=source_bundle,
        candidate_public_root=candidate_public_root,
        receipt_out=receipt_out,
    )
    tested_origin = _normalized_origin(base_url, label="base_url")
    transport_origin = _normalized_origin(
        transport_base_url or base_url,
        label="transport_base_url",
    )
    _loopback_transport_required(transport_origin, tested_origin)
    if expected_slug and SLUG_PATTERN.fullmatch(expected_slug) is None:
        raise MaterializationError("expected_slug_invalid")
    for value, reason in (
        (expected_core_manifest_sha256, "expected_core_manifest_sha256_invalid"),
        (expected_source_tree_sha256, "expected_source_tree_sha256_invalid"),
        (expected_bundle_material_sha256, "expected_bundle_material_sha256_invalid"),
    ):
        if value and DIGEST_PATTERN.fullmatch(value) is None:
            raise MaterializationError(reason)
    if timeout_ms < 10_000 or timeout_ms > 300_000:
        raise MaterializationError("timeout_ms_out_of_bounds")
    if observed_at:
        try:
            parsed_observed_at = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise MaterializationError("observed_at_invalid") from exc
        if parsed_observed_at.tzinfo is None:
            raise MaterializationError("observed_at_invalid")
        normalized_observed_at = (
            parsed_observed_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    else:
        normalized_observed_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    candidate = _prepare_candidate_copy(
        source_bundle=source_bundle,
        candidate_public_root=candidate_public_root,
        expected_slug=expected_slug,
        expected_core_manifest_sha256=expected_core_manifest_sha256,
        expected_source_tree_sha256=expected_source_tree_sha256,
        expected_bundle_material_sha256=expected_bundle_material_sha256,
        receipt_out=receipt_out,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="propertyquarry-ai-panorama-proof-") as directory:
            browser_proof = capture(
                candidate=candidate,
                tested_origin=tested_origin,
                transport_origin=transport_origin,
                output_dir=Path(directory),
                observed_at=normalized_observed_at,
                timeout_ms=timeout_ms,
            )
            receipt = _seal_candidate(
                candidate=candidate,
                browser_proof=browser_proof,
                tested_origin=tested_origin,
            )
    except Exception:
        try:
            _rollback_candidate_to_pending(candidate)
        except Exception as rollback_exc:
            raise MaterializationError("candidate_failure_rollback_failed") from rollback_exc
        raise
    receipt["observed_at"] = normalized_observed_at
    receipt["tested_origin"] = tested_origin
    receipt["transport_origin"] = transport_origin
    receipt["external_receipt"] = {
        "written": receipt_out is not None,
        "source_unchanged_post_write": True if receipt_out is not None else None,
        "candidate_unchanged_post_write": True if receipt_out is not None else None,
    }
    receipt["candidate_identity_rechecked_after_receipt_write"] = (
        receipt_out is not None
    )
    if receipt_out is not None:
        receipt_bytes = _canonical_json_bytes(receipt)
        created_identity: tuple[int, int] | None = None
        try:
            rechecked_source, rechecked_candidate, rechecked_receipt = (
                _resolved_materialization_paths(
                    source_bundle=candidate.source_bundle,
                    candidate_public_root=candidate.public_root,
                    receipt_out=receipt_out,
                )
            )
            if (
                rechecked_source != candidate.source_bundle
                or rechecked_candidate != candidate.public_root
                or rechecked_receipt != receipt_out
            ):
                raise MaterializationError("materialization_path_identity_changed")
            _assert_tree_identity(
                candidate.source_bundle,
                directory_identity=candidate.source_directory_identity,
                file_count=candidate.source_file_count,
                total_bytes=candidate.source_size_bytes,
                tree_sha256=candidate.source_tree_sha256,
                reason="source_bundle_mutated_before_receipt_write",
            )
            _assert_sealed_candidate_identity(
                candidate,
                receipt,
                reason="candidate_bundle_mutated_before_receipt_write",
            )
            created_identity = _atomic_create(receipt_out, receipt_bytes)
            _assert_regular_file_identity(
                receipt_out,
                expected_identity=created_identity,
                expected_content=receipt_bytes,
                reason="receipt_out_write_mismatch",
            )
            _assert_tree_identity(
                candidate.source_bundle,
                directory_identity=candidate.source_directory_identity,
                file_count=candidate.source_file_count,
                total_bytes=candidate.source_size_bytes,
                tree_sha256=candidate.source_tree_sha256,
                reason="source_bundle_mutated_during_receipt_write",
            )
            _assert_sealed_candidate_identity(
                candidate,
                receipt,
                reason="candidate_bundle_mutated_during_receipt_write",
            )
            _assert_regular_file_identity(
                receipt_out,
                expected_identity=created_identity,
                expected_content=receipt_bytes,
                reason="receipt_out_changed_after_write",
            )
        except Exception:
            cleanup_error: Exception | None = None
            try:
                if created_identity is not None:
                    _unlink_created_file(receipt_out, created_identity)
                _rollback_candidate_to_pending(candidate)
            except Exception as exc:
                cleanup_error = exc
            if cleanup_error is not None:
                raise MaterializationError(
                    "receipt_failure_rollback_failed"
                ) from cleanup_error
            raise
    else:
        try:
            _assert_sealed_candidate_identity(
                candidate,
                receipt,
                reason="candidate_bundle_mutated_after_seal",
            )
        except Exception:
            try:
                _rollback_candidate_to_pending(candidate)
            except Exception as rollback_exc:
                raise MaterializationError(
                    "candidate_failure_rollback_failed"
                ) from rollback_exc
            raise
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize a canonical PropertyQuarry AI-panorama browser proof "
            "inside a new isolated candidate copy."
        )
    )
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--candidate-public-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--transport-base-url", default="")
    parser.add_argument("--expected-slug", default="")
    parser.add_argument("--expected-core-manifest-sha256", default="")
    parser.add_argument("--expected-source-tree-sha256", default="")
    parser.add_argument("--expected-bundle-material-sha256", default="")
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument("--receipt-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = materialize(
            source_bundle=args.source_bundle,
            candidate_public_root=args.candidate_public_root,
            base_url=args.base_url,
            transport_base_url=args.transport_base_url,
            expected_slug=args.expected_slug,
            expected_core_manifest_sha256=args.expected_core_manifest_sha256,
            expected_source_tree_sha256=args.expected_source_tree_sha256,
            expected_bundle_material_sha256=args.expected_bundle_material_sha256,
            receipt_out=args.receipt_out,
            timeout_ms=args.timeout_ms,
        )
    except MaterializationError as exc:
        print(f"propertyquarry_ai_panorama_materialization_failed:{exc}", file=sys.stderr)
        return 1
    content = _canonical_json_bytes(receipt)
    sys.stdout.buffer.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
