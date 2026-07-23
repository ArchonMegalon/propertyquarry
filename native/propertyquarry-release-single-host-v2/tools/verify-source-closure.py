#!/usr/bin/env python3
"""Create and verify the exact, immutable native-build source closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from typing import NoReturn


PATH_PATTERN = re.compile(r"^[A-Za-z0-9_@+.,/=-]+$")
EXECUTABLE_PATTERN = re.compile(r"^tools/[^/]+\.(?:py|sh)$")
MATERIAL_PATTERN = re.compile(
    r"^([0-9a-f]{64})  ([0-7]{4})  regular  ([A-Za-z0-9_@+.,/=-]+)$"
)


def fail(code: str) -> NoReturn:
    raise SystemExit(code)


def expected_mode(relative: str) -> int:
    return 0o755 if EXECUTABLE_PATTERN.fullmatch(relative) else 0o644


def load_manifest(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        fail("source-manifest-unavailable")
    if not raw or not raw.endswith(b"\n") or b"\r" in raw or b"\0" in raw:
        fail("source-manifest-encoding-invalid")
    try:
        lines = raw[:-1].decode("ascii").split("\n")
    except UnicodeDecodeError:
        fail("source-manifest-encoding-invalid")
    if (
        not lines
        or any(not line for line in lines)
        or lines != sorted(lines)
        or len(lines) != len(set(lines))
    ):
        fail("source-manifest-order-invalid")
    for relative in lines:
        parts = PurePosixPath(relative).parts
        if (
            not PATH_PATTERN.fullmatch(relative)
            or relative.startswith("/")
            or "//" in relative
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
            or os.fspath(PurePosixPath(*parts)) != relative
            or "__pycache__" in parts
            or relative.endswith((".pyc", ".pyo"))
        ):
            fail("source-manifest-path-invalid")
    return lines


def inventory(root: Path) -> dict[str, os.stat_result]:
    try:
        root_metadata = root.lstat()
    except OSError:
        fail("source-root-unavailable")
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root.resolve() != root
    ):
        fail("source-root-invalid")
    result: dict[str, os.stat_result] = {}

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            fail("source-inventory-unavailable")
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                fail("source-entry-unavailable")
            if entry.is_symlink():
                fail("source-symlink-forbidden")
            if stat.S_ISDIR(metadata.st_mode):
                if metadata.st_mode & 0o022:
                    fail("source-directory-mode-invalid")
                walk(Path(entry.path), relative)
            elif stat.S_ISREG(metadata.st_mode):
                if "__pycache__" in PurePosixPath(relative).parts or relative.endswith(
                    (".pyc", ".pyo")
                ):
                    fail("source-generated-artifact-forbidden")
                result[relative] = metadata
            else:
                fail("source-entry-type-invalid")

    walk(root, "")
    return result


def read_stable(path: Path, metadata: os.stat_result) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    if (
        identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("source-mutated-during-read")
    raw = b"".join(chunks)
    if len(raw) != metadata.st_size:
        fail("source-size-mutated")
    return raw


def write_snapshot_file(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    succeeded = False
    try:
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count < 1:
                fail("source-snapshot-write-failed")
            written += count
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        succeeded = True
    finally:
        os.close(descriptor)
        if not succeeded:
            try:
                path.unlink()
            except OSError:
                pass


def create_snapshot(root: Path, manifest: Path, snapshot: Path, material: Path) -> None:
    lines = load_manifest(manifest)
    actual = inventory(root)
    if set(lines) != set(actual):
        fail("source-inventory-mismatch")
    try:
        snapshot_metadata = snapshot.lstat()
    except OSError:
        fail("source-snapshot-root-unavailable")
    if (
        not stat.S_ISDIR(snapshot_metadata.st_mode)
        or stat.S_ISLNK(snapshot_metadata.st_mode)
        or any(snapshot.iterdir())
    ):
        fail("source-snapshot-root-invalid")
    records: list[str] = []
    for relative in lines:
        metadata = actual[relative]
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_nlink != 1 or mode != expected_mode(relative):
            fail("source-file-metadata-invalid")
        raw = read_stable(root / relative, metadata)
        records.append(
            f"{hashlib.sha256(raw).hexdigest()}  {mode:04o}  regular  {relative}\n"
        )
        write_snapshot_file(snapshot / relative, raw, mode & ~0o222)
    material_raw = "".join(records).encode("ascii")
    descriptor = os.open(
        material,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        written = 0
        while written < len(material_raw):
            count = os.write(descriptor, material_raw[written:])
            if count < 1:
                fail("source-material-write-failed")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for directory, _children, _files in os.walk(snapshot, topdown=False):
        os.chmod(directory, 0o500)


def load_material(path: Path) -> dict[str, tuple[str, int]]:
    try:
        raw = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        fail("source-material-unavailable")
    if not raw or not raw.endswith("\n"):
        fail("source-material-invalid")
    result: dict[str, tuple[str, int]] = {}
    for line in raw[:-1].split("\n"):
        match = MATERIAL_PATTERN.fullmatch(line)
        if match is None or match.group(3) in result:
            fail("source-material-invalid")
        result[match.group(3)] = (match.group(1), int(match.group(2), 8))
    if list(result) != sorted(result):
        fail("source-material-order-invalid")
    return result


def verify_snapshot(snapshot: Path, material: Path) -> None:
    expected = load_material(material)
    actual = inventory(snapshot)
    if set(actual) != set(expected):
        fail("source-snapshot-inventory-mismatch")
    for relative, (expected_digest, _source_mode) in expected.items():
        metadata = actual[relative]
        if stat.S_IMODE(metadata.st_mode) != expected_mode(relative) & ~0o222:
            fail("source-snapshot-mode-invalid")
        raw = read_stable(snapshot / relative, metadata)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            fail("source-snapshot-digest-invalid")


def iter_json_values(raw: str):
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            return
        value, offset = decoder.raw_decode(raw, offset)
        yield value


def verify_go_list(snapshot: Path, material: Path, go_list: Path) -> None:
    expected = load_material(material)
    expected_go = {relative for relative in expected if relative.endswith(".go")}
    try:
        raw = go_list.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        fail("source-go-list-unavailable")
    observed: set[str] = set()
    try:
        values = list(iter_json_values(raw))
    except (json.JSONDecodeError, ValueError):
        fail("source-go-list-invalid")
    for value in values:
        if type(value) is not dict or type(value.get("Dir")) is not str:
            fail("source-go-list-invalid")
        directory = Path(value["Dir"])
        try:
            package_relative = directory.relative_to(snapshot)
        except ValueError:
            fail("source-go-list-escape")
        if value.get("CgoFiles") not in (None, []):
            fail("source-cgo-forbidden")
        for field in ("GoFiles", "TestGoFiles", "XTestGoFiles", "EmbedFiles"):
            names = value.get(field, [])
            if type(names) is not list or any(type(name) is not str for name in names):
                fail("source-go-list-invalid")
            for name in names:
                candidate = package_relative / name
                relative = candidate.as_posix()
                if relative.startswith("../") or relative not in expected:
                    fail("source-go-input-unbound")
                if field != "EmbedFiles" and not relative.endswith(".go"):
                    fail("source-go-list-invalid")
                if relative.endswith(".go"):
                    observed.add(relative)
    if observed != expected_go:
        fail("source-go-input-set-mismatch")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create", allow_abbrev=False)
    create.add_argument("--module-root", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--snapshot", required=True)
    create.add_argument("--material", required=True)
    verify = subcommands.add_parser("verify", allow_abbrev=False)
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--material", required=True)
    go_list = subcommands.add_parser("verify-go-list", allow_abbrev=False)
    go_list.add_argument("--snapshot", required=True)
    go_list.add_argument("--material", required=True)
    go_list.add_argument("--go-list", required=True)
    return parser.parse_args()


def absolute(value: str, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        fail(code)
    return path


def main() -> int:
    arguments = parse_arguments()
    if arguments.command == "create":
        create_snapshot(
            absolute(arguments.module_root, "source-root-path-invalid"),
            absolute(arguments.manifest, "source-manifest-path-invalid"),
            absolute(arguments.snapshot, "source-snapshot-path-invalid"),
            absolute(arguments.material, "source-material-path-invalid"),
        )
    elif arguments.command == "verify":
        verify_snapshot(
            absolute(arguments.snapshot, "source-snapshot-path-invalid"),
            absolute(arguments.material, "source-material-path-invalid"),
        )
    else:
        verify_go_list(
            absolute(arguments.snapshot, "source-snapshot-path-invalid"),
            absolute(arguments.material, "source-material-path-invalid"),
            absolute(arguments.go_list, "source-go-list-path-invalid"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
