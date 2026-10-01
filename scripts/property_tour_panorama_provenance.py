#!/usr/bin/env python3
"""Fail-closed, byte-bound provenance for local panorama tour controls."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree


PANORAMA_SPATIAL_PROVENANCE_SCHEMA = (
    "propertyquarry.panorama_spatial_provenance.v1"
)
PANO2VR_SPATIAL_PROVENANCE_KEY = "pano2vr_spatial_provenance"
KRPANO_SPATIAL_PROVENANCE_KEY = "krpano_spatial_provenance"

MAX_ARTIFACT_FILES = 20_000
MAX_ARTIFACT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_DEPTH = 32
MAX_XML_FILES = 256
MAX_XML_FILE_BYTES = 8 * 1024 * 1024
MAX_XML_TOTAL_BYTES = 32 * 1024 * 1024

_ROOT_FIELDS = frozenset(
    {"schema", "status", "provider", "target_slug", "artifact", "capture", "authorization", "review"}
)
_ARTIFACT_FIELDS = frozenset({"kind", "sha256", "entry_relpath"})
_CAPTURE_FIELDS = frozenset(
    {"source_kind", "projection", "scene_count", "navigation_hotspot_count", "all_scenes_reachable"}
)
_AUTHORIZATION_FIELDS = frozenset({"status", "reference"})
_REVIEW_FIELDS = frozenset(
    {
        "property_match",
        "visual_match",
        "spatial_capture_match",
        "flat_composite_absent",
        "reviewed_by",
        "reviewed_at",
    }
)
_SOURCE_KINDS = frozenset(
    {"camera_equirectangular", "camera_cubemap", "authorized_provider_capture"}
)
_PROJECTIONS = frozenset({"equirectangular", "cubemap"})
_WALKABLE_SCENE_STRATEGIES = frozenset(
    {
        "walkable_360",
        "walkable_cube",
        "walkable_cubemap",
        "walkable_panorama",
    }
)
_WALKABLE_CREATION_MODES = frozenset(
    {"hosted_walkable_360", "walkable_360_tour"}
)
_PANORAMA_ASSET_KEYS = (
    "panorama_relpath",
    "equirect_relpath",
    "image_relpath",
    "asset_relpath",
)
_NAVIGATION_TARGET_KEYS = (
    "linkedscene",
    "linked_scene",
    "targetscene",
    "target_scene",
    "targetnode",
    "target_node",
    "nodeid",
    "node_id",
    "sceneid",
    "scene_id",
    "target",
    "url",
    "href",
)


def safe_relpath(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if "\\" in value or value.startswith("/") or "\x00" in value:
        return ""
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return ""
    normalized = PurePosixPath(*parts).as_posix()
    return normalized if normalized == value else ""


def _bounded_regular_files(
    root: Path,
    *,
    maximum_files: int = MAX_ARTIFACT_FILES,
    maximum_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
    maximum_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
    maximum_depth: int = MAX_ARTIFACT_DEPTH,
) -> list[tuple[str, Path, int]]:
    candidate_root = root.expanduser()
    details = candidate_root.stat(follow_symlinks=False)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ValueError("artifact_root_invalid")
    resolved_root = candidate_root.resolve()
    found: list[tuple[str, Path, int]] = []
    total_bytes = 0
    stack: list[tuple[Path, int]] = [(resolved_root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > maximum_depth:
            raise ValueError("artifact_depth_limit")
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_details = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_details.st_mode):
                    raise ValueError("artifact_symlink_forbidden")
                path = Path(entry.path)
                if stat.S_ISDIR(entry_details.st_mode):
                    stack.append((path, depth + 1))
                    continue
                if not stat.S_ISREG(entry_details.st_mode):
                    raise ValueError("artifact_special_file_forbidden")
                size = int(entry_details.st_size)
                if size < 0 or size > maximum_file_bytes:
                    raise ValueError("artifact_file_size_limit")
                total_bytes += size
                if total_bytes > maximum_total_bytes:
                    raise ValueError("artifact_total_size_limit")
                relpath = path.relative_to(resolved_root).as_posix()
                found.append((relpath, path, size))
                if len(found) > maximum_files:
                    raise ValueError("artifact_file_count_limit")
    return sorted(found, key=lambda item: item[0])


def _sha256_regular_file(path: Path, *, expected_size: int) -> str:
    details = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError("artifact_file_invalid")
    if int(details.st_size) != expected_size:
        raise ValueError("artifact_file_changed")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise ValueError("artifact_file_changed")
            digest.update(chunk)
    if total != expected_size:
        raise ValueError("artifact_file_changed")
    return digest.hexdigest()


def _update_artifact_digest(
    digest: Any,
    *,
    relpath: str,
    path: Path,
    size: int,
) -> None:
    digest.update(relpath.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    digest.update(_sha256_regular_file(path, expected_size=size).encode("ascii"))
    digest.update(b"\n")


def export_tree_sha256(
    export_dir: Path,
    *,
    maximum_files: int = MAX_ARTIFACT_FILES,
    maximum_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
    maximum_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
) -> str:
    digest = hashlib.sha256()
    files = _bounded_regular_files(
        export_dir,
        maximum_files=maximum_files,
        maximum_total_bytes=maximum_total_bytes,
        maximum_file_bytes=maximum_file_bytes,
    )
    for relpath, path, size in files:
        _update_artifact_digest(
            digest,
            relpath=relpath,
            path=path,
            size=size,
        )
    return digest.hexdigest() if files else ""


def asset_set_sha256(
    bundle_dir: Path,
    relpaths: Iterable[object],
    *,
    maximum_files: int = MAX_ARTIFACT_FILES,
    maximum_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
    maximum_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
) -> str:
    root_details = bundle_dir.stat(follow_symlinks=False)
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(root_details.st_mode):
        raise ValueError("asset_root_invalid")
    root = bundle_dir.resolve()
    normalized = sorted({safe_relpath(value) for value in relpaths})
    if not normalized or any(not value for value in normalized):
        return ""
    if len(normalized) > maximum_files:
        raise ValueError("artifact_file_count_limit")
    digest = hashlib.sha256()
    total_bytes = 0
    for relpath in normalized:
        path = root
        parts = PurePosixPath(relpath).parts
        for index, part in enumerate(parts):
            path = path / part
            details = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(details.st_mode):
                raise ValueError("asset_symlink_forbidden")
            if index < len(parts) - 1 and not stat.S_ISDIR(details.st_mode):
                raise ValueError("asset_parent_invalid")
        resolved_path = path.resolve()
        if resolved_path == root or root not in resolved_path.parents:
            raise ValueError("asset_path_outside_bundle")
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("asset_file_invalid")
        size = int(details.st_size)
        if size < 0 or size > maximum_file_bytes:
            raise ValueError("artifact_file_size_limit")
        total_bytes += size
        if total_bytes > maximum_total_bytes:
            raise ValueError("artifact_total_size_limit")
        _update_artifact_digest(
            digest,
            relpath=relpath,
            path=resolved_path,
            size=size,
        )
    return digest.hexdigest()


def panorama_walkable_required(payload: dict[str, object]) -> bool:
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    return (
        scene_strategy in _WALKABLE_SCENE_STRATEGIES
        or creation_mode in _WALKABLE_CREATION_MODES
    )


def _scene_asset_relpaths(scene: dict[str, object]) -> list[str]:
    paths = [safe_relpath(scene.get(key)) for key in _PANORAMA_ASSET_KEYS]
    cube_faces = scene.get("cube_faces")
    values = (
        list(cube_faces.values())
        if isinstance(cube_faces, dict)
        else list(cube_faces)
        if isinstance(cube_faces, list)
        else []
    )
    paths.extend(safe_relpath(value) for value in values)
    return [path for path in paths if path]


def panorama_asset_relpaths(payload: dict[str, object]) -> list[str]:
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        return []
    paths = _scene_asset_relpaths(walkable_scene)
    scenes = walkable_scene.get("scenes")
    scene_rows = list(scenes.values()) if isinstance(scenes, dict) else scenes
    if isinstance(scene_rows, list):
        for scene in scene_rows:
            if isinstance(scene, dict):
                paths.extend(_scene_asset_relpaths(scene))
    return sorted(set(paths))


def _local_name(value: object) -> str:
    return str(value or "").rsplit("}", 1)[-1].strip().lower()


def _normalize_target(value: object) -> str:
    target = str(value or "").strip()
    if not target or target.lower().startswith(("javascript:", "http:", "https:")):
        return ""
    if target.startswith("{") and target.endswith("}"):
        target = target[1:-1].strip()
    target = target.lstrip("#")
    if not target:
        return ""
    if "/" in target or target.lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm")
    ):
        return ""
    return target


def _element_navigation_target(element: ElementTree.Element) -> str:
    attributes = {_local_name(key): value for key, value in element.attrib.items()}
    for key in _NAVIGATION_TARGET_KEYS:
        target = _normalize_target(attributes.get(key))
        if target:
            return target
    for descendant in element.iter():
        if descendant is element:
            continue
        attributes = {_local_name(key): value for key, value in descendant.attrib.items()}
        for key in _NAVIGATION_TARGET_KEYS:
            target = _normalize_target(attributes.get(key))
            if target:
                return target
    return ""


def _all_reachable(scene_ids: list[str], edges: list[tuple[str, str]]) -> bool:
    if not scene_ids:
        return False
    if len(scene_ids) == 1:
        return True
    known = set(scene_ids)
    adjacency: dict[str, set[str]] = {scene_id: set() for scene_id in scene_ids}
    for source, target in edges:
        if source in known and target in known:
            adjacency[source].add(target)
    reached = {scene_ids[0]}
    pending = [scene_ids[0]]
    while pending:
        source = pending.pop()
        for target in adjacency.get(source, set()):
            if target not in reached:
                reached.add(target)
                pending.append(target)
    return reached == known


def pano2vr_export_topology(export_dir: Path) -> dict[str, object]:
    files = _bounded_regular_files(export_dir)
    xml_files = [(relpath, path, size) for relpath, path, size in files if path.suffix.lower() == ".xml"]
    if len(xml_files) > MAX_XML_FILES:
        raise ValueError("pano2vr_xml_file_count_limit")
    if sum(size for _, _, size in xml_files) > MAX_XML_TOTAL_BYTES:
        raise ValueError("pano2vr_xml_total_size_limit")

    scene_ids: list[str] = []
    edges: list[tuple[str, str]] = []
    navigation_hotspots = 0
    for relpath, path, size in xml_files:
        if size > MAX_XML_FILE_BYTES:
            raise ValueError("pano2vr_xml_file_size_limit")
        data = path.read_bytes()
        if len(data) != size:
            raise ValueError("pano2vr_xml_changed")
        lowered_data = data.lower()
        if b"<!doctype" in lowered_data or b"<!entity" in lowered_data:
            raise ValueError("pano2vr_xml_dtd_forbidden")
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            continue
        panoramas = [element for element in root.iter() if _local_name(element.tag) == "panorama"]
        for index, panorama in enumerate(panoramas):
            scene_id = str(
                panorama.attrib.get("id")
                or panorama.attrib.get("nodeid")
                or panorama.attrib.get("node_id")
                or f"{relpath}#{index + 1}"
            ).strip()
            if scene_id not in scene_ids:
                scene_ids.append(scene_id)
            for element in panorama.iter():
                if _local_name(element.tag) != "hotspot":
                    continue
                target = _element_navigation_target(element)
                if target:
                    navigation_hotspots += 1
                    edges.append((scene_id, target))
    return {
        "scene_count": len(scene_ids),
        "navigation_hotspot_count": navigation_hotspots,
        "all_scenes_reachable": _all_reachable(scene_ids, edges),
    }


def _mapping_navigation_target(row: dict[str, object]) -> str:
    for key in _NAVIGATION_TARGET_KEYS:
        target = _normalize_target(row.get(key))
        if target:
            return target
    return ""


def walkable_scene_topology(payload: dict[str, object]) -> dict[str, object]:
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        return {"scene_count": 0, "navigation_hotspot_count": 0, "all_scenes_reachable": False}
    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, dict):
        scene_rows: list[tuple[str, dict[str, object]]] = [
            (str(key), value) for key, value in raw_scenes.items() if isinstance(value, dict)
        ]
    elif isinstance(raw_scenes, list):
        scene_rows = [(str(index + 1), value) for index, value in enumerate(raw_scenes) if isinstance(value, dict)]
    else:
        scene_rows = []

    scene_ids: list[str] = []
    edges: list[tuple[str, str]] = []
    navigation_hotspots = 0
    if scene_rows:
        for fallback_id, scene in scene_rows:
            if not _scene_asset_relpaths(scene):
                continue
            scene_id = str(
                scene.get("id")
                or scene.get("node_id")
                or scene.get("scene_id")
                or fallback_id
            ).strip()
            if not scene_id or scene_id in scene_ids:
                continue
            scene_ids.append(scene_id)
            for key in ("hotspots", "transitions", "links"):
                rows = scene.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    target = _mapping_navigation_target(row)
                    if target:
                        navigation_hotspots += 1
                        edges.append((scene_id, target))
    elif _scene_asset_relpaths(walkable_scene):
        scene_ids = [str(walkable_scene.get("id") or "scene-1").strip() or "scene-1"]

    for key in ("hotspots", "transitions", "links"):
        rows = walkable_scene.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = _mapping_navigation_target(row)
            source = str(row.get("source") or row.get("source_scene") or (scene_ids[0] if scene_ids else "")).strip()
            if target and source:
                navigation_hotspots += 1
                edges.append((source, target))
    return {
        "scene_count": len(scene_ids),
        "navigation_hotspot_count": navigation_hotspots,
        "all_scenes_reachable": _all_reachable(scene_ids, edges),
    }


def _valid_sha256(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        return ""
    return normalized


def _valid_reviewed_at(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ""
    return raw


def _closed_object(
    value: object,
    allowed_fields: frozenset[str],
    *,
    error: str,
    errors: list[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(error)
        return {}
    if set(value) != set(allowed_fields):
        errors.append(f"{error}_fields_invalid")
    return dict(value)


def validate_panorama_spatial_provenance(
    receipt: dict[str, Any],
    *,
    provider: str,
    target_slug: str,
    artifact_kind: str,
    artifact_sha256: str,
    observed_topology: dict[str, object],
    entry_relpath: str = "",
    observed_projection: str = "",
    walkable_required: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if set(receipt) != set(_ROOT_FIELDS):
        errors.append("receipt_fields_invalid")
    artifact = _closed_object(
        receipt.get("artifact"), _ARTIFACT_FIELDS, error="artifact", errors=errors
    )
    capture = _closed_object(
        receipt.get("capture"), _CAPTURE_FIELDS, error="capture", errors=errors
    )
    authorization = _closed_object(
        receipt.get("authorization"),
        _AUTHORIZATION_FIELDS,
        error="authorization",
        errors=errors,
    )
    review = _closed_object(
        receipt.get("review"), _REVIEW_FIELDS, error="review", errors=errors
    )

    expected_provider = str(provider or "").strip().lower()
    expected_slug = str(target_slug or "").strip()
    expected_kind = str(artifact_kind or "").strip().lower()
    expected_sha256 = _valid_sha256(artifact_sha256)
    expected_entry = safe_relpath(entry_relpath) if entry_relpath else ""
    schema = str(receipt.get("schema") or "").strip()
    status = str(receipt.get("status") or "").strip().lower()
    receipt_provider = str(receipt.get("provider") or "").strip().lower()
    receipt_slug = str(receipt.get("target_slug") or "").strip()
    receipt_kind = str(artifact.get("kind") or "").strip().lower()
    receipt_sha256 = _valid_sha256(artifact.get("sha256"))
    receipt_entry = safe_relpath(artifact.get("entry_relpath"))
    source_kind = str(capture.get("source_kind") or "").strip().lower()
    projection = str(capture.get("projection") or "").strip().lower()
    scene_count = capture.get("scene_count")
    hotspot_count = capture.get("navigation_hotspot_count")
    all_reachable = capture.get("all_scenes_reachable")

    if schema != PANORAMA_SPATIAL_PROVENANCE_SCHEMA:
        errors.append("schema_invalid")
    if status != "pass":
        errors.append("status_not_pass")
    if receipt_provider != expected_provider or expected_provider not in {"pano2vr", "krpano"}:
        errors.append("provider_mismatch")
    if not expected_slug or receipt_slug != expected_slug:
        errors.append("target_slug_mismatch")
    if receipt_kind != expected_kind or expected_kind not in {"local_export", "panorama_assets"}:
        errors.append("artifact_kind_mismatch")
    if not expected_sha256 or receipt_sha256 != expected_sha256:
        errors.append("artifact_sha256_mismatch")
    if expected_entry:
        if receipt_entry != expected_entry:
            errors.append("artifact_entry_relpath_mismatch")
    elif receipt_entry:
        errors.append("artifact_entry_relpath_unexpected")
    if source_kind not in _SOURCE_KINDS:
        errors.append("capture_source_kind_invalid")
    if projection not in _PROJECTIONS:
        errors.append("capture_projection_invalid")
    if observed_projection and projection != str(observed_projection).strip().lower():
        errors.append("capture_projection_mismatch")
    if type(scene_count) is not int or scene_count < 1:
        errors.append("scene_count_invalid")
    if type(hotspot_count) is not int or hotspot_count < 0:
        errors.append("navigation_hotspot_count_invalid")
    if type(all_reachable) is not bool:
        errors.append("all_scenes_reachable_invalid")

    observed_scene_count = observed_topology.get("scene_count")
    observed_hotspot_count = observed_topology.get("navigation_hotspot_count")
    observed_reachable = observed_topology.get("all_scenes_reachable")
    if scene_count != observed_scene_count:
        errors.append("scene_count_mismatch")
    if hotspot_count != observed_hotspot_count:
        errors.append("navigation_hotspot_count_mismatch")
    if all_reachable is not observed_reachable:
        errors.append("all_scenes_reachable_mismatch")
    if walkable_required:
        if type(scene_count) is not int or scene_count < 2:
            errors.append("walkable_scene_count_insufficient")
        if type(hotspot_count) is not int or hotspot_count < 1:
            errors.append("walkable_navigation_missing")
        if all_reachable is not True:
            errors.append("walkable_scenes_not_reachable")

    if str(authorization.get("status") or "").strip().lower() != "approved":
        errors.append("authorization_not_approved")
    if not str(authorization.get("reference") or "").strip():
        errors.append("authorization_reference_missing")
    for key in ("property_match", "visual_match", "spatial_capture_match"):
        if str(review.get(key) or "").strip().lower() != "pass":
            errors.append(f"{key}_not_pass")
    if review.get("flat_composite_absent") is not True:
        errors.append("flat_composite_not_excluded")
    if not str(review.get("reviewed_by") or "").strip():
        errors.append("reviewer_missing")
    if not _valid_reviewed_at(review.get("reviewed_at")):
        errors.append("reviewed_at_invalid")

    normalized = {
        "schema": PANORAMA_SPATIAL_PROVENANCE_SCHEMA,
        "status": status,
        "provider": receipt_provider,
        "target_slug": receipt_slug,
        "artifact": {
            "kind": receipt_kind,
            "sha256": receipt_sha256,
            "entry_relpath": receipt_entry,
        },
        "capture": {
            "source_kind": source_kind,
            "projection": projection,
            "scene_count": scene_count,
            "navigation_hotspot_count": hotspot_count,
            "all_scenes_reachable": all_reachable,
        },
        "authorization": {
            "status": str(authorization.get("status") or "").strip().lower(),
            "reference": str(authorization.get("reference") or "").strip(),
        },
        "review": {
            "property_match": str(review.get("property_match") or "").strip().lower(),
            "visual_match": str(review.get("visual_match") or "").strip().lower(),
            "spatial_capture_match": str(review.get("spatial_capture_match") or "").strip().lower(),
            "flat_composite_absent": review.get("flat_composite_absent"),
            "reviewed_by": str(review.get("reviewed_by") or "").strip(),
            "reviewed_at": _valid_reviewed_at(review.get("reviewed_at")),
        },
    }
    return normalized, list(dict.fromkeys(errors))
