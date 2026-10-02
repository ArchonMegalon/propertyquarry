#!/usr/bin/env python3
"""Property-owned, fail-closed live publication exchange and rollback lane.

The materialized package and its authority receipt remain immutable inputs.
Live activation is a compare-and-swap directory exchange.  The displaced tree
is retained under a deterministic hidden name on the same filesystem so a
separately granted rollback is another atomic exchange, never a copy-back.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.materialize_property_tour_publication_package import (  # noqa: E402
    AUTHORIZED_SLUG,
    PROPERTY_REPOSITORY,
    PUBLICATION_AUTHORITY_SCHEMA,
    PUBLIC_TOUR_PACKAGE_SCHEMA,
)
from scripts.property_tour_governed_reservation import (  # noqa: E402
    require_dynamic_tour_slug,
)


PRECONDITION_SCHEMA = "propertyquarry.live-tour-publication-precondition.v1"
REPLACEMENT_GRANT_SCHEMA = "propertyquarry.live-tour-replacement-grant.v1"
PUBLICATION_RECEIPT_SCHEMA = "propertyquarry.live-tour-publication-exchange.v1"
ROLLBACK_GRANT_SCHEMA = "propertyquarry.live-tour-rollback-grant.v1"
ROLLBACK_RECEIPT_SCHEMA = "propertyquarry.live-tour-rollback-exchange.v1"
TRANSACTION_RECEIPT_SCHEMA = "propertyquarry.live-tour-exchange-transaction.v1"

OWNER = "PropertyQuarry"
AUTHORITY_SOURCE = "propertyquarry_upstream"
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_MAX_TREE_FILES = 128
_MAX_TREE_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_PUBLIC_FILE_RELPATHS = frozenset(
    {
        "tour.json",
        "generated-reconstruction/viewer.html",
        "generated-reconstruction/reconstruction.json",
        "generated-reconstruction/source-floorplan.png",
        "generated-reconstruction/vendor/three.module.js",
        (
            "generated-reconstruction/vendor/examples/jsm/controls/"
            "OrbitControls.js"
        ),
    }
)
_ASSET_RELPATHS = _PUBLIC_FILE_RELPATHS - {"tour.json"}


class LivePublicationError(RuntimeError):
    """Secret-safe failure from the live publication control lane."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileSnapshot:
    relpath: str
    content: bytes
    sha256: str
    size_bytes: int
    mode: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class DirectorySnapshot:
    relpath: str
    mode: int


@dataclass(frozen=True)
class TreeSnapshot:
    root: Path
    root_device: int
    root_inode: int
    root_mode: int
    root_mtime_ns: int
    root_ctime_ns: int
    directories: tuple[DirectorySnapshot, ...]
    files: tuple[FileSnapshot, ...]
    tree_sha256: str
    total_size_bytes: int


@dataclass(frozen=True)
class SecureFile:
    path: Path
    content: bytes
    sha256: str
    size_bytes: int
    mode: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class PackageEvidence:
    package_root: Path
    package_root_identity: tuple[int, int]
    bundle: TreeSnapshot
    authority: SecureFile
    user_instruction_sha256: str
    origin: str


def _fail(code: str) -> None:
    raise LivePublicationError(code)


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


def _strict_string(value: object, code: str) -> str:
    _require(type(value) is str, code)
    return value


def _strict_sha256(value: object, code: str) -> str:
    text = _strict_string(value, code)
    _require(bool(_SHA256_RE.fullmatch(text)), code)
    return text


def _strict_int(value: object, code: str, *, minimum: int = 0) -> int:
    _require(type(value) is int and value >= minimum, code)
    return value


def _identity(path_stat: os.stat_result) -> tuple[int, int]:
    return path_stat.st_dev, path_stat.st_ino


def _identity_row(identity: tuple[int, int]) -> dict[str, int]:
    return {"device": identity[0], "inode": identity[1]}


def _strict_identity(value: object, code: str) -> tuple[int, int]:
    _require(isinstance(value, dict) and set(value) == {"device", "inode"}, code)
    row = dict(value)
    return (
        _strict_int(row.get("device"), code),
        _strict_int(row.get("inode"), code, minimum=1),
    )


def _fingerprint(path_stat: os.stat_result) -> tuple[int, ...]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        stat.S_IMODE(path_stat.st_mode),
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    _require(
        type(nofollow) is int
        and nofollow > 0
        and type(directory) is int
        and directory > 0,
        "nofollow_unavailable",
    )
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow | directory


def _file_read_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    _require(type(nofollow) is int and nofollow > 0, "nofollow_unavailable")
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow


def _open_directory_nofollow(path: Path, *, code: str) -> int:
    absolute = _absolute(path)
    _require(absolute.is_absolute(), code)
    try:
        descriptor = os.open("/", _directory_flags())
    except OSError:
        _fail(code)
    try:
        for part in absolute.parts[1:]:
            child: int | None = None
            try:
                entry_stat = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
                child_stat = os.fstat(child)
            except OSError:
                if child is not None:
                    os.close(child)
                _fail(code)
            if not stat.S_ISDIR(entry_stat.st_mode) or _identity(entry_stat) != _identity(
                child_stat
            ):
                os.close(child)
                _fail(code)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _safe_entry_name(value: str, code: str) -> str:
    _require(
        value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
        and len(value.encode("utf-8")) <= 255
        and value.isprintable(),
        code,
    )
    return value


def _read_descriptor(descriptor: int, size: int, code: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
        except OSError:
            _fail(code)
        _require(bool(chunk), code)
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        extra = os.read(descriptor, 1)
    except OSError:
        _fail(code)
    _require(extra == b"", code)
    return b"".join(chunks)


def _snapshot_secure_file(
    path: Path,
    *,
    code: str,
    expected_mode: int | None = None,
    maximum: int = 8 * 1024 * 1024,
) -> SecureFile:
    absolute = _absolute(path)
    parent_fd = _open_directory_nofollow(absolute.parent, code=code)
    descriptor: int | None = None
    try:
        name = _safe_entry_name(absolute.name, code)
        try:
            entry_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            descriptor = os.open(name, _file_read_flags(), dir_fd=parent_fd)
            descriptor_before = os.fstat(descriptor)
        except OSError:
            _fail(code)
        _require(
            stat.S_ISREG(entry_before.st_mode)
            and stat.S_ISREG(descriptor_before.st_mode)
            and _identity(entry_before) == _identity(descriptor_before),
            code,
        )
        _require(descriptor_before.st_nlink == 1, "hardlink_forbidden")
        _require(0 < descriptor_before.st_size <= maximum, code)
        if expected_mode is not None:
            _require(stat.S_IMODE(descriptor_before.st_mode) == expected_mode, code)
        content = _read_descriptor(descriptor, descriptor_before.st_size, code)
        descriptor_after = os.fstat(descriptor)
        entry_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require(
            _fingerprint(descriptor_before)
            == _fingerprint(descriptor_after)
            == _fingerprint(entry_after),
            "filesystem_race_detected",
        )
        return SecureFile(
            path=absolute,
            content=content,
            sha256=_sha256(content),
            size_bytes=len(content),
            mode=stat.S_IMODE(descriptor_after.st_mode),
            device=descriptor_after.st_dev,
            inode=descriptor_after.st_ino,
            mtime_ns=descriptor_after.st_mtime_ns,
            ctime_ns=descriptor_after.st_ctime_ns,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _snapshot_secure_file_at(
    parent_fd: int,
    name: str,
    *,
    display_path: Path,
    code: str,
    expected_mode: int | None = None,
    maximum: int = 8 * 1024 * 1024,
) -> SecureFile:
    """Snapshot one regular file through an already-bound parent descriptor."""

    safe_name = _safe_entry_name(name, code)
    descriptor: int | None = None
    try:
        try:
            entry_before = os.stat(
                safe_name, dir_fd=parent_fd, follow_symlinks=False
            )
            descriptor = os.open(safe_name, _file_read_flags(), dir_fd=parent_fd)
            descriptor_before = os.fstat(descriptor)
        except OSError:
            _fail(code)
        _require(
            stat.S_ISREG(entry_before.st_mode)
            and stat.S_ISREG(descriptor_before.st_mode)
            and _identity(entry_before) == _identity(descriptor_before),
            code,
        )
        _require(descriptor_before.st_nlink == 1, "hardlink_forbidden")
        _require(0 < descriptor_before.st_size <= maximum, code)
        if expected_mode is not None:
            _require(stat.S_IMODE(descriptor_before.st_mode) == expected_mode, code)
        content = _read_descriptor(descriptor, descriptor_before.st_size, code)
        descriptor_after = os.fstat(descriptor)
        try:
            entry_after = os.stat(
                safe_name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError:
            _fail("filesystem_race_detected")
        _require(
            _fingerprint(descriptor_before)
            == _fingerprint(descriptor_after)
            == _fingerprint(entry_after),
            "filesystem_race_detected",
        )
        return SecureFile(
            path=_absolute(display_path),
            content=content,
            sha256=_sha256(content),
            size_bytes=len(content),
            mode=stat.S_IMODE(descriptor_after.st_mode),
            device=descriptor_after.st_dev,
            inode=descriptor_after.st_ino,
            mtime_ns=descriptor_after.st_mtime_ns,
            ctime_ns=descriptor_after.st_ctime_ns,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _snapshot_tree(
    root: Path,
    *,
    code: str,
    expected_paths: frozenset[str] | None = None,
    expected_file_mode: int | None = None,
    expected_directory_mode: int | None = None,
    _root_fd: int | None = None,
    _verify_absolute_path: bool = True,
) -> TreeSnapshot:
    absolute = _absolute(root)
    root_fd = (
        _open_directory_nofollow(absolute, code=code)
        if _root_fd is None
        else os.dup(_root_fd)
    )
    files: list[FileSnapshot] = []
    directories: list[DirectorySnapshot] = []
    total_size = 0
    root_before = os.fstat(root_fd)

    def scan(directory_fd: int, prefix: PurePosixPath) -> None:
        nonlocal total_size
        try:
            names = sorted(entry.name for entry in os.scandir(directory_fd))
        except OSError:
            _fail(code)
        for name in names:
            _safe_entry_name(name, code)
            relative = prefix / name
            relpath = relative.as_posix()
            try:
                entry_before = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError:
                _fail("filesystem_race_detected")
            if stat.S_ISLNK(entry_before.st_mode):
                _fail("symlink_forbidden")
            if stat.S_ISDIR(entry_before.st_mode):
                child: int | None = None
                try:
                    child = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    child_before = os.fstat(child)
                    _require(
                        _identity(child_before) == _identity(entry_before),
                        "filesystem_race_detected",
                    )
                    mode = stat.S_IMODE(child_before.st_mode)
                    if expected_directory_mode is not None:
                        _require(mode == expected_directory_mode, code)
                    directories.append(DirectorySnapshot(relpath=relpath, mode=mode))
                    scan(child, relative)
                    child_after = os.fstat(child)
                    entry_after = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    _require(
                        _fingerprint(child_before)
                        == _fingerprint(child_after)
                        == _fingerprint(entry_after),
                        "filesystem_race_detected",
                    )
                except OSError:
                    _fail("filesystem_race_detected")
                finally:
                    if child is not None:
                        os.close(child)
                continue
            if not stat.S_ISREG(entry_before.st_mode):
                _fail("special_file_forbidden")
            _require(entry_before.st_nlink == 1, "hardlink_forbidden")
            descriptor: int | None = None
            try:
                descriptor = os.open(name, _file_read_flags(), dir_fd=directory_fd)
                descriptor_before = os.fstat(descriptor)
                _require(
                    stat.S_ISREG(descriptor_before.st_mode)
                    and descriptor_before.st_nlink == 1
                    and _identity(descriptor_before) == _identity(entry_before),
                    "filesystem_race_detected",
                )
                _require(
                    0 <= descriptor_before.st_size <= _MAX_TREE_BYTES,
                    "tree_size_invalid",
                )
                mode = stat.S_IMODE(descriptor_before.st_mode)
                if expected_file_mode is not None:
                    _require(mode == expected_file_mode, code)
                content = _read_descriptor(
                    descriptor, descriptor_before.st_size, "filesystem_race_detected"
                )
                descriptor_after = os.fstat(descriptor)
                entry_after = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                _require(
                    _fingerprint(descriptor_before)
                    == _fingerprint(descriptor_after)
                    == _fingerprint(entry_after),
                    "filesystem_race_detected",
                )
                files.append(
                    FileSnapshot(
                        relpath=relpath,
                        content=content,
                        sha256=_sha256(content),
                        size_bytes=len(content),
                        mode=mode,
                        device=descriptor_after.st_dev,
                        inode=descriptor_after.st_ino,
                        mtime_ns=descriptor_after.st_mtime_ns,
                        ctime_ns=descriptor_after.st_ctime_ns,
                    )
                )
                total_size += len(content)
                _require(
                    len(files) <= _MAX_TREE_FILES
                    and total_size <= _MAX_TREE_BYTES,
                    "tree_size_invalid",
                )
            except OSError:
                _fail("filesystem_race_detected")
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    try:
        root_mode = stat.S_IMODE(root_before.st_mode)
        if expected_directory_mode is not None:
            _require(root_mode == expected_directory_mode, code)
        directories.append(DirectorySnapshot(relpath=".", mode=root_mode))
        scan(root_fd, PurePosixPath())
        root_after = os.fstat(root_fd)
        _require(
            _fingerprint(root_before) == _fingerprint(root_after),
            "filesystem_race_detected",
        )
    finally:
        os.close(root_fd)
    if _verify_absolute_path:
        verification_fd = _open_directory_nofollow(absolute, code=code)
        try:
            verification = os.fstat(verification_fd)
            _require(
                _fingerprint(verification) == _fingerprint(root_before),
                "filesystem_race_detected",
            )
        finally:
            os.close(verification_fd)
    paths = frozenset(row.relpath for row in files)
    if expected_paths is not None:
        _require(paths == expected_paths, code)
    tree_payload = {
        "directories": [
            {"mode": row.mode, "path": row.relpath}
            for row in sorted(directories, key=lambda item: item.relpath)
        ],
        "files": [
            {
                "mode": row.mode,
                "path": row.relpath,
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
            }
            for row in sorted(files, key=lambda item: item.relpath)
        ],
    }
    return TreeSnapshot(
        root=absolute,
        root_device=root_before.st_dev,
        root_inode=root_before.st_ino,
        root_mode=stat.S_IMODE(root_before.st_mode),
        root_mtime_ns=root_before.st_mtime_ns,
        root_ctime_ns=root_before.st_ctime_ns,
        directories=tuple(sorted(directories, key=lambda item: item.relpath)),
        files=tuple(sorted(files, key=lambda item: item.relpath)),
        tree_sha256=_sha256(_canonical_json_bytes(tree_payload)),
        total_size_bytes=total_size,
    )


def _snapshot_tree_at(
    parent_fd: int,
    name: str,
    *,
    display_root: Path,
    code: str,
) -> TreeSnapshot:
    _safe_entry_name(name, code)
    child_fd: int | None = None
    try:
        entry_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        child_before = os.fstat(child_fd)
        _require(
            stat.S_ISDIR(entry_before.st_mode)
            and _identity(entry_before) == _identity(child_before),
            code,
        )
        snapshot = _snapshot_tree(
            display_root,
            code=code,
            _root_fd=child_fd,
            _verify_absolute_path=False,
        )
        child_after = os.fstat(child_fd)
        entry_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require(
            _fingerprint(child_before)
            == _fingerprint(child_after)
            == _fingerprint(entry_after),
            "filesystem_race_detected",
        )
        return snapshot
    except OSError:
        _fail(code)
    finally:
        if child_fd is not None:
            os.close(child_fd)


def _require_directory_path_identity(
    path: Path, expected_identity: tuple[int, int], code: str
) -> None:
    descriptor = _open_directory_nofollow(path, code=code)
    try:
        _require(_identity(os.fstat(descriptor)) == expected_identity, code)
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "json_duplicate_key")
        result[key] = value
    return result


def _parse_json_object(content: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except LivePublicationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    _require(isinstance(value, dict), code)
    return dict(value)


def _canonical_origin(value: object) -> str:
    text = _strict_string(value, "origin_invalid")
    parsed = urllib.parse.urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        _fail("origin_invalid")
    hostname = parsed.hostname
    _require(
        parsed.scheme == "https"
        and type(hostname) is str
        and bool(hostname)
        and hostname == hostname.lower()
        and not hostname.endswith(".")
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and text == f"https://{hostname}",
        "origin_invalid",
    )
    return text


def _safe_slug(value: object) -> str:
    slug = _strict_string(value, "slug_invalid")
    _require(bool(_SAFE_NAME_RE.fullmatch(slug)), "slug_invalid")
    _require(slug == AUTHORIZED_SLUG, "slug_unauthorized")
    try:
        require_dynamic_tour_slug(slug)
    except RuntimeError as exc:
        raise LivePublicationError(str(exc)) from exc
    return slug


def _logical_volume(value: object) -> str:
    name = _strict_string(value, "logical_volume_invalid")
    _require(bool(_SAFE_NAME_RE.fullmatch(name)), "logical_volume_invalid")
    return name


def _destination(value: object, slug: str) -> str:
    destination = _strict_string(value, "destination_invalid")
    _require(destination == slug, "destination_invalid")
    return destination


def _get_dict(payload: dict[str, Any], key: str, code: str) -> dict[str, Any]:
    value = payload.get(key)
    _require(isinstance(value, dict), code)
    return dict(value)


def _validate_asset_bindings(
    raw: object, bundle: TreeSnapshot, *, code: str
) -> list[dict[str, Any]]:
    _require(isinstance(raw, list) and len(raw) == len(_ASSET_RELPATHS), code)
    files = {row.relpath: row for row in bundle.files}
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw:
        _require(
            isinstance(value, dict)
            and set(value)
            == {"mime_type", "path", "role", "sha256", "size_bytes"},
            code,
        )
        row = dict(value)
        path = _strict_string(row.get("path"), code)
        digest = _strict_sha256(row.get("sha256"), code)
        size = _strict_int(row.get("size_bytes"), code, minimum=1)
        _strict_string(row.get("mime_type"), code)
        _strict_string(row.get("role"), code)
        file_row = files.get(path)
        _require(
            path in _ASSET_RELPATHS
            and path not in seen
            and file_row is not None
            and digest == file_row.sha256
            and size == file_row.size_bytes,
            code,
        )
        seen.add(path)
        bindings.append(row)
    _require(seen == _ASSET_RELPATHS, code)
    return bindings


def _inspect_package(
    package_root: Path,
    *,
    slug: str,
    origin: str,
    user_instruction_sha256: str,
) -> PackageEvidence:
    root = _absolute(package_root)
    root_fd = _open_directory_nofollow(root, code="package_root_invalid")
    try:
        root_identity = _identity(os.fstat(root_fd))
    finally:
        os.close(root_fd)
    bundle = _snapshot_tree(
        root / "public_property_tours" / slug,
        code="package_tree_invalid",
        expected_paths=_PUBLIC_FILE_RELPATHS,
        expected_file_mode=0o644,
        expected_directory_mode=0o755,
    )
    authority = _snapshot_secure_file(
        root / "publication-authority" / f"{slug}.json",
        code="authority_receipt_invalid",
        expected_mode=0o600,
    )
    authority_payload = _parse_json_object(
        authority.content, "authority_receipt_invalid"
    )
    _require(
        authority_payload.get("schema") == PUBLICATION_AUTHORITY_SCHEMA
        and authority_payload.get("status") == "authorized"
        and authority_payload.get("owner") == OWNER
        and authority_payload.get("repository") == PROPERTY_REPOSITORY
        and authority_payload.get("slug") == slug
        and authority_payload.get("public_activation_authority") is True
        and authority_payload.get("publication_authority_verified") is True,
        "authority_receipt_invalid",
    )
    source_user_hash = _strict_sha256(
        authority_payload.get("user_instruction_sha256"),
        "authority_user_instruction_invalid",
    )
    _require(
        source_user_hash == user_instruction_sha256,
        "authority_user_instruction_mismatch",
    )
    origins = authority_payload.get("allowed_public_origins")
    _require(isinstance(origins, list) and bool(origins), "authority_origin_invalid")
    validated_origins = [
        _canonical_origin(item) for item in origins
    ]
    _require(len(set(validated_origins)) == len(validated_origins), "authority_origin_invalid")
    _require(origin in validated_origins, "origin_unauthorized")
    package = _get_dict(authority_payload, "package", "authority_package_invalid")
    public_paths = package.get("public_file_relpaths")
    _require(
        isinstance(public_paths, list)
        and all(type(item) is str for item in public_paths)
        and public_paths == sorted(_PUBLIC_FILE_RELPATHS)
        and type(package.get("public_file_count")) is int
        and package.get("public_file_count") == 6
        and package.get("public_bundle_relpath")
        == f"public_property_tours/{slug}",
        "authority_package_invalid",
    )
    authority_bindings = _validate_asset_bindings(
        package.get("asset_bindings"), bundle, code="authority_package_invalid"
    )
    tour_file = next(row for row in bundle.files if row.relpath == "tour.json")
    tour = _parse_json_object(tour_file.content, "tour_manifest_invalid")
    release = _get_dict(tour, "generated_viewer_release", "tour_manifest_invalid")
    _require(
        tour.get("schema") == PUBLIC_TOUR_PACKAGE_SCHEMA
        and tour.get("slug") == slug
        and release.get("status") == "ready"
        and release.get("public_activation_authority") is True
        and release.get("publication_authority_verified") is True
        and release.get("revoked") is False
        and release.get("disqualified") is False
        and _strict_sha256(
            release.get("publication_authority_receipt_sha256"),
            "tour_manifest_invalid",
        )
        == authority.sha256,
        "tour_manifest_invalid",
    )
    tour_bindings = _validate_asset_bindings(
        release.get("asset_bindings"), bundle, code="tour_manifest_invalid"
    )
    _require(tour_bindings == authority_bindings, "tour_manifest_invalid")
    return PackageEvidence(
        package_root=root,
        package_root_identity=root_identity,
        bundle=bundle,
        authority=authority,
        user_instruction_sha256=source_user_hash,
        origin=origin,
    )


def _tree_receipt(snapshot: TreeSnapshot) -> dict[str, object]:
    return {
        "file_count": len(snapshot.files),
        "root_identity": _identity_row(
            (snapshot.root_device, snapshot.root_inode)
        ),
        "sha256": snapshot.tree_sha256,
        "total_size_bytes": snapshot.total_size_bytes,
    }


def _paths_disjoint(left: Path, right: Path) -> bool:
    left = _absolute(left)
    right = _absolute(right)
    return not (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _ensure_private_receipt_root(
    receipt_root: Path,
    *,
    live_volume_root: Path,
    package_root: Path | None,
) -> Path:
    root = _absolute(receipt_root)
    _require(_paths_disjoint(root, live_volume_root), "receipt_root_inside_served_tree")
    if package_root is not None:
        _require(
            _paths_disjoint(root, package_root),
            "receipt_root_inside_served_tree",
        )
    if not root.exists():
        parent_fd = _open_directory_nofollow(
            root.parent, code="receipt_root_invalid"
        )
        try:
            try:
                os.mkdir(root.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                _fail("receipt_root_invalid")
        finally:
            os.close(parent_fd)
    descriptor = _open_directory_nofollow(root, code="receipt_root_invalid")
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_IMODE(metadata.st_mode) == 0o700
            and metadata.st_uid == os.geteuid(),
            "receipt_root_invalid",
        )
    finally:
        os.close(descriptor)
    return root


def inspect_live_precondition(
    *,
    package_root: Path,
    live_volume_root: Path,
    receipt_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
    user_instruction_sha256: str,
) -> dict[str, object]:
    """Read live and package state without mutating the served filesystem."""

    normalized_origin = _canonical_origin(origin)
    normalized_volume = _logical_volume(logical_volume)
    normalized_slug = _safe_slug(slug)
    normalized_destination = _destination(destination_relpath, normalized_slug)
    instruction_hash = _strict_sha256(
        user_instruction_sha256, "user_instruction_hash_invalid"
    )
    package = _inspect_package(
        package_root,
        slug=normalized_slug,
        origin=normalized_origin,
        user_instruction_sha256=instruction_hash,
    )
    volume_root = _absolute(live_volume_root)
    _require(
        _paths_disjoint(package.package_root, volume_root),
        "package_inside_live_volume",
    )
    control_root = _ensure_private_receipt_root(
        receipt_root,
        live_volume_root=volume_root,
        package_root=package.package_root,
    )
    control_fd = _open_directory_nofollow(
        control_root, code="receipt_root_invalid"
    )
    try:
        control_identity = _identity(os.fstat(control_fd))
    finally:
        os.close(control_fd)
    volume_fd = _open_directory_nofollow(volume_root, code="live_volume_invalid")
    try:
        volume_stat = os.fstat(volume_fd)
        volume_identity = _identity(volume_stat)
    finally:
        os.close(volume_fd)
    old_tree = _snapshot_tree(
        volume_root / normalized_destination, code="live_destination_invalid"
    )
    _require(old_tree.root_device == volume_identity[0], "live_device_mismatch")
    return {
        "schema": PRECONDITION_SCHEMA,
        "status": "pass",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": "replace",
        "origin": normalized_origin,
        "logical_volume": normalized_volume,
        "live_volume_root": str(volume_root),
        "live_volume_root_identity": _identity_row(volume_identity),
        "receipt_root": str(control_root),
        "receipt_root_identity": _identity_row(control_identity),
        "slug": normalized_slug,
        "destination_relpath": normalized_destination,
        "destination_path": str(volume_root / normalized_destination),
        "old_tree": _tree_receipt(old_tree),
        "replacement_package": {
            "package_root": str(package.package_root),
            "package_root_identity": _identity_row(package.package_root_identity),
            "bundle_path": str(package.bundle.root),
            "bundle_root_identity": _identity_row(
                (package.bundle.root_device, package.bundle.root_inode)
            ),
            "tree_sha256": package.bundle.tree_sha256,
            "file_count": len(package.bundle.files),
            "authority_receipt_path": str(package.authority.path),
            "authority_receipt_sha256": package.authority.sha256,
            "authority_receipt_identity": _identity_row(
                (package.authority.device, package.authority.inode)
            ),
            "user_instruction_sha256": package.user_instruction_sha256,
        },
        "public_activation_authority": True,
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }


def _exact_match(actual: object, expected: object, code: str) -> None:
    _require(type(actual) is type(expected), code)
    if isinstance(expected, dict):
        _require(set(actual) == set(expected), code)  # type: ignore[arg-type]
        for key, expected_value in expected.items():
            _exact_match(actual[key], expected_value, code)  # type: ignore[index]
    elif isinstance(expected, list):
        _require(len(actual) == len(expected), code)  # type: ignore[arg-type]
        for actual_value, expected_value in zip(actual, expected):  # type: ignore[arg-type]
            _exact_match(actual_value, expected_value, code)
    else:
        _require(actual == expected, code)


def _receipt_path(path: Path, receipt_root: Path, code: str) -> Path:
    absolute = _absolute(path)
    _require(absolute.parent == receipt_root, code)
    _safe_entry_name(absolute.name, code)
    return absolute


def _write_bytes_exclusive_at(
    parent_fd: int,
    name: str,
    *,
    display_path: Path,
    content: bytes,
    mode: int,
    code: str,
    fsync_parent: bool = True,
) -> SecureFile:
    safe_name = _safe_entry_name(name, code)
    descriptor: int | None = None
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            descriptor = os.open(safe_name, flags, mode, dir_fd=parent_fd)
            written = 0
            while written < len(content):
                count = os.write(descriptor, content[written:])
                _require(count > 0, code)
                written += count
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            before_read = os.fstat(descriptor)
            _require(
                stat.S_ISREG(before_read.st_mode)
                and before_read.st_nlink == 1
                and stat.S_IMODE(before_read.st_mode) == mode
                and before_read.st_size == len(content),
                code,
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = _read_descriptor(descriptor, len(content), code)
            after_read = os.fstat(descriptor)
            entry = os.stat(safe_name, dir_fd=parent_fd, follow_symlinks=False)
            _require(
                observed == content
                and _fingerprint(before_read)
                == _fingerprint(after_read)
                == _fingerprint(entry),
                code,
            )
            if fsync_parent:
                os.fsync(parent_fd)
        except OSError:
            _fail(code)
        return SecureFile(
            path=_absolute(display_path),
            content=content,
            sha256=_sha256(content),
            size_bytes=len(content),
            mode=stat.S_IMODE(after_read.st_mode),
            device=after_read.st_dev,
            inode=after_read.st_ino,
            mtime_ns=after_read.st_mtime_ns,
            ctime_ns=after_read.st_ctime_ns,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_bytes_exclusive(path: Path, content: bytes, *, mode: int, code: str) -> None:
    parent_fd = _open_directory_nofollow(path.parent, code=code)
    descriptor: int | None = None
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            descriptor = os.open(path.name, flags, mode, dir_fd=parent_fd)
            written = 0
            while written < len(content):
                count = os.write(descriptor, content[written:])
                _require(count > 0, code)
                written += count
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            _require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == mode,
                code,
            )
            os.fsync(parent_fd)
        except OSError:
            _fail(code)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def write_precondition_receipt(
    receipt: dict[str, object], *, path: Path, receipt_root: Path
) -> SecureFile:
    root = _absolute(receipt_root)
    target = _receipt_path(path, root, "precondition_receipt_path_invalid")
    _write_bytes_exclusive(
        target,
        _json_file_bytes(receipt),
        mode=0o600,
        code="precondition_receipt_write_failed",
    )
    return _snapshot_secure_file(
        target, code="precondition_receipt_invalid", expected_mode=0o600
    )


def _replacement_binding(
    precondition: dict[str, object], precondition_sha256: str
) -> dict[str, object]:
    package = dict(precondition["replacement_package"])  # type: ignore[arg-type]
    old_tree = dict(precondition["old_tree"])  # type: ignore[arg-type]
    return {
        "precondition_receipt_sha256": precondition_sha256,
        "origin": precondition["origin"],
        "logical_volume": precondition["logical_volume"],
        "live_volume_root": precondition["live_volume_root"],
        "live_volume_root_identity": precondition["live_volume_root_identity"],
        "receipt_root": precondition["receipt_root"],
        "receipt_root_identity": precondition["receipt_root_identity"],
        "slug": precondition["slug"],
        "destination_relpath": precondition["destination_relpath"],
        "old_tree_sha256": old_tree["sha256"],
        "old_tree_root_identity": old_tree["root_identity"],
        "replacement_tree_sha256": package["tree_sha256"],
        "replacement_file_count": package["file_count"],
        "replacement_bundle_root_identity": package["bundle_root_identity"],
        "authority_receipt_sha256": package["authority_receipt_sha256"],
        "authority_receipt_identity": package["authority_receipt_identity"],
        "user_instruction_sha256": package["user_instruction_sha256"],
    }


def create_replacement_grant(
    *,
    precondition_receipt_path: Path,
    grant_path: Path,
    package_root: Path,
    live_volume_root: Path,
    receipt_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
    user_instruction_sha256: str,
) -> dict[str, object]:
    current = inspect_live_precondition(
        package_root=package_root,
        live_volume_root=live_volume_root,
        receipt_root=receipt_root,
        origin=origin,
        logical_volume=logical_volume,
        slug=slug,
        destination_relpath=destination_relpath,
        user_instruction_sha256=user_instruction_sha256,
    )
    root = _absolute(receipt_root)
    precondition_path = _receipt_path(
        precondition_receipt_path, root, "precondition_receipt_path_invalid"
    )
    precondition_file = _snapshot_secure_file(
        precondition_path,
        code="precondition_receipt_invalid",
        expected_mode=0o600,
    )
    supplied = _parse_json_object(
        precondition_file.content, "precondition_receipt_invalid"
    )
    _exact_match(supplied, current, "precondition_receipt_stale")
    grant_id = secrets.token_hex(32)
    grant = {
        "schema": REPLACEMENT_GRANT_SCHEMA,
        "status": "authorized",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": "replace",
        "grant_id": grant_id,
        "single_use": True,
        "required_mode": 0o600,
        "binding": _replacement_binding(current, precondition_file.sha256),
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }
    target = _receipt_path(grant_path, root, "replacement_grant_path_invalid")
    _write_bytes_exclusive(
        target,
        _json_file_bytes(grant),
        mode=0o600,
        code="replacement_grant_write_failed",
    )
    return grant


def _renameat2(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    flags: int,
    code: str,
) -> None:
    _safe_entry_name(source_name, code)
    _safe_entry_name(destination_name, code)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    _require(function is not None, "renameat2_unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        _fail(
            "single_use_grant_already_claimed"
            if flags == _RENAME_NOREPLACE and code == "grant_claim_failed"
            else code
        )
    _fail(code)


def _ensure_private_child_directory(root: Path, name: str) -> Path:
    _safe_entry_name(name, "receipt_ledger_invalid")
    root_fd = _open_directory_nofollow(root, code="receipt_ledger_invalid")
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        except OSError:
            _fail("receipt_ledger_invalid")
    finally:
        os.close(root_fd)
    child = root / name
    descriptor = _open_directory_nofollow(child, code="receipt_ledger_invalid")
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_IMODE(metadata.st_mode) == 0o700
            and metadata.st_uid == os.geteuid(),
            "receipt_ledger_invalid",
        )
    finally:
        os.close(descriptor)
    return child


def _claim_grant(
    *,
    grant_path: Path,
    receipt_root: Path,
    grant: dict[str, Any],
    expected_sha256: str,
) -> SecureFile:
    grant_id = _strict_sha256(grant.get("grant_id"), "grant_id_invalid")
    source = _receipt_path(grant_path, receipt_root, "grant_path_invalid")
    source_file = _snapshot_secure_file(
        source, code="grant_invalid", expected_mode=0o600
    )
    _require(source_file.sha256 == expected_sha256, "grant_race_detected")
    ledger = _ensure_private_child_directory(receipt_root, "used-grants")
    destination = _claimed_grant_path(
        receipt_root,
        operation=_strict_string(grant.get("operation"), "grant_invalid"),
        grant_id=grant_id,
    )
    source_parent_fd = _open_directory_nofollow(
        receipt_root, code="grant_claim_failed"
    )
    destination_parent_fd = _open_directory_nofollow(
        ledger, code="grant_claim_failed"
    )
    try:
        before = os.stat(source.name, dir_fd=source_parent_fd, follow_symlinks=False)
        _require(_identity(before) == (source_file.device, source_file.inode), "grant_race_detected")
        _renameat2(
            source_parent_fd,
            source.name,
            destination_parent_fd,
            destination.name,
            _RENAME_NOREPLACE,
            "grant_claim_failed",
        )
        os.fsync(source_parent_fd)
        os.fsync(destination_parent_fd)
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)
    claimed = _snapshot_secure_file(
        destination, code="grant_claim_failed", expected_mode=0o600
    )
    _require(
        (claimed.device, claimed.inode, claimed.sha256)
        == (source_file.device, source_file.inode, source_file.sha256),
        "grant_race_detected",
    )
    return claimed


def _claimed_grant_path(
    receipt_root: Path, *, operation: str, grant_id: str
) -> Path:
    _require(operation in {"replace", "rollback"}, "grant_invalid")
    _strict_sha256(grant_id, "grant_id_invalid")
    return receipt_root / "used-grants" / f"{operation}-{grant_id}.json"


def _transaction_retained_path(
    receipt_root: Path, *, operation: str, grant_id: str
) -> Path:
    _require(operation in {"replace", "rollback"}, "transaction_receipt_invalid")
    _strict_sha256(grant_id, "transaction_receipt_invalid")
    return receipt_root / f".property-live-{operation}-transaction-{grant_id}.json"


def _build_prepared_transaction(
    *,
    operation: str,
    origin: str,
    logical_volume: str,
    live_volume_root: Path,
    live_volume_root_identity: tuple[int, int],
    receipt_root: Path,
    receipt_root_identity: tuple[int, int],
    slug: str,
    destination_relpath: str,
    peer_relpath: str,
    destination_before: TreeSnapshot,
    peer_before: TreeSnapshot,
    grant: dict[str, Any],
    grant_sha256: str,
    final_receipt_path: Path,
) -> dict[str, object]:
    grant_id = _strict_sha256(grant.get("grant_id"), "grant_id_invalid")
    retained_path = _transaction_retained_path(
        receipt_root, operation=operation, grant_id=grant_id
    )
    claimed_path = _claimed_grant_path(
        receipt_root, operation=operation, grant_id=grant_id
    )
    binding = grant.get("binding")
    _require(isinstance(binding, dict), "grant_invalid")
    return {
        "schema": TRANSACTION_RECEIPT_SCHEMA,
        "status": "prepared",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": operation,
        "origin": origin,
        "logical_volume": logical_volume,
        "live_volume_root": str(live_volume_root),
        "live_volume_root_identity": _identity_row(live_volume_root_identity),
        "receipt_root": str(receipt_root),
        "receipt_root_identity": _identity_row(receipt_root_identity),
        "slug": slug,
        "destination_relpath": destination_relpath,
        "peer_relpath": peer_relpath,
        "grant_schema": grant["schema"],
        "grant_id": grant_id,
        "grant_sha256": grant_sha256,
        "grant_binding": dict(binding),
        "expected_consumed_grant_path": str(claimed_path),
        "final_receipt_path": str(final_receipt_path),
        "retained_transaction_path": str(retained_path),
        "destination_before": _tree_receipt(destination_before),
        "peer_before": _tree_receipt(peer_before),
        "destination_after": _tree_receipt(peer_before),
        "peer_after": _tree_receipt(destination_before),
        "atomic_exchange_planned": True,
        "recovery_evidence_durable_before_exchange": True,
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }


def _reserve_prepared_transaction(
    *,
    final_receipt_path: Path,
    receipt_root: Path,
    transaction: dict[str, object],
    code: str,
) -> SecureFile:
    target = _receipt_path(final_receipt_path, receipt_root, code)
    _write_bytes_exclusive(
        target,
        _json_file_bytes(transaction),
        mode=0o600,
        code=code,
    )
    return _snapshot_secure_file(target, code=code, expected_mode=0o600)


def _before_receipt_exchange_hook(
    _receipt_root: Path, _target_name: str, _candidate_name: str, _code: str
) -> None:
    """Test seam immediately before the final receipt name exchange."""


def _after_receipt_exchange_hook(
    _receipt_root: Path, _target_name: str, _candidate_name: str, _code: str
) -> None:
    """Test seam after receipt exchange and before verification/durability."""


def _fsync_final_receipt_transition(root_fd: int) -> None:
    """Durability seam kept separate for exact post-exchange fault injection."""

    os.fsync(root_fd)


def _secure_file_matches(actual: SecureFile, expected: SecureFile) -> bool:
    return (
        actual.device,
        actual.inode,
        actual.sha256,
        actual.content,
        actual.size_bytes,
    ) == (
        expected.device,
        expected.inode,
        expected.sha256,
        expected.content,
        expected.size_bytes,
    )


def _requested_receipt_contains_prepared(
    target: Path, prepared: SecureFile
) -> bool:
    try:
        observed = _snapshot_secure_file(
            target,
            code="transaction_receipt_recovery_failed",
            expected_mode=0o600,
        )
    except LivePublicationError:
        return False
    return (
        observed.sha256,
        observed.content,
        observed.size_bytes,
    ) == (prepared.sha256, prepared.content, prepared.size_bytes)


def _restore_bound_prepared_candidate(
    *,
    root_fd: int,
    target: Path,
    retained: Path,
    prepared: SecureFile,
    completed: SecureFile | None,
) -> None:
    """Best-effort exchange-back only when both bound names are exact."""

    if completed is None:
        return
    try:
        current_target = _snapshot_secure_file_at(
            root_fd,
            target.name,
            display_path=target,
            code="transaction_receipt_recovery_failed",
            expected_mode=0o600,
        )
        current_retained = _snapshot_secure_file_at(
            root_fd,
            retained.name,
            display_path=retained,
            code="transaction_receipt_recovery_failed",
            expected_mode=0o600,
        )
        if not (
            _secure_file_matches(current_target, completed)
            and _secure_file_matches(current_retained, prepared)
        ):
            return
        try:
            _renameat2(
                root_fd,
                target.name,
                root_fd,
                retained.name,
                _RENAME_EXCHANGE,
                "transaction_receipt_recovery_failed",
            )
        except LivePublicationError:
            pass
        try:
            os.fsync(root_fd)
        except OSError:
            pass
    except (LivePublicationError, OSError):
        return


def _restore_prepared_transaction_receipt(
    *,
    final_receipt_path: Path,
    receipt_root: Path,
    prepared: SecureFile,
) -> None:
    """Force known prepared bytes back to the requested name without deletion."""

    target = _receipt_path(
        final_receipt_path, receipt_root, "transaction_receipt_recovery_failed"
    )
    root_fd = _open_directory_nofollow(
        receipt_root, code="transaction_receipt_recovery_failed"
    )
    try:
        root_metadata = os.fstat(root_fd)
        _require(
            stat.S_IMODE(root_metadata.st_mode) == 0o700
            and root_metadata.st_uid == os.geteuid(),
            "transaction_receipt_recovery_failed",
        )
        recovery_name = (
            ".property-live-receipt-recovery-"
            f"{prepared.sha256[:16]}-{secrets.token_hex(16)}.json"
        )
        recovery_path = receipt_root / recovery_name
        recovery = _write_bytes_exclusive_at(
            root_fd,
            recovery_name,
            display_path=recovery_path,
            content=prepared.content,
            mode=0o600,
            code="transaction_receipt_recovery_failed",
            fsync_parent=False,
        )
        target_exists = True
        try:
            target_entry = os.stat(
                target.name, dir_fd=root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            target_exists = False
            target_entry = None
        except OSError:
            _fail("transaction_receipt_recovery_failed")
        if target_exists:
            _require(
                target_entry is not None
                and _identity(target_entry) != (recovery.device, recovery.inode),
                "transaction_receipt_recovery_failed",
            )
            _renameat2(
                root_fd,
                recovery_name,
                root_fd,
                target.name,
                _RENAME_EXCHANGE,
                "transaction_receipt_recovery_failed",
            )
        else:
            try:
                _renameat2(
                    root_fd,
                    recovery_name,
                    root_fd,
                    target.name,
                    _RENAME_NOREPLACE,
                    "transaction_receipt_recovery_failed",
                )
            except LivePublicationError:
                try:
                    os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
                except OSError:
                    _fail("transaction_receipt_recovery_failed")
                _renameat2(
                    root_fd,
                    recovery_name,
                    root_fd,
                    target.name,
                    _RENAME_EXCHANGE,
                    "transaction_receipt_recovery_failed",
                )
        try:
            os.fsync(root_fd)
        except OSError:
            _fail("transaction_receipt_recovery_failed")
        restored = _snapshot_secure_file_at(
            root_fd,
            target.name,
            display_path=target,
            code="transaction_receipt_recovery_failed",
            expected_mode=0o600,
        )
        _require(
            (
                restored.sha256,
                restored.content,
                restored.size_bytes,
            )
            == (prepared.sha256, prepared.content, prepared.size_bytes),
            "transaction_receipt_recovery_failed",
        )
    finally:
        os.close(root_fd)
    absolute_restored = _snapshot_secure_file(
        target,
        code="transaction_receipt_recovery_failed",
        expected_mode=0o600,
    )
    _require(
        (
            absolute_restored.sha256,
            absolute_restored.content,
            absolute_restored.size_bytes,
        )
        == (prepared.sha256, prepared.content, prepared.size_bytes),
        "transaction_receipt_recovery_failed",
    )


def _finalize_transaction_receipt(
    *,
    final_receipt_path: Path,
    retained_transaction_path: Path,
    receipt_root: Path,
    prepared: SecureFile,
    final_receipt: dict[str, object],
    live_volume_root: Path,
    expected_live_volume_identity: tuple[int, int],
    expected_receipt_root_identity: tuple[int, int],
    code: str,
) -> None:
    target = _receipt_path(final_receipt_path, receipt_root, code)
    retained = _receipt_path(retained_transaction_path, receipt_root, code)
    root_fd: int | None = None
    completed: SecureFile | None = None
    try:
        root_fd = _open_directory_nofollow(receipt_root, code=code)
        _require(
            _identity(os.fstat(root_fd)) == expected_receipt_root_identity,
            "receipt_root_identity_drift",
        )
        _require_directory_path_identity(
            receipt_root,
            expected_receipt_root_identity,
            "receipt_root_identity_drift",
        )
        target_before = _snapshot_secure_file_at(
            root_fd,
            target.name,
            display_path=target,
            code="transaction_receipt_race_detected",
            expected_mode=0o600,
        )
        _require(
            (
                target_before.device,
                target_before.inode,
                target_before.sha256,
            )
            == (prepared.device, prepared.inode, prepared.sha256),
            "transaction_receipt_race_detected",
        )
        completed = _write_bytes_exclusive_at(
            root_fd,
            retained.name,
            display_path=retained,
            content=_json_file_bytes(final_receipt),
            mode=0o600,
            code=code,
        )
        _require_directory_path_identity(
            live_volume_root,
            expected_live_volume_identity,
            "live_volume_identity_drift",
        )
        _require_directory_path_identity(
            receipt_root,
            expected_receipt_root_identity,
            "receipt_root_identity_drift",
        )
        target_entry = os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
        retained_entry = os.stat(
            retained.name, dir_fd=root_fd, follow_symlinks=False
        )
        _require(
            _identity(target_entry) == (prepared.device, prepared.inode)
            and _identity(retained_entry) == (completed.device, completed.inode),
            "transaction_receipt_race_detected",
        )
        _before_receipt_exchange_hook(
            receipt_root, target.name, retained.name, code
        )
        _renameat2(
            root_fd,
            target.name,
            root_fd,
            retained.name,
            _RENAME_EXCHANGE,
            code,
        )
        _after_receipt_exchange_hook(
            receipt_root, target.name, retained.name, code
        )
        final_file = _snapshot_secure_file_at(
            root_fd,
            target.name,
            display_path=target,
            code="transaction_receipt_race_detected",
            expected_mode=0o600,
        )
        retained_prepared = _snapshot_secure_file_at(
            root_fd,
            retained.name,
            display_path=retained,
            code="transaction_receipt_race_detected",
            expected_mode=0o600,
        )
        _require(
            (final_file.device, final_file.inode, final_file.sha256)
            == (completed.device, completed.inode, completed.sha256)
            and (
                retained_prepared.device,
                retained_prepared.inode,
                retained_prepared.sha256,
            )
            == (prepared.device, prepared.inode, prepared.sha256),
            "transaction_receipt_race_detected",
        )
        _fsync_final_receipt_transition(root_fd)
        final_after_fsync = _snapshot_secure_file_at(
            root_fd,
            target.name,
            display_path=target,
            code="transaction_receipt_race_detected",
            expected_mode=0o600,
        )
        prepared_after_fsync = _snapshot_secure_file_at(
            root_fd,
            retained.name,
            display_path=retained,
            code="transaction_receipt_race_detected",
            expected_mode=0o600,
        )
        _require(
            (
                final_after_fsync.device,
                final_after_fsync.inode,
                final_after_fsync.sha256,
            )
            == (completed.device, completed.inode, completed.sha256)
            and (
                prepared_after_fsync.device,
                prepared_after_fsync.inode,
                prepared_after_fsync.sha256,
            )
            == (prepared.device, prepared.inode, prepared.sha256),
            "transaction_receipt_race_detected",
        )
        _require_directory_path_identity(
            live_volume_root,
            expected_live_volume_identity,
            "live_volume_identity_drift",
        )
        _require_directory_path_identity(
            receipt_root,
            expected_receipt_root_identity,
            "receipt_root_identity_drift",
        )
    except BaseException as failure:
        if root_fd is not None:
            _restore_bound_prepared_candidate(
                root_fd=root_fd,
                target=target,
                retained=retained,
                prepared=prepared,
                completed=completed,
            )
            os.close(root_fd)
            root_fd = None
        if not _requested_receipt_contains_prepared(target, prepared):
            try:
                _restore_prepared_transaction_receipt(
                    final_receipt_path=target,
                    receipt_root=receipt_root,
                    prepared=prepared,
                )
            except BaseException:
                if not _requested_receipt_contains_prepared(target, prepared):
                    _fail("transaction_receipt_recovery_failed")
        if isinstance(failure, LivePublicationError):
            raise failure
        _fail(code)
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _open_relative_directory(root_fd: int, relpath: PurePosixPath) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in relpath.parts:
            if part in {"", "."}:
                continue
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            entry = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            _require(_identity(entry) == _identity(os.fstat(child)), "staging_race_detected")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _copy_package_to_hidden_stage(
    *,
    volume_root: Path,
    stage_name: str,
    package: TreeSnapshot,
    _volume_fd: int | None = None,
) -> tuple[int, int]:
    volume_fd = (
        _open_directory_nofollow(volume_root, code="live_volume_invalid")
        if _volume_fd is None
        else os.dup(_volume_fd)
    )
    stage_fd: int | None = None
    try:
        try:
            os.mkdir(stage_name, package.root_mode, dir_fd=volume_fd)
            os.fsync(volume_fd)
            stage_fd = os.open(stage_name, _directory_flags(), dir_fd=volume_fd)
        except FileExistsError:
            _fail("staging_path_exists")
        except OSError:
            _fail("staging_create_failed")
        stage_stat = os.fstat(stage_fd)
        entry_stat = os.stat(stage_name, dir_fd=volume_fd, follow_symlinks=False)
        _require(
            _identity(stage_stat) == _identity(entry_stat)
            and stage_stat.st_dev == os.fstat(volume_fd).st_dev,
            "staging_device_invalid",
        )
        os.fchmod(stage_fd, package.root_mode)
        for directory in sorted(
            (row for row in package.directories if row.relpath != "."),
            key=lambda row: (len(PurePosixPath(row.relpath).parts), row.relpath),
        ):
            path = PurePosixPath(directory.relpath)
            parent_fd = _open_relative_directory(stage_fd, path.parent)
            try:
                os.mkdir(path.name, directory.mode, dir_fd=parent_fd)
                child_fd = os.open(path.name, _directory_flags(), dir_fd=parent_fd)
                try:
                    os.fchmod(child_fd, directory.mode)
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                os.fsync(parent_fd)
            except OSError:
                _fail("staging_create_failed")
            finally:
                os.close(parent_fd)
        for file_row in package.files:
            path = PurePosixPath(file_row.relpath)
            parent_fd = _open_relative_directory(stage_fd, path.parent)
            descriptor: int | None = None
            try:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(path.name, flags, file_row.mode, dir_fd=parent_fd)
                written = 0
                while written < len(file_row.content):
                    count = os.write(descriptor, file_row.content[written:])
                    _require(count > 0, "staging_write_failed")
                    written += count
                os.fchmod(descriptor, file_row.mode)
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
                _require(metadata.st_nlink == 1, "staging_race_detected")
                os.fsync(parent_fd)
            except OSError:
                _fail("staging_write_failed")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                os.close(parent_fd)
        for directory in sorted(
            package.directories,
            key=lambda row: (-len(PurePosixPath(row.relpath).parts), row.relpath),
        ):
            descriptor = _open_relative_directory(
                stage_fd, PurePosixPath(directory.relpath)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(stage_fd)
        os.fsync(volume_fd)
        return _identity(stage_stat)
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(volume_fd)


def _same_tree(actual: TreeSnapshot, expected: TreeSnapshot, code: str) -> None:
    _require(
        actual.tree_sha256 == expected.tree_sha256
        and actual.total_size_bytes == expected.total_size_bytes
        and len(actual.files) == len(expected.files),
        code,
    )


def _before_exchange_hook(
    _operation: str, _volume_root: Path, _destination_name: str, _peer_name: str
) -> None:
    """Test seam for the final path-check-to-exchange race window."""


def _exchange_and_verify(
    *,
    operation: str,
    volume_root: Path,
    destination_name: str,
    peer_name: str,
    expected_volume_identity: tuple[int, int],
    expected_destination: TreeSnapshot,
    expected_peer: TreeSnapshot,
) -> tuple[TreeSnapshot, TreeSnapshot]:
    volume_fd = _open_directory_nofollow(volume_root, code="live_volume_invalid")
    destination_fd: int | None = None
    peer_fd: int | None = None
    try:
        _require(
            _identity(os.fstat(volume_fd)) == expected_volume_identity,
            "live_volume_identity_drift",
        )
        destination_fd = os.open(destination_name, _directory_flags(), dir_fd=volume_fd)
        peer_fd = os.open(peer_name, _directory_flags(), dir_fd=volume_fd)
        _require(
            _identity(os.fstat(destination_fd))
            == (expected_destination.root_device, expected_destination.root_inode)
            and _identity(os.fstat(peer_fd))
            == (expected_peer.root_device, expected_peer.root_inode),
            "exchange_precondition_drift",
        )
        destination_entry = os.stat(
            destination_name, dir_fd=volume_fd, follow_symlinks=False
        )
        peer_entry = os.stat(peer_name, dir_fd=volume_fd, follow_symlinks=False)
        _require(
            _identity(destination_entry) == _identity(os.fstat(destination_fd))
            and _identity(peer_entry) == _identity(os.fstat(peer_fd)),
            "exchange_precondition_drift",
        )
        _before_exchange_hook(operation, volume_root, destination_name, peer_name)
        peer_entry_at_exchange = os.stat(
            peer_name, dir_fd=volume_fd, follow_symlinks=False
        )
        _require(
            _identity(peer_entry_at_exchange) == _identity(os.fstat(peer_fd)),
            "exchange_precondition_drift",
        )
        _renameat2(
            volume_fd,
            destination_name,
            volume_fd,
            peer_name,
            _RENAME_EXCHANGE,
            "exchange_failed",
        )
        def is_exact_tree(actual: TreeSnapshot, expected: TreeSnapshot) -> bool:
            return (
                actual.root_device,
                actual.root_inode,
                actual.tree_sha256,
                actual.total_size_bytes,
                len(actual.files),
            ) == (
                expected.root_device,
                expected.root_inode,
                expected.tree_sha256,
                expected.total_size_bytes,
                len(expected.files),
            )

        def current_destination_is_known_safe() -> bool:
            try:
                current = _snapshot_tree_at(
                    volume_fd,
                    destination_name,
                    display_root=volume_root / destination_name,
                    code="exchange_recovery_binding_drift",
                )
            except LivePublicationError:
                return False
            return is_exact_tree(current, expected_destination) or is_exact_tree(
                current, expected_peer
            )

        def restore_known_safe_destination() -> None:
            _require_directory_path_identity(
                volume_root,
                expected_volume_identity,
                "exchange_recovery_binding_drift",
            )
            if current_destination_is_known_safe():
                return
            while True:
                _require_directory_path_identity(
                    volume_root,
                    expected_volume_identity,
                    "exchange_recovery_binding_drift",
                )
                recovery_parent_name = (
                    f".property-live-repair-{operation}-{secrets.token_hex(32)}"
                )
                recovery_child_name = "known-safe-tree"
                recovery_parent_fd: int | None = None
                try:
                    try:
                        os.mkdir(recovery_parent_name, 0o700, dir_fd=volume_fd)
                        os.fsync(volume_fd)
                        recovery_parent_fd = os.open(
                            recovery_parent_name,
                            _directory_flags(),
                            dir_fd=volume_fd,
                        )
                    except OSError:
                        _fail("exchange_repair_failed")
                    parent_entry = os.stat(
                        recovery_parent_name,
                        dir_fd=volume_fd,
                        follow_symlinks=False,
                    )
                    parent_metadata = os.fstat(recovery_parent_fd)
                    _require(
                        _identity(parent_entry) == _identity(parent_metadata)
                        and parent_metadata.st_dev == os.fstat(volume_fd).st_dev
                        and stat.S_IMODE(parent_metadata.st_mode) == 0o700,
                        "exchange_repair_failed",
                    )
                    clone_identity = _copy_package_to_hidden_stage(
                        volume_root=volume_root / recovery_parent_name,
                        stage_name=recovery_child_name,
                        package=expected_destination,
                        _volume_fd=recovery_parent_fd,
                    )
                    clone = _snapshot_tree_at(
                        recovery_parent_fd,
                        recovery_child_name,
                        display_root=(
                            volume_root
                            / recovery_parent_name
                            / recovery_child_name
                        ),
                        code="exchange_repair_failed",
                    )
                    _require(
                        (clone.root_device, clone.root_inode) == clone_identity,
                        "exchange_repair_failed",
                    )
                    _same_tree(clone, expected_destination, "exchange_repair_failed")
                    try:
                        _renameat2(
                            volume_fd,
                            destination_name,
                            recovery_parent_fd,
                            recovery_child_name,
                            _RENAME_EXCHANGE,
                            "exchange_repair_failed",
                        )
                    except LivePublicationError:
                        pass
                    durable = True
                    try:
                        os.fsync(volume_fd)
                        os.fsync(recovery_parent_fd)
                    except OSError:
                        durable = False
                    try:
                        restored = _snapshot_tree_at(
                            volume_fd,
                            destination_name,
                            display_root=volume_root / destination_name,
                            code="exchange_repair_failed",
                        )
                    except LivePublicationError:
                        restored = None
                    if (
                        restored is not None
                        and (restored.root_device, restored.root_inode)
                        == clone_identity
                        and restored.tree_sha256 == expected_destination.tree_sha256
                        and restored.total_size_bytes
                        == expected_destination.total_size_bytes
                        and len(restored.files) == len(expected_destination.files)
                    ):
                        _require_directory_path_identity(
                            volume_root,
                            expected_volume_identity,
                            "exchange_recovery_binding_drift",
                        )
                        _require(durable, "exchange_repair_failed")
                        return
                    if current_destination_is_known_safe():
                        return
                finally:
                    if recovery_parent_fd is not None:
                        os.close(recovery_parent_fd)

        try:
            os.fsync(volume_fd)
            _require_directory_path_identity(
                volume_root,
                expected_volume_identity,
                "live_volume_identity_drift",
            )
            destination_after = _snapshot_tree_at(
                volume_fd,
                destination_name,
                display_root=volume_root / destination_name,
                code="exchange_verification_failed",
            )
            peer_after = _snapshot_tree_at(
                volume_fd,
                peer_name,
                display_root=volume_root / peer_name,
                code="exchange_verification_failed",
            )
            valid_exchange = (
                (destination_after.root_device, destination_after.root_inode)
                == (expected_peer.root_device, expected_peer.root_inode)
                and destination_after.tree_sha256 == expected_peer.tree_sha256
                and (peer_after.root_device, peer_after.root_inode)
                == (expected_destination.root_device, expected_destination.root_inode)
                and peer_after.tree_sha256 == expected_destination.tree_sha256
            )
        except (LivePublicationError, OSError):
            restore_known_safe_destination()
            _fail("exchange_compare_and_swap_failed")
        if not valid_exchange:
            restore_known_safe_destination()
            _fail("exchange_compare_and_swap_failed")
        _require_directory_path_identity(
            volume_root,
            expected_volume_identity,
            "live_volume_identity_drift",
        )
        os.fsync(volume_fd)
        return destination_after, peer_after
    except OSError:
        _fail("exchange_precondition_drift")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if peer_fd is not None:
            os.close(peer_fd)
        os.close(volume_fd)


def _validate_replacement_grant(
    grant: dict[str, Any], *, expected_binding: dict[str, object]
) -> None:
    _require(
        set(grant)
        == {
            "schema",
            "status",
            "owner",
            "authority_source",
            "operation",
            "grant_id",
            "single_use",
            "required_mode",
            "binding",
            "property_authority_upstream",
            "ea_authority",
            "secret_material_recorded",
        }
        and grant.get("schema") == REPLACEMENT_GRANT_SCHEMA
        and grant.get("status") == "authorized"
        and grant.get("owner") == OWNER
        and grant.get("authority_source") == AUTHORITY_SOURCE
        and grant.get("operation") == "replace"
        and grant.get("single_use") is True
        and type(grant.get("required_mode")) is int
        and grant.get("required_mode") == 0o600
        and grant.get("property_authority_upstream") is True
        and grant.get("ea_authority") is False
        and grant.get("secret_material_recorded") is False,
        "replacement_grant_invalid",
    )
    _strict_sha256(grant.get("grant_id"), "grant_id_invalid")
    _exact_match(grant.get("binding"), expected_binding, "replacement_grant_binding_mismatch")


def publish_live_with_grant(
    *,
    precondition_receipt_path: Path,
    grant_path: Path,
    publication_receipt_path: Path,
    package_root: Path,
    live_volume_root: Path,
    receipt_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
    user_instruction_sha256: str,
) -> dict[str, object]:
    current = inspect_live_precondition(
        package_root=package_root,
        live_volume_root=live_volume_root,
        receipt_root=receipt_root,
        origin=origin,
        logical_volume=logical_volume,
        slug=slug,
        destination_relpath=destination_relpath,
        user_instruction_sha256=user_instruction_sha256,
    )
    receipt_root = _absolute(receipt_root)
    precondition_file = _snapshot_secure_file(
        _receipt_path(
            precondition_receipt_path,
            receipt_root,
            "precondition_receipt_path_invalid",
        ),
        code="precondition_receipt_invalid",
        expected_mode=0o600,
    )
    supplied_precondition = _parse_json_object(
        precondition_file.content, "precondition_receipt_invalid"
    )
    _exact_match(supplied_precondition, current, "precondition_receipt_stale")
    grant_file = _snapshot_secure_file(
        _receipt_path(grant_path, receipt_root, "replacement_grant_path_invalid"),
        code="replacement_grant_invalid",
        expected_mode=0o600,
    )
    grant = _parse_json_object(grant_file.content, "replacement_grant_invalid")
    expected_binding = _replacement_binding(current, precondition_file.sha256)
    _validate_replacement_grant(grant, expected_binding=expected_binding)
    package = _inspect_package(
        package_root,
        slug=_safe_slug(slug),
        origin=_canonical_origin(origin),
        user_instruction_sha256=_strict_sha256(
            user_instruction_sha256, "user_instruction_hash_invalid"
        ),
    )
    old_tree = _snapshot_tree(
        _absolute(live_volume_root) / _destination(destination_relpath, slug),
        code="live_destination_invalid",
    )
    old_binding = dict(current["old_tree"])  # type: ignore[arg-type]
    _require(
        old_tree.tree_sha256 == old_binding["sha256"]
        and _identity_row((old_tree.root_device, old_tree.root_inode))
        == old_binding["root_identity"],
        "live_precondition_drift",
    )
    grant_id = _strict_sha256(grant.get("grant_id"), "grant_id_invalid")
    rollback_name = f".property-live-rollback-{grant_id}"
    stage_identity = _copy_package_to_hidden_stage(
        volume_root=_absolute(live_volume_root),
        stage_name=rollback_name,
        package=package.bundle,
    )
    staged = _snapshot_tree(
        _absolute(live_volume_root) / rollback_name,
        code="staging_verification_failed",
        expected_paths=_PUBLIC_FILE_RELPATHS,
        expected_file_mode=0o644,
        expected_directory_mode=0o755,
    )
    _require(
        (staged.root_device, staged.root_inode) == stage_identity,
        "staging_identity_drift",
    )
    _same_tree(staged, package.bundle, "staging_content_drift")
    package_after_stage = _inspect_package(
        package_root,
        slug=slug,
        origin=origin,
        user_instruction_sha256=user_instruction_sha256,
    )
    _require(
        package_after_stage.package_root_identity == package.package_root_identity
        and (
            package_after_stage.bundle.root_device,
            package_after_stage.bundle.root_inode,
        )
        == (package.bundle.root_device, package.bundle.root_inode)
        and package_after_stage.authority.sha256 == package.authority.sha256
        and (package_after_stage.authority.device, package_after_stage.authority.inode)
        == (package.authority.device, package.authority.inode),
        "package_source_drift",
    )
    _same_tree(package_after_stage.bundle, package.bundle, "package_source_drift")
    old_before_exchange = _snapshot_tree(
        _absolute(live_volume_root) / destination_relpath,
        code="live_destination_invalid",
    )
    _require(
        (old_before_exchange.root_device, old_before_exchange.root_inode)
        == (old_tree.root_device, old_tree.root_inode)
        and old_before_exchange.tree_sha256 == old_tree.tree_sha256,
        "live_precondition_drift",
    )
    publication_target = _receipt_path(
        publication_receipt_path,
        receipt_root,
        "publication_receipt_path_invalid",
    )
    transaction_path = _transaction_retained_path(
        receipt_root, operation="replace", grant_id=grant_id
    )
    prepared_transaction = _build_prepared_transaction(
        operation="replace",
        origin=str(current["origin"]),
        logical_volume=str(current["logical_volume"]),
        live_volume_root=_absolute(live_volume_root),
        live_volume_root_identity=_strict_identity(
            current["live_volume_root_identity"], "live_volume_identity_drift"
        ),
        receipt_root=receipt_root,
        receipt_root_identity=_strict_identity(
            current["receipt_root_identity"], "receipt_root_identity_drift"
        ),
        slug=slug,
        destination_relpath=destination_relpath,
        peer_relpath=rollback_name,
        destination_before=old_before_exchange,
        peer_before=staged,
        grant=grant,
        grant_sha256=grant_file.sha256,
        final_receipt_path=publication_target,
    )
    prepared_file = _reserve_prepared_transaction(
        final_receipt_path=publication_target,
        receipt_root=receipt_root,
        transaction=prepared_transaction,
        code="publication_receipt_reservation_failed",
    )
    claimed = _claim_grant(
        grant_path=_absolute(grant_path),
        receipt_root=receipt_root,
        grant=grant,
        expected_sha256=grant_file.sha256,
    )
    published, retained_old = _exchange_and_verify(
        operation="replace",
        volume_root=_absolute(live_volume_root),
        destination_name=destination_relpath,
        peer_name=rollback_name,
        expected_volume_identity=_strict_identity(
            current["live_volume_root_identity"], "live_volume_identity_drift"
        ),
        expected_destination=old_before_exchange,
        expected_peer=staged,
    )
    package_after_exchange = _inspect_package(
        package_root,
        slug=slug,
        origin=origin,
        user_instruction_sha256=user_instruction_sha256,
    )
    _same_tree(package_after_exchange.bundle, package.bundle, "package_source_drift")
    _require(
        (
            package_after_exchange.bundle.root_device,
            package_after_exchange.bundle.root_inode,
        )
        == (package.bundle.root_device, package.bundle.root_inode),
        "package_source_drift",
    )
    _require_directory_path_identity(
        _absolute(live_volume_root),
        _strict_identity(
            current["live_volume_root_identity"], "live_volume_identity_drift"
        ),
        "live_volume_identity_drift",
    )
    receipt = {
        "schema": PUBLICATION_RECEIPT_SCHEMA,
        "status": "published",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": "replace",
        "origin": current["origin"],
        "logical_volume": current["logical_volume"],
        "live_volume_root": current["live_volume_root"],
        "live_volume_root_identity": current["live_volume_root_identity"],
        "receipt_root": current["receipt_root"],
        "receipt_root_identity": current["receipt_root_identity"],
        "slug": current["slug"],
        "destination_relpath": current["destination_relpath"],
        "grant_id": grant_id,
        "consumed_grant_path": str(claimed.path),
        "consumed_grant_sha256": claimed.sha256,
        "precondition_receipt_sha256": precondition_file.sha256,
        "transaction_receipt_path": str(transaction_path),
        "transaction_receipt_sha256": prepared_file.sha256,
        "authority_receipt_sha256": package.authority.sha256,
        "user_instruction_sha256": package.user_instruction_sha256,
        "published_tree": _tree_receipt(published),
        "retained_rollback_relpath": rollback_name,
        "retained_old_tree": _tree_receipt(retained_old),
        "atomic_exchange": True,
        "rollback_tree_retained": True,
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }
    _finalize_transaction_receipt(
        final_receipt_path=publication_target,
        retained_transaction_path=transaction_path,
        receipt_root=receipt_root,
        prepared=prepared_file,
        final_receipt=receipt,
        live_volume_root=_absolute(live_volume_root),
        expected_live_volume_identity=_strict_identity(
            current["live_volume_root_identity"], "live_volume_identity_drift"
        ),
        expected_receipt_root_identity=_strict_identity(
            current["receipt_root_identity"], "receipt_root_identity_drift"
        ),
        code="publication_receipt_write_failed",
    )
    return receipt


def _validate_publication_receipt(
    receipt: dict[str, Any],
    *,
    origin: str,
    logical_volume: str,
    live_volume_root: Path,
    receipt_root: Path,
    slug: str,
    destination_relpath: str,
) -> None:
    required = {
        "schema",
        "status",
        "owner",
        "authority_source",
        "operation",
        "origin",
        "logical_volume",
        "live_volume_root",
        "live_volume_root_identity",
        "receipt_root",
        "receipt_root_identity",
        "slug",
        "destination_relpath",
        "grant_id",
        "consumed_grant_path",
        "consumed_grant_sha256",
        "precondition_receipt_sha256",
        "transaction_receipt_path",
        "transaction_receipt_sha256",
        "authority_receipt_sha256",
        "user_instruction_sha256",
        "published_tree",
        "retained_rollback_relpath",
        "retained_old_tree",
        "atomic_exchange",
        "rollback_tree_retained",
        "property_authority_upstream",
        "ea_authority",
        "secret_material_recorded",
    }
    _require(
        set(receipt) == required
        and receipt.get("schema") == PUBLICATION_RECEIPT_SCHEMA
        and receipt.get("status") == "published"
        and receipt.get("owner") == OWNER
        and receipt.get("authority_source") == AUTHORITY_SOURCE
        and receipt.get("operation") == "replace"
        and receipt.get("origin") == _canonical_origin(origin)
        and receipt.get("logical_volume") == _logical_volume(logical_volume)
        and receipt.get("live_volume_root") == str(_absolute(live_volume_root))
        and receipt.get("receipt_root") == str(_absolute(receipt_root))
        and receipt.get("slug") == _safe_slug(slug)
        and receipt.get("destination_relpath") == _destination(destination_relpath, slug)
        and receipt.get("atomic_exchange") is True
        and receipt.get("rollback_tree_retained") is True
        and receipt.get("property_authority_upstream") is True
        and receipt.get("ea_authority") is False
        and receipt.get("secret_material_recorded") is False,
        "publication_receipt_invalid",
    )
    _strict_identity(receipt.get("live_volume_root_identity"), "publication_receipt_invalid")
    _strict_identity(receipt.get("receipt_root_identity"), "publication_receipt_invalid")
    _strict_sha256(receipt.get("grant_id"), "publication_receipt_invalid")
    for key in (
        "consumed_grant_sha256",
        "precondition_receipt_sha256",
        "transaction_receipt_sha256",
        "authority_receipt_sha256",
        "user_instruction_sha256",
    ):
        _strict_sha256(receipt.get(key), "publication_receipt_invalid")
    for key in ("published_tree", "retained_old_tree"):
        row = receipt.get(key)
        _require(
            isinstance(row, dict)
            and set(row)
            == {"file_count", "root_identity", "sha256", "total_size_bytes"},
            "publication_receipt_invalid",
        )
        tree = dict(row)
        _strict_int(tree.get("file_count"), "publication_receipt_invalid")
        _strict_int(tree.get("total_size_bytes"), "publication_receipt_invalid")
        _strict_identity(tree.get("root_identity"), "publication_receipt_invalid")
        _strict_sha256(tree.get("sha256"), "publication_receipt_invalid")
    rollback_name = _strict_string(
        receipt.get("retained_rollback_relpath"), "publication_receipt_invalid"
    )
    _require(
        rollback_name == f".property-live-rollback-{receipt['grant_id']}",
        "publication_receipt_invalid",
    )
    expected_transaction_path = _transaction_retained_path(
        _absolute(receipt_root),
        operation="replace",
        grant_id=str(receipt["grant_id"]),
    )
    _require(
        receipt.get("transaction_receipt_path")
        == str(expected_transaction_path),
        "publication_receipt_invalid",
    )


def _rollback_binding(
    publication: dict[str, Any],
    publication_sha256: str,
    rollback_instruction_sha256: str,
) -> dict[str, object]:
    return {
        "publication_receipt_sha256": publication_sha256,
        "origin": publication["origin"],
        "logical_volume": publication["logical_volume"],
        "live_volume_root": publication["live_volume_root"],
        "live_volume_root_identity": publication["live_volume_root_identity"],
        "receipt_root": publication["receipt_root"],
        "receipt_root_identity": publication["receipt_root_identity"],
        "slug": publication["slug"],
        "destination_relpath": publication["destination_relpath"],
        "published_tree": publication["published_tree"],
        "retained_rollback_relpath": publication["retained_rollback_relpath"],
        "retained_old_tree": publication["retained_old_tree"],
        "publication_user_instruction_sha256": publication["user_instruction_sha256"],
        "rollback_user_instruction_sha256": rollback_instruction_sha256,
    }


def _load_current_publication(
    *,
    publication_receipt_path: Path,
    receipt_root: Path,
    live_volume_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
) -> tuple[SecureFile, dict[str, Any], TreeSnapshot, TreeSnapshot]:
    receipt_root = _ensure_private_receipt_root(
        receipt_root,
        live_volume_root=live_volume_root,
        package_root=None,
    )
    receipt_root_fd = _open_directory_nofollow(
        receipt_root, code="receipt_root_invalid"
    )
    try:
        receipt_root_identity = _identity(os.fstat(receipt_root_fd))
    finally:
        os.close(receipt_root_fd)
    publication_file = _snapshot_secure_file(
        _receipt_path(
            publication_receipt_path,
            receipt_root,
            "publication_receipt_path_invalid",
        ),
        code="publication_receipt_invalid",
        expected_mode=0o600,
    )
    publication = _parse_json_object(
        publication_file.content, "publication_receipt_invalid"
    )
    _validate_publication_receipt(
        publication,
        origin=origin,
        logical_volume=logical_volume,
        live_volume_root=live_volume_root,
        receipt_root=receipt_root,
        slug=slug,
        destination_relpath=destination_relpath,
    )
    transaction_file = _snapshot_secure_file(
        _receipt_path(
            Path(str(publication["transaction_receipt_path"])),
            receipt_root,
            "transaction_receipt_invalid",
        ),
        code="transaction_receipt_invalid",
        expected_mode=0o600,
    )
    _require(
        transaction_file.sha256 == publication["transaction_receipt_sha256"],
        "transaction_receipt_invalid",
    )
    transaction = _parse_json_object(
        transaction_file.content, "transaction_receipt_invalid"
    )
    _require(
        transaction.get("schema") == TRANSACTION_RECEIPT_SCHEMA
        and transaction.get("status") == "prepared"
        and transaction.get("owner") == OWNER
        and transaction.get("authority_source") == AUTHORITY_SOURCE
        and transaction.get("operation") == "replace"
        and transaction.get("origin") == publication["origin"]
        and transaction.get("logical_volume") == publication["logical_volume"]
        and transaction.get("live_volume_root") == publication["live_volume_root"]
        and transaction.get("live_volume_root_identity")
        == publication["live_volume_root_identity"]
        and transaction.get("receipt_root") == publication["receipt_root"]
        and transaction.get("receipt_root_identity")
        == publication["receipt_root_identity"]
        and transaction.get("slug") == publication["slug"]
        and transaction.get("destination_relpath")
        == publication["destination_relpath"]
        and transaction.get("peer_relpath")
        == publication["retained_rollback_relpath"]
        and transaction.get("grant_id") == publication["grant_id"]
        and transaction.get("grant_sha256")
        == publication["consumed_grant_sha256"]
        and transaction.get("expected_consumed_grant_path")
        == publication["consumed_grant_path"]
        and transaction.get("final_receipt_path")
        == str(_absolute(publication_receipt_path))
        and transaction.get("retained_transaction_path")
        == publication["transaction_receipt_path"]
        and transaction.get("destination_before")
        == publication["retained_old_tree"]
        and transaction.get("peer_before") == publication["published_tree"]
        and transaction.get("destination_after") == publication["published_tree"]
        and transaction.get("peer_after") == publication["retained_old_tree"]
        and transaction.get("atomic_exchange_planned") is True
        and transaction.get("recovery_evidence_durable_before_exchange") is True
        and transaction.get("property_authority_upstream") is True
        and transaction.get("ea_authority") is False
        and transaction.get("secret_material_recorded") is False,
        "transaction_receipt_invalid",
    )
    volume_fd = _open_directory_nofollow(
        live_volume_root, code="live_volume_invalid"
    )
    try:
        volume_identity = _identity(os.fstat(volume_fd))
    finally:
        os.close(volume_fd)
    _require(
        _identity_row(volume_identity) == publication["live_volume_root_identity"],
        "live_volume_identity_drift",
    )
    _require(
        _identity_row(receipt_root_identity) == publication["receipt_root_identity"],
        "receipt_root_identity_drift",
    )
    published = _snapshot_tree(
        _absolute(live_volume_root) / destination_relpath,
        code="live_destination_invalid",
    )
    retained = _snapshot_tree(
        _absolute(live_volume_root) / publication["retained_rollback_relpath"],
        code="retained_rollback_invalid",
    )
    _exact_match(
        _tree_receipt(published), publication["published_tree"], "published_tree_drift"
    )
    _exact_match(
        _tree_receipt(retained), publication["retained_old_tree"], "rollback_tree_drift"
    )
    return publication_file, publication, published, retained


def create_rollback_grant(
    *,
    publication_receipt_path: Path,
    grant_path: Path,
    live_volume_root: Path,
    receipt_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
    rollback_user_instruction_sha256: str,
) -> dict[str, object]:
    root = _absolute(receipt_root)
    rollback_instruction = _strict_sha256(
        rollback_user_instruction_sha256, "rollback_user_instruction_invalid"
    )
    publication_file, publication, _published, _retained = _load_current_publication(
        publication_receipt_path=publication_receipt_path,
        receipt_root=root,
        live_volume_root=_absolute(live_volume_root),
        origin=origin,
        logical_volume=logical_volume,
        slug=slug,
        destination_relpath=destination_relpath,
    )
    grant = {
        "schema": ROLLBACK_GRANT_SCHEMA,
        "status": "authorized",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": "rollback",
        "grant_id": secrets.token_hex(32),
        "single_use": True,
        "required_mode": 0o600,
        "binding": _rollback_binding(
            publication,
            publication_file.sha256,
            rollback_instruction,
        ),
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }
    target = _receipt_path(grant_path, root, "rollback_grant_path_invalid")
    _write_bytes_exclusive(
        target,
        _json_file_bytes(grant),
        mode=0o600,
        code="rollback_grant_write_failed",
    )
    return grant


def _validate_rollback_grant(
    grant: dict[str, Any], *, expected_binding: dict[str, object]
) -> None:
    _require(
        set(grant)
        == {
            "schema",
            "status",
            "owner",
            "authority_source",
            "operation",
            "grant_id",
            "single_use",
            "required_mode",
            "binding",
            "property_authority_upstream",
            "ea_authority",
            "secret_material_recorded",
        }
        and grant.get("schema") == ROLLBACK_GRANT_SCHEMA
        and grant.get("status") == "authorized"
        and grant.get("owner") == OWNER
        and grant.get("authority_source") == AUTHORITY_SOURCE
        and grant.get("operation") == "rollback"
        and grant.get("single_use") is True
        and type(grant.get("required_mode")) is int
        and grant.get("required_mode") == 0o600
        and grant.get("property_authority_upstream") is True
        and grant.get("ea_authority") is False
        and grant.get("secret_material_recorded") is False,
        "rollback_grant_invalid",
    )
    _strict_sha256(grant.get("grant_id"), "rollback_grant_invalid")
    _exact_match(grant.get("binding"), expected_binding, "rollback_grant_binding_mismatch")


def rollback_live_with_grant(
    *,
    publication_receipt_path: Path,
    grant_path: Path,
    rollback_receipt_path: Path,
    live_volume_root: Path,
    receipt_root: Path,
    origin: str,
    logical_volume: str,
    slug: str,
    destination_relpath: str,
    rollback_user_instruction_sha256: str,
) -> dict[str, object]:
    root = _absolute(receipt_root)
    publication_file, publication, published, retained = _load_current_publication(
        publication_receipt_path=publication_receipt_path,
        receipt_root=root,
        live_volume_root=_absolute(live_volume_root),
        origin=origin,
        logical_volume=logical_volume,
        slug=slug,
        destination_relpath=destination_relpath,
    )
    rollback_instruction = _strict_sha256(
        rollback_user_instruction_sha256, "rollback_user_instruction_invalid"
    )
    grant_file = _snapshot_secure_file(
        _receipt_path(grant_path, root, "rollback_grant_path_invalid"),
        code="rollback_grant_invalid",
        expected_mode=0o600,
    )
    grant = _parse_json_object(grant_file.content, "rollback_grant_invalid")
    expected_binding = _rollback_binding(
        publication, publication_file.sha256, rollback_instruction
    )
    _validate_rollback_grant(grant, expected_binding=expected_binding)
    grant_id = _strict_sha256(grant.get("grant_id"), "rollback_grant_invalid")
    rollback_target = _receipt_path(
        rollback_receipt_path, root, "rollback_receipt_path_invalid"
    )
    transaction_path = _transaction_retained_path(
        root, operation="rollback", grant_id=grant_id
    )
    prepared_transaction = _build_prepared_transaction(
        operation="rollback",
        origin=str(publication["origin"]),
        logical_volume=str(publication["logical_volume"]),
        live_volume_root=_absolute(live_volume_root),
        live_volume_root_identity=_strict_identity(
            publication["live_volume_root_identity"],
            "live_volume_identity_drift",
        ),
        receipt_root=root,
        receipt_root_identity=_strict_identity(
            publication["receipt_root_identity"],
            "receipt_root_identity_drift",
        ),
        slug=slug,
        destination_relpath=destination_relpath,
        peer_relpath=str(publication["retained_rollback_relpath"]),
        destination_before=published,
        peer_before=retained,
        grant=grant,
        grant_sha256=grant_file.sha256,
        final_receipt_path=rollback_target,
    )
    prepared_file = _reserve_prepared_transaction(
        final_receipt_path=rollback_target,
        receipt_root=root,
        transaction=prepared_transaction,
        code="rollback_receipt_reservation_failed",
    )
    claimed = _claim_grant(
        grant_path=grant_path,
        receipt_root=root,
        grant=grant,
        expected_sha256=grant_file.sha256,
    )
    restored, retained_published = _exchange_and_verify(
        operation="rollback",
        volume_root=_absolute(live_volume_root),
        destination_name=destination_relpath,
        peer_name=publication["retained_rollback_relpath"],
        expected_volume_identity=_strict_identity(
            publication["live_volume_root_identity"],
            "live_volume_identity_drift",
        ),
        expected_destination=published,
        expected_peer=retained,
    )
    _require_directory_path_identity(
        _absolute(live_volume_root),
        _strict_identity(
            publication["live_volume_root_identity"],
            "live_volume_identity_drift",
        ),
        "live_volume_identity_drift",
    )
    receipt = {
        "schema": ROLLBACK_RECEIPT_SCHEMA,
        "status": "rolled_back",
        "owner": OWNER,
        "authority_source": AUTHORITY_SOURCE,
        "operation": "rollback",
        "origin": publication["origin"],
        "logical_volume": publication["logical_volume"],
        "live_volume_root": publication["live_volume_root"],
        "live_volume_root_identity": publication["live_volume_root_identity"],
        "receipt_root": publication["receipt_root"],
        "receipt_root_identity": publication["receipt_root_identity"],
        "slug": publication["slug"],
        "destination_relpath": publication["destination_relpath"],
        "publication_receipt_sha256": publication_file.sha256,
        "grant_id": grant_id,
        "consumed_grant_path": str(claimed.path),
        "consumed_grant_sha256": claimed.sha256,
        "transaction_receipt_path": str(transaction_path),
        "transaction_receipt_sha256": prepared_file.sha256,
        "rollback_user_instruction_sha256": rollback_instruction,
        "restored_tree": _tree_receipt(restored),
        "retained_published_relpath": publication["retained_rollback_relpath"],
        "retained_published_tree": _tree_receipt(retained_published),
        "atomic_exchange": True,
        "published_tree_retained": True,
        "property_authority_upstream": True,
        "ea_authority": False,
        "secret_material_recorded": False,
    }
    _finalize_transaction_receipt(
        final_receipt_path=rollback_target,
        retained_transaction_path=transaction_path,
        receipt_root=root,
        prepared=prepared_file,
        final_receipt=receipt,
        live_volume_root=_absolute(live_volume_root),
        expected_live_volume_identity=_strict_identity(
            publication["live_volume_root_identity"],
            "live_volume_identity_drift",
        ),
        expected_receipt_root_identity=_strict_identity(
            publication["receipt_root_identity"],
            "receipt_root_identity_drift",
        ),
        code="rollback_receipt_write_failed",
    )
    return receipt


def _add_common_arguments(parser: argparse.ArgumentParser, *, package: bool) -> None:
    if package:
        parser.add_argument("--package-root", required=True)
        parser.add_argument("--user-instruction-sha256", required=True)
    parser.add_argument("--live-volume-root", required=True)
    parser.add_argument("--receipt-root", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--logical-volume", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--destination-relpath", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Property-owned live tour CAS publication and rollback lane."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    precondition = subparsers.add_parser("precondition")
    _add_common_arguments(precondition, package=True)
    precondition.add_argument("--receipt", required=True)
    grant = subparsers.add_parser("grant-replacement")
    _add_common_arguments(grant, package=True)
    grant.add_argument("--precondition-receipt", required=True)
    grant.add_argument("--grant", required=True)
    publish = subparsers.add_parser("publish")
    _add_common_arguments(publish, package=True)
    publish.add_argument("--precondition-receipt", required=True)
    publish.add_argument("--grant", required=True)
    publish.add_argument("--receipt", required=True)
    rollback_grant = subparsers.add_parser("grant-rollback")
    _add_common_arguments(rollback_grant, package=False)
    rollback_grant.add_argument("--publication-receipt", required=True)
    rollback_grant.add_argument("--grant", required=True)
    rollback_grant.add_argument("--rollback-user-instruction-sha256", required=True)
    rollback = subparsers.add_parser("rollback")
    _add_common_arguments(rollback, package=False)
    rollback.add_argument("--publication-receipt", required=True)
    rollback.add_argument("--grant", required=True)
    rollback.add_argument("--receipt", required=True)
    rollback.add_argument("--rollback-user-instruction-sha256", required=True)
    return parser


def _common_kwargs(args: argparse.Namespace) -> dict[str, object]:
    return {
        "live_volume_root": Path(args.live_volume_root),
        "receipt_root": Path(args.receipt_root),
        "origin": args.origin,
        "logical_volume": args.logical_volume,
        "slug": args.slug,
        "destination_relpath": args.destination_relpath,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        common = _common_kwargs(args)
        if args.command == "precondition":
            receipt = inspect_live_precondition(
                **common,
                package_root=Path(args.package_root),
                user_instruction_sha256=args.user_instruction_sha256,
            )
            write_precondition_receipt(
                receipt,
                path=Path(args.receipt),
                receipt_root=Path(args.receipt_root),
            )
        elif args.command == "grant-replacement":
            receipt = create_replacement_grant(
                **common,
                package_root=Path(args.package_root),
                user_instruction_sha256=args.user_instruction_sha256,
                precondition_receipt_path=Path(args.precondition_receipt),
                grant_path=Path(args.grant),
            )
        elif args.command == "publish":
            receipt = publish_live_with_grant(
                **common,
                package_root=Path(args.package_root),
                user_instruction_sha256=args.user_instruction_sha256,
                precondition_receipt_path=Path(args.precondition_receipt),
                grant_path=Path(args.grant),
                publication_receipt_path=Path(args.receipt),
            )
        elif args.command == "grant-rollback":
            receipt = create_rollback_grant(
                **common,
                publication_receipt_path=Path(args.publication_receipt),
                grant_path=Path(args.grant),
                rollback_user_instruction_sha256=(
                    args.rollback_user_instruction_sha256
                ),
            )
        else:
            receipt = rollback_live_with_grant(
                **common,
                publication_receipt_path=Path(args.publication_receipt),
                grant_path=Path(args.grant),
                rollback_receipt_path=Path(args.receipt),
                rollback_user_instruction_sha256=(
                    args.rollback_user_instruction_sha256
                ),
            )
    except LivePublicationError as exc:
        print(
            json.dumps(
                {
                    "schema": "propertyquarry.live-tour-operation.v1",
                    "status": "blocked",
                    "error": exc.code,
                    "secret_material_recorded": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
