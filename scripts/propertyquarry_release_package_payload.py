#!/usr/bin/env python3
"""Assemble one unsigned, non-installing PropertyQuarry v2 payload tree.

The assembler binds bytes and projected installation metadata.  It writes only
the caller-selected payload output parent; it does not authenticate inputs,
sign a package, install the role paths into ``/``, or establish readiness.
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
from typing import Callable, Final, Mapping

try:  # Support both ``python -m scripts...`` and direct script execution.
    from scripts import propertyquarry_release_installation_model as installation
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI tests
    import propertyquarry_release_installation_model as installation


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT: Final = (
    REPOSITORY_ROOT / "packaging" / "propertyquarry-release-control-v2"
)
RECEIPT_SCHEMA: Final = "propertyquarry.release-control.package-payload-receipt.v2"
MAX_JSON_BYTES: Final = 256 * 1024
MAX_JSON_DEPTH: Final = 32
MAX_SERVICE_GID: Final = (1 << 31) - 1
RENAME_NOREPLACE: Final = 1

NATIVE_FILES: Final[dict[str, int | frozenset[int] | None]] = {
    "build-receipt.json": 0o644,
    "propertyquarry-release-controller-v2": 0o755,
    "propertyquarry-release-supervisor-v2": 0o755,
    "propertyquarry-release-watchdog-v2": 0o755,
}
TEMPLATE_FILES: Final[dict[str, int | frozenset[int] | None]] = {
    "systemd/propertyquarry-release-control-v2.socket": None,
    "systemd/propertyquarry-release-control-v2@.service": None,
    "systemd/propertyquarry-release-watchdog-v2.service": None,
    "sysusers.d/propertyquarry-release-control-v2.conf": None,
    "tmpfiles.d/propertyquarry-release-control-v2.conf": None,
    "schema/controller-v2.schema.json": None,
    "schema/watchdog-v2.schema.json": None,
}
PRIVATE_FILES: Final[dict[str, int | frozenset[int] | None]] = {
    "controller-v2.json": frozenset({0o400, 0o440, 0o600, 0o640}),
    "watchdog-v2.json": frozenset({0o400, 0o440, 0o600, 0o640}),
    "policy-v2.json": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/request-authority-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/response-authority-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/lifecycle-cas-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/evidence-authority-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/resource-mediator-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
    "trust.d/package-authority-v2.pem": frozenset({0o400, 0o440, 0o600, 0o640}),
}
NATIVE_SIZE_LIMITS: Final = {
    name: (65_536 if name == "build-receipt.json" else installation.MAX_FILE_BYTES)
    for name in NATIVE_FILES
}
TEMPLATE_SIZE_LIMITS: Final = {name: 1_048_576 for name in TEMPLATE_FILES}
PRIVATE_SIZE_LIMITS: Final = {name: 1_048_576 for name in PRIVATE_FILES}

ROLE_SOURCE: Final[dict[str, tuple[str, str]]] = {
    "supervisor-executable": (
        "native",
        "propertyquarry-release-supervisor-v2",
    ),
    "controller-executable": (
        "native",
        "propertyquarry-release-controller-v2",
    ),
    "watchdog-executable": (
        "native",
        "propertyquarry-release-watchdog-v2",
    ),
    "systemd-socket-unit": (
        "template",
        "systemd/propertyquarry-release-control-v2.socket",
    ),
    "systemd-controller-template-unit": (
        "template",
        "systemd/propertyquarry-release-control-v2@.service",
    ),
    "systemd-watchdog-unit": (
        "template",
        "systemd/propertyquarry-release-watchdog-v2.service",
    ),
    "sysusers-config": (
        "template",
        "sysusers.d/propertyquarry-release-control-v2.conf",
    ),
    "tmpfiles-config": (
        "template",
        "tmpfiles.d/propertyquarry-release-control-v2.conf",
    ),
    "controller-schema": ("template", "schema/controller-v2.schema.json"),
    "watchdog-schema": ("template", "schema/watchdog-v2.schema.json"),
    "controller-config": ("private", "controller-v2.json"),
    "watchdog-config": ("private", "watchdog-v2.json"),
    "root-policy": ("private", "policy-v2.json"),
    "request-trust-root": ("private", "trust.d/request-authority-v2.pem"),
    "response-trust-root": ("private", "trust.d/response-authority-v2.pem"),
    "lifecycle-cas-trust-root": ("private", "trust.d/lifecycle-cas-v2.pem"),
    "evidence-trust-root": ("private", "trust.d/evidence-authority-v2.pem"),
    "resource-mediator-trust-root": (
        "private",
        "trust.d/resource-mediator-v2.pem",
    ),
    "package-trust-root": ("private", "trust.d/package-authority-v2.pem"),
}

_NATIVE_RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "authoritative",
        "production_ready",
        "reproducible_double_build",
        "distinct_absolute_source_roots",
        "isolated_build_caches",
        "independent_toolchain_extractions",
        "go_subprocess_environment_allowlisted",
        "go_subprocess_inherited_environment_cleared",
        "module_network_resolution_disabled",
        "host_network_namespace_isolated",
        "go_tests_passed_in_both_builds",
        "scratch_execution",
        "source_manifest_reverified_after_build",
        "receipt_published_last",
        "root_install_performed",
        "package_signature_verified",
        "builder_identity_authenticated",
        "toolchain",
        "toolchain_archive_bytes",
        "toolchain_archive_sha256",
        "go_binary_sha256",
        "source_manifest_sha256",
        "build_flags",
        "ldflags",
        "build_environment",
        "binary_mode",
        "binary_sizes",
        "binaries",
    }
)
_ROOT_POLICY_KEYS: Final = frozenset(
    {
        "schema",
        "identity",
        "required_checks",
        "decision_policy_digest",
        "max_request_ttl",
        "max_preflight_validity",
    }
)
_IDENTITY_KEYS: Final = frozenset(
    {
        "audience",
        "repository",
        "ref",
        "candidate_sha",
        "workflow_ref",
        "workflow_sha",
        "run_id",
        "run_attempt",
        "job",
        "environment",
    }
)
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")
_NATIVE_NAMES: Final = (
    "propertyquarry-release-controller-v2",
    "propertyquarry-release-supervisor-v2",
    "propertyquarry-release-watchdog-v2",
)
_NATIVE_BUILD_ENVIRONMENT: Final = {
    "CGO_ENABLED": "0",
    "GO111MODULE": "on",
    "GOARCH": "amd64",
    "GOAMD64": "v1",
    "GOENV": "off",
    "GOEXPERIMENT": "",
    "GOFIPS140": "off",
    "GOFLAGS": "",
    "GOOS": "linux",
    "GOPROXY": "off",
    "GOSUMDB": "off",
    "GOTELEMETRY": "off",
    "GOTOOLCHAIN": "local",
    "GOWORK": "off",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
}
NATIVE_SCRATCH_EXECUTION: Final = {
    "contract": "linux-amd64-static-et-exec-v1",
    "elf_class": "ELF64",
    "elf_data": "little-endian",
    "elf_machine": "Advanced Micro Devices X86-64",
    "elf_type": "ET_EXEC",
    "statically_linked": True,
    "pt_interp_absent": True,
    "dynamic_section_absent": True,
    "dt_needed_absent": True,
    "non_executable_stack": True,
    "writable_executable_load_segments_absent": True,
    "file_gate_passed": True,
    "readelf_gate_passed": True,
}


class PayloadError(ValueError):
    """A secret-free deterministic package assembly failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class FileSnapshot:
    relative: str
    data: bytes
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int
    identity: tuple[int, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class TreeSnapshot:
    root: str
    root_identity: tuple[int, ...]
    files: tuple[FileSnapshot, ...]
    directories: tuple[
        tuple[str, tuple[int, int, int, tuple[int, ...]]], ...
    ] = ()

    def by_relative(self) -> dict[str, FileSnapshot]:
        return {item.relative: item for item in self.files}


def _fail(code: str) -> None:
    raise PayloadError(code)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail("canonical-json-invalid")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_static_elf_bytes(data: bytes) -> None:
    """Validate the kernel-only Linux AMD64 executable contract from bytes."""

    try:
        if len(data) < 64:
            _fail("native-static-elf-invalid")
        header = struct.unpack_from("<16sHHIQQQIHHHHHH", data, 0)
        (
            identity,
            elf_type,
            machine,
            version,
            entry,
            program_offset,
            section_offset,
            _flags,
            header_size,
            program_entry_size,
            program_count,
            section_entry_size,
            section_count,
            section_name_index,
        ) = header
        if (
            identity[:7] != b"\x7fELF\x02\x01\x01"
            or identity[7] != 0
            or elf_type != 2
            or machine != 62
            or version != 1
            or entry == 0
            or header_size != 64
            or program_entry_size != 56
            or program_count < 2
            or program_count > 128
            or program_offset < header_size
            or program_offset > len(data)
            or program_count > (len(data) - program_offset) // program_entry_size
        ):
            _fail("native-static-elf-invalid")

        load_count = 0
        stack_count = 0
        executable_entry = False
        for index in range(program_count):
            offset = program_offset + index * program_entry_size
            (
                segment_type,
                segment_flags,
                file_offset,
                virtual_address,
                _physical_address,
                file_size,
                memory_size,
                alignment,
            ) = struct.unpack_from("<IIQQQQQQ", data, offset)
            if (
                file_size > memory_size
                or file_offset > len(data)
                or file_size > len(data) - file_offset
            ):
                _fail("native-static-elf-invalid")
            if segment_type in {2, 3}:  # PT_DYNAMIC and PT_INTERP.
                _fail("native-static-elf-invalid")
            if segment_type == 1:  # PT_LOAD.
                load_count += 1
                if segment_flags & 0x3 == 0x3:  # PF_W | PF_X.
                    _fail("native-static-elf-invalid")
                if alignment not in {0, 1}:
                    if alignment & (alignment - 1):
                        _fail("native-static-elf-invalid")
                    if file_offset % alignment != virtual_address % alignment:
                        _fail("native-static-elf-invalid")
                if (
                    segment_flags & 0x1
                    and virtual_address <= entry < virtual_address + memory_size
                ):
                    executable_entry = True
            elif segment_type == 0x6474E551:  # PT_GNU_STACK.
                stack_count += 1
                if segment_flags & 0x1:
                    _fail("native-static-elf-invalid")
        if load_count < 1 or stack_count != 1 or not executable_entry:
            _fail("native-static-elf-invalid")

        if section_count == 0:
            if section_offset != 0 or section_name_index != 0:
                _fail("native-static-elf-invalid")
        else:
            if (
                section_entry_size != 64
                or section_count > 4096
                or section_name_index == 0
                or section_name_index >= section_count
                or section_offset < header_size
                or section_offset > len(data)
                or section_count
                > (len(data) - section_offset) // section_entry_size
            ):
                _fail("native-static-elf-invalid")
            sections: list[tuple[int, int, int, int]] = []
            for index in range(section_count):
                offset = section_offset + index * section_entry_size
                (
                    name_offset,
                    section_type,
                    _section_flags,
                    _section_address,
                    file_offset,
                    file_size,
                    _section_link,
                    _section_info,
                    _section_alignment,
                    _section_entsize,
                ) = struct.unpack_from("<IIQQQQIIQQ", data, offset)
                if section_type == 6:  # SHT_DYNAMIC.
                    _fail("native-static-elf-invalid")
                if section_type != 8 and (
                    file_offset > len(data)
                    or file_size > len(data) - file_offset
                ):
                    _fail("native-static-elf-invalid")
                sections.append(
                    (name_offset, section_type, file_offset, file_size)
                )
            _, names_type, names_offset, names_size = sections[
                section_name_index
            ]
            if (
                names_type != 3  # SHT_STRTAB.
                or names_size == 0
                or names_offset > len(data)
                or names_size > len(data) - names_offset
            ):
                _fail("native-static-elf-invalid")
            names = data[names_offset : names_offset + names_size]
            for name_offset, _, _, _ in sections:
                if name_offset >= len(names):
                    _fail("native-static-elf-invalid")
                terminator = names.find(b"\x00", name_offset)
                if terminator < 0:
                    _fail("native-static-elf-invalid")
                if names[name_offset:terminator] in {b".interp", b".dynamic"}:
                    _fail("native-static-elf-invalid")
    except (OverflowError, struct.error):
        _fail("native-static-elf-invalid")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_uid,
        value.st_gid,
    )


def _parent_path_identity(value: os.stat_result) -> tuple[int, ...]:
    """Identity fields unchanged by this assembler's child publication."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_gid,
    )


def _stable_directory_identity_from_file_identity(
    value: tuple[int, ...],
) -> tuple[int, ...]:
    return (
        value[0],
        value[1],
        stat.S_IFMT(value[2]),
        stat.S_IMODE(value[2]),
        value[3],
        value[4],
        value[5],
    )


def _path_text(value: object, code: str) -> str:
    if type(value) is not str or not value or not os.path.isabs(value):
        _fail(code)
    if os.path.normpath(value) != value or value == "/":
        _fail(code)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail(code)
    try:
        if len(value.encode("utf-8")) > installation.MAX_PATH_BYTES:
            _fail(code)
    except UnicodeEncodeError:
        _fail(code)
    return value


def _open_flags(*, directory: bool, write: bool = False) -> int:
    if not all(hasattr(os, name) for name in ("O_CLOEXEC", "O_NOFOLLOW")):
        _fail("platform-open-flags-unavailable")
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            _fail("platform-open-flags-unavailable")
        flags |= os.O_DIRECTORY
    elif hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_controlled_directory(path: str, *, writable: bool) -> tuple[int, tuple[int, ...]]:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            _fail("directory-type-invalid")
        if os.path.realpath(path) != path:
            _fail("directory-symlink-rejected")
        if stat.S_IMODE(before.st_mode) & 0o022:
            _fail("directory-mode-unsafe")
        allowed_owners = {0, os.geteuid()}
        if before.st_uid not in allowed_owners:
            _fail("directory-owner-unsafe")
        if writable and before.st_uid != os.geteuid():
            _fail("output-parent-not-controlled")
        descriptor = os.open(path, _open_flags(directory=True))
        current = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(current):
            os.close(descriptor)
            _fail("directory-mutated")
        return descriptor, _directory_identity(current)
    except PayloadError:
        raise
    except OSError:
        _fail("directory-open-failed")


def _expected_directories(paths: Mapping[str, object]) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            _fail("internal-path-contract-invalid")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    return directories


def _expected_child_map(
    paths: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    directories = _expected_directories(paths)
    result: dict[str, dict[str, str]] = {"": {}}
    for relative in sorted(directories):
        parent, _, name = relative.rpartition("/")
        result.setdefault(parent, {})[name] = "directory"
        result.setdefault(relative, {})
    for relative in paths:
        parent, _, name = relative.rpartition("/")
        if name in result.setdefault(parent, {}):
            _fail("internal-path-contract-invalid")
        result[parent][name] = "file"
    return result


def _enumerate_tree(
    root_fd: int,
    expected_paths: Mapping[str, object],
) -> tuple[set[str], dict[str, tuple[int, int, int, tuple[int, ...]]]]:
    files: set[str] = set()
    directories: dict[str, tuple[int, int, int, tuple[int, ...]]] = {}
    expected_tree = _expected_child_map(expected_paths)

    def visit(directory_fd: int, prefix: str, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            _fail("input-tree-depth-invalid")
        expected_children = expected_tree[prefix]
        seen: set[str] = set()
        try:
            iterator = os.scandir(directory_fd)
        except OSError:
            _fail("input-tree-enumeration-failed")
        try:
            with iterator:
                for entry in iterator:
                    name = entry.name
                    if (
                        type(name) is not str
                        or name in {"", ".", ".."}
                        or "/" in name
                        or any(ord(character) < 0x20 for character in name)
                    ):
                        _fail("input-tree-name-invalid")
                    if name in seen or name not in expected_children:
                        _fail("input-tree-set-invalid")
                    seen.add(name)
                    relative = f"{prefix}/{name}" if prefix else name
                    try:
                        metadata = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError:
                        _fail("input-tree-stat-failed")
                    if stat.S_ISLNK(metadata.st_mode):
                        _fail("input-symlink-rejected")
                    expected_kind = expected_children[name]
                    if expected_kind == "directory":
                        if not stat.S_ISDIR(metadata.st_mode):
                            _fail("input-tree-type-invalid")
                        if stat.S_IMODE(metadata.st_mode) & 0o022:
                            _fail("input-directory-mode-unsafe")
                        if metadata.st_uid not in {0, os.geteuid()}:
                            _fail("input-directory-owner-unsafe")
                        child = -1
                        try:
                            child = os.open(
                                name,
                                _open_flags(directory=True),
                                dir_fd=directory_fd,
                            )
                            opened = os.fstat(child)
                        except OSError:
                            if child >= 0:
                                os.close(child)
                            _fail("input-directory-open-failed")
                        if _identity(metadata) != _identity(opened):
                            os.close(child)
                            _fail("input-concurrent-mutation")
                        directories[relative] = (
                            stat.S_IMODE(opened.st_mode),
                            opened.st_uid,
                            opened.st_gid,
                            _identity(opened),
                        )
                        try:
                            visit(child, relative, depth + 1)
                        finally:
                            os.close(child)
                    elif expected_kind == "file":
                        if not stat.S_ISREG(metadata.st_mode):
                            _fail("input-special-file-rejected")
                        files.add(relative)
                    else:
                        _fail("internal-path-contract-invalid")
        except PayloadError:
            raise
        except (OSError, RecursionError):
            _fail("input-tree-enumeration-failed")
        if seen != set(expected_children):
            _fail("input-tree-set-invalid")

    visit(root_fd, "", 0)
    return files, directories


def _open_relative_file(root_fd: int, relative: str) -> tuple[int, int]:
    parts = relative.split("/")
    current = os.dup(root_fd)
    descriptor = -1
    try:
        for part in parts[:-1]:
            metadata = os.stat(part, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                _fail("input-symlink-rejected")
            if not stat.S_ISDIR(metadata.st_mode):
                _fail("input-ancestor-type-invalid")
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                _fail("input-directory-mode-unsafe")
            if metadata.st_uid not in {0, os.geteuid()}:
                _fail("input-directory-owner-unsafe")
            following = os.open(
                part,
                _open_flags(directory=True),
                dir_fd=current,
            )
            try:
                opened = os.fstat(following)
            except OSError:
                os.close(following)
                raise
            if _identity(metadata) != _identity(opened):
                os.close(following)
                _fail("input-concurrent-mutation")
            os.close(current)
            current = following
        metadata = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            _fail("input-symlink-rejected")
        if not stat.S_ISREG(metadata.st_mode):
            _fail("input-special-file-rejected")
        descriptor = os.open(
            parts[-1],
            _open_flags(directory=False),
            dir_fd=current,
        )
        if _identity(metadata) != _identity(os.fstat(descriptor)):
            _fail("input-concurrent-mutation")
        return current, descriptor
    except PayloadError:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(current)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(current)
        _fail("input-file-open-failed")


def _snapshot_file(
    root_fd: int,
    relative: str,
    accepted_mode: int | frozenset[int] | None,
    maximum_size: int,
) -> FileSnapshot:
    parent_fd, descriptor = _open_relative_file(root_fd, relative)
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode):
            _fail("input-special-file-rejected")
        if before.st_nlink != 1:
            _fail("input-hardlink-rejected")
        if before.st_size <= 0 or before.st_size > maximum_size:
            _fail("input-size-invalid")
        if accepted_mode is not None:
            allowed = (
                {accepted_mode}
                if type(accepted_mode) is int
                else set(accepted_mode)
            )
            if mode not in allowed:
                _fail("input-mode-invalid")
        elif mode & 0o022 or not mode & 0o400:
            _fail("input-mode-unsafe")
        chunks: list[bytes] = []
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while total <= maximum_size:
            chunk = os.read(
                descriptor,
                min(65_536, maximum_size + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total != before.st_size or total > maximum_size:
            _fail("input-size-invalid")
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            _fail("input-concurrent-mutation")
        verify_parent, verify_descriptor = _open_relative_file(root_fd, relative)
        try:
            current = os.fstat(verify_descriptor)
        finally:
            os.close(verify_descriptor)
            os.close(verify_parent)
        if _identity(before) != _identity(current):
            _fail("input-concurrent-mutation")
        data = b"".join(chunks)
        return FileSnapshot(
            relative=relative,
            data=data,
            sha256=_digest(data),
            size=len(data),
            mode=mode,
            uid=before.st_uid,
            gid=before.st_gid,
            identity=_identity(before),
        )
    except PayloadError:
        raise
    except OSError:
        _fail("input-read-failed")
    finally:
        os.close(descriptor)
        os.close(parent_fd)


def _snapshot_tree(
    root: str,
    specification: Mapping[str, int | frozenset[int] | None],
    *,
    exact: bool = True,
    directory_modes: Mapping[str, int] | None = None,
    size_limits: Mapping[str, int] | None = None,
) -> TreeSnapshot:
    validated = _path_text(root, "input-root-invalid")
    if size_limits is not None and (
        set(size_limits) != set(specification)
        or any(type(value) is not int or value < 1 for value in size_limits.values())
    ):
        _fail("size-limit-contract-invalid")
    root_fd, _root_stable_identity = _open_controlled_directory(
        validated, writable=False
    )
    root_identity = _identity(os.fstat(root_fd))
    try:
        first_files: set[str] | None = None
        first_directories: dict[
            str, tuple[int, int, int, tuple[int, ...]]
        ] | None = None
        if exact:
            first_files, first_directories = _enumerate_tree(
                root_fd, specification
            )
            if first_files != set(specification) or set(
                first_directories
            ) != _expected_directories(specification):
                _fail("input-tree-set-invalid")
            if directory_modes is not None:
                if set(directory_modes) != set(first_directories):
                    _fail("directory-mode-contract-invalid")
                for relative, metadata in first_directories.items():
                    if (
                        metadata[0] != directory_modes[relative]
                        or metadata[1] != os.geteuid()
                        or metadata[2] != os.getegid()
                    ):
                        _fail("directory-metadata-invalid")
        snapshots = tuple(
            _snapshot_file(
                root_fd,
                relative,
                mode,
                (
                    size_limits[relative]
                    if size_limits is not None
                    else installation.MAX_FILE_BYTES
                ),
            )
            for relative, mode in specification.items()
        )
        if exact:
            second_files, second_directories = _enumerate_tree(
                root_fd, specification
            )
            if (
                second_files != first_files
                or second_directories != first_directories
            ):
                _fail("input-concurrent-mutation")
        if _identity(os.fstat(root_fd)) != root_identity:
            _fail("input-concurrent-mutation")
    finally:
        os.close(root_fd)
    verify_fd, _verify_stable_identity = _open_controlled_directory(
        validated, writable=False
    )
    verify_identity = _identity(os.fstat(verify_fd))
    os.close(verify_fd)
    if verify_identity != root_identity:
        _fail("input-concurrent-mutation")
    return TreeSnapshot(
        validated,
        root_identity,
        snapshots,
        tuple(sorted((first_directories or {}).items())),
    )


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("json-depth-invalid")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        ):
            _fail("json-string-invalid")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail("json-key-invalid")
            _validate_json_value(key, depth=depth + 1)
            _validate_json_value(item, depth=depth + 1)
        return
    _fail("json-type-invalid")


def _decode_json(raw: bytes, *, maximum: int = MAX_JSON_BYTES) -> object:
    if not raw or len(raw) > maximum:
        _fail("json-size-invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        ValueError,
        RecursionError,
    ):
        _fail("json-decode-invalid")
    _validate_json_value(value)
    return value


def _exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _exact_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _exact_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _validate_schema_instance(instance: object, schema: object, root: dict[str, object]) -> None:
    if type(schema) is not dict:
        _fail("private-json-schema-invalid")
    if "$ref" in schema:
        reference = schema["$ref"]
        if type(reference) is not str or not reference.startswith("#/$defs/"):
            _fail("private-json-schema-invalid")
        name = reference.removeprefix("#/$defs/")
        definitions = root.get("$defs")
        if type(definitions) is not dict or name not in definitions:
            _fail("private-json-schema-invalid")
        _validate_schema_instance(instance, definitions[name], root)
        return
    expected_type = schema.get("type")
    valid_type = {
        "object": type(instance) is dict,
        "array": type(instance) is list,
        "string": type(instance) is str,
        "integer": type(instance) is int,
        None: True,
    }.get(expected_type, False)
    if not valid_type:
        _fail("private-json-schema-invalid")
    if "const" in schema and not _exact_equal(instance, schema["const"]):
        _fail("private-json-schema-invalid")
    if type(instance) is dict:
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if type(required) is not list or type(properties) is not dict:
            _fail("private-json-schema-invalid")
        if any(type(name) is not str or name not in instance for name in required):
            _fail("private-json-schema-invalid")
        if schema.get("additionalProperties") is False and not set(instance) <= set(
            properties
        ):
            _fail("private-json-schema-invalid")
        for name, value in instance.items():
            if name in properties:
                _validate_schema_instance(value, properties[name], root)
    if type(instance) is str:
        maximum = schema.get("maxLength")
        if type(maximum) is int and len(instance) > maximum:
            _fail("private-json-schema-invalid")
        pattern = schema.get("pattern")
        if type(pattern) is str and re.search(pattern, instance) is None:
            _fail("private-json-schema-invalid")
    if type(instance) is int:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if type(minimum) is int and instance < minimum:
            _fail("private-json-schema-invalid")
        if type(maximum) is int and instance > maximum:
            _fail("private-json-schema-invalid")


def _validate_root_policy(raw: bytes) -> None:
    value = _decode_json(raw)
    if type(value) is not dict or set(value) != _ROOT_POLICY_KEYS:
        _fail("private-root-policy-invalid")
    identity = value["identity"]
    checks = value["required_checks"]
    if (
        value["schema"] != "propertyquarry.release-root-policy.v2"
        or type(identity) is not dict
        or set(identity) != _IDENTITY_KEYS
        or type(checks) is not list
        or not checks
        or any(
            type(check) is not str or _IDENTIFIER_RE.fullmatch(check) is None
            for check in checks
        )
        or len(set(checks)) != len(checks)
        or type(value["decision_policy_digest"]) is not str
        or _DIGEST_RE.fullmatch(value["decision_policy_digest"]) is None
        or type(value["max_request_ttl"]) is not int
        or value["max_request_ttl"] < 1
        or type(value["max_preflight_validity"]) is not int
        or value["max_preflight_validity"] < 1
    ):
        _fail("private-root-policy-invalid")
    for name, item in identity.items():
        if name == "run_attempt":
            if type(item) is not int or item < 1:
                _fail("private-root-policy-invalid")
        elif type(item) is not str or _IDENTIFIER_RE.fullmatch(item) is None:
            _fail("private-root-policy-invalid")
    for name in ("candidate_sha", "workflow_sha"):
        if re.fullmatch(r"[0-9a-f]{40}", identity[name]) is None:
            _fail("private-root-policy-invalid")
    if raw != _canonical_bytes(value):
        _fail("private-root-policy-not-canonical")


def _validate_private_documents(
    private: TreeSnapshot,
    templates: TreeSnapshot,
) -> None:
    private_files = private.by_relative()
    template_files = templates.by_relative()
    for config_name, schema_name in (
        ("controller-v2.json", "schema/controller-v2.schema.json"),
        ("watchdog-v2.json", "schema/watchdog-v2.schema.json"),
    ):
        instance = _decode_json(private_files[config_name].data)
        schema = _decode_json(template_files[schema_name].data)
        if type(schema) is not dict:
            _fail("private-json-schema-invalid")
        _validate_schema_instance(instance, schema, schema)
    _validate_root_policy(private_files["policy-v2.json"].data)


def _validate_service_gid(value: object) -> int:
    if type(value) is not int or value < 1 or value > MAX_SERVICE_GID:
        _fail("service-gid-invalid")
    return value


def _validate_native_build_receipt(native: TreeSnapshot) -> None:
    native_files = native.by_relative()
    value = _decode_json(
        native_files["build-receipt.json"].data,
        maximum=65_536,
    )
    if type(value) is not dict or set(value) != _NATIVE_RECEIPT_KEYS:
        _fail("native-build-receipt-invalid")
    expected_digests = {
        name: native_files[name].sha256 for name in _NATIVE_NAMES
    }
    expected_sizes = {name: native_files[name].size for name in _NATIVE_NAMES}
    source_digest = value["source_manifest_sha256"]
    valid = (
        value["schema"]
        == "propertyquarry.release-control.native-build-receipt.v2"
        and value["authoritative"] is False
        and value["production_ready"] is False
        and value["reproducible_double_build"] is True
        and value["distinct_absolute_source_roots"] is True
        and value["isolated_build_caches"] is True
        and value["independent_toolchain_extractions"] is True
        and value["go_subprocess_environment_allowlisted"] is True
        and value["go_subprocess_inherited_environment_cleared"] is True
        and value["module_network_resolution_disabled"] is True
        and value["host_network_namespace_isolated"] is False
        and value["go_tests_passed_in_both_builds"] is True
        and _exact_equal(value["scratch_execution"], NATIVE_SCRATCH_EXECUTION)
        and value["source_manifest_reverified_after_build"] is True
        and value["receipt_published_last"] is True
        and value["root_install_performed"] is False
        and value["package_signature_verified"] is False
        and value["builder_identity_authenticated"] is False
        and value["toolchain"] == "go1.26.5 linux/amd64"
        and value["toolchain_archive_bytes"] == 66_879_095
        and value["toolchain_archive_sha256"]
        == "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
        and value["go_binary_sha256"]
        == "sha256:8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9"
        and type(source_digest) is str
        and _DIGEST_RE.fullmatch(source_digest) is not None
        and _exact_equal(
            value["build_flags"],
            ["-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"],
        )
        and value["ldflags"]
        == (
            "-buildid= -linkmode=internal -X propertyquarry.local/"
            "release-control-v2/internal/"
            "releasecontrol.SourceManifestDigest="
            + source_digest
            + " -X propertyquarry.local/release-control-v2/internal/"
            "releasecontrol.ScratchExecutionContract="
            + NATIVE_SCRATCH_EXECUTION["contract"]
        )
        and _exact_equal(value["build_environment"], _NATIVE_BUILD_ENVIRONMENT)
        and value["binary_mode"] == "0755"
        and _exact_equal(value["binary_sizes"], expected_sizes)
        and _exact_equal(value["binaries"], expected_digests)
    )
    if not valid:
        _fail("native-build-receipt-invalid")
    for name in _NATIVE_NAMES:
        _validate_static_elf_bytes(native_files[name].data)


def _make_private_temp(parent_fd: int, parent: str, prefix: str) -> tuple[str, str, int]:
    for _attempt in range(128):
        name = f".{prefix}.{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError:
            _fail("temporary-directory-create-failed")
        path = os.path.join(parent, name)
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                _open_flags(directory=True),
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
            ):
                _fail("temporary-directory-metadata-invalid")
        except (OSError, PayloadError) as error:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                _fail("temporary-cleanup-failed")
            if isinstance(error, PayloadError):
                raise
            _fail("temporary-directory-open-failed")
        return name, path, descriptor
    _fail("temporary-name-exhausted")


def _directory_object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _erase_directory_contents_fd(directory_fd: int) -> None:
    """Erase only descendants reached through an already-pinned directory fd."""

    if not hasattr(os, "O_PATH"):
        _fail("platform-open-flags-unavailable")

    def visit(current_fd: int, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            _fail("temporary-cleanup-depth-invalid")
        try:
            with os.scandir(current_fd) as iterator:
                entries = sorted(tuple(iterator), key=lambda entry: entry.name)
        except OSError:
            _fail("temporary-cleanup-enumeration-failed")
        for entry in entries:
            name = entry.name
            if (
                type(name) is not str
                or name in {"", ".", ".."}
                or "/" in name
                or any(ord(character) < 0x20 for character in name)
            ):
                _fail("temporary-cleanup-name-invalid")
            try:
                metadata = os.stat(
                    name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except OSError:
                _fail("temporary-cleanup-entry-mutated")
            child_fd = -1
            try:
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(
                        name,
                        _open_flags(directory=True),
                        dir_fd=current_fd,
                    )
                    opened = os.fstat(child_fd)
                    if _identity(metadata) != _identity(opened):
                        _fail("temporary-cleanup-entry-mutated")
                    visit(child_fd, depth + 1)
                    current = os.stat(
                        name,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or _directory_object_identity(current)
                        != _directory_object_identity(opened)
                    ):
                        _fail("temporary-cleanup-entry-mutated")
                    os.rmdir(name, dir_fd=current_fd)
                    if os.fstat(child_fd).st_nlink != 0:
                        _fail("temporary-cleanup-entry-mutated")
                else:
                    child_fd = os.open(
                        name,
                        os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=current_fd,
                    )
                    opened = os.fstat(child_fd)
                    if _identity(metadata) != _identity(opened):
                        _fail("temporary-cleanup-entry-mutated")
                    current = os.stat(
                        name,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if _identity(current) != _identity(opened):
                        _fail("temporary-cleanup-entry-mutated")
                    os.unlink(name, dir_fd=current_fd)
                    if os.fstat(child_fd).st_nlink != max(opened.st_nlink - 1, 0):
                        _fail("temporary-cleanup-entry-mutated")
            except PayloadError:
                raise
            except OSError:
                _fail("temporary-cleanup-entry-failed")
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
        try:
            with os.scandir(current_fd) as iterator:
                if next(iterator, None) is not None:
                    _fail("temporary-cleanup-directory-not-empty")
        except PayloadError:
            raise
        except OSError:
            _fail("temporary-cleanup-enumeration-failed")

    visit(directory_fd, 0)


def _remove_temp(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, ...],
) -> None:
    descriptor = -1
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("temporary-cleanup-target-replaced")
        descriptor = os.open(
            name,
            _open_flags(directory=True),
            dir_fd=parent_fd,
        )
        if (
            _directory_identity(metadata) != expected_identity
            or _directory_identity(os.fstat(descriptor)) != expected_identity
        ):
            _fail("temporary-cleanup-target-replaced")
        pinned_object = _directory_object_identity(os.fstat(descriptor))
        _erase_directory_contents_fd(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _directory_object_identity(current) != pinned_object
            or _directory_object_identity(os.fstat(descriptor)) != pinned_object
        ):
            _fail("temporary-cleanup-target-replaced")
        os.rmdir(name, dir_fd=parent_fd)
        if os.fstat(descriptor).st_nlink != 0:
            _fail("temporary-cleanup-target-replaced")
    except FileNotFoundError:
        _fail("temporary-cleanup-target-replaced")
    except PayloadError:
        raise
    except OSError:
        _fail("temporary-cleanup-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_mode(relative: str) -> int:
    if relative in {
        "etc/propertyquarry-release-control",
        "etc/propertyquarry-release-control/trust.d",
    }:
        return 0o750
    return 0o755


def _ensure_output_parent(root_fd: int, components: list[str]) -> int:
    current = os.dup(root_fd)
    built: list[str] = []
    try:
        for component in components:
            built.append(component)
            expected_mode = _directory_mode("/".join(built))
            created = False
            try:
                os.mkdir(
                    component,
                    expected_mode,
                    dir_fd=current,
                )
                created = True
                os.fsync(current)
            except FileExistsError:
                pass
            except OSError:
                _fail("payload-directory-create-failed")
            following = -1
            try:
                metadata = os.stat(component, dir_fd=current, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    _fail("payload-directory-type-invalid")
                following = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=current,
                )
                if created:
                    os.fchmod(following, expected_mode)
                opened = os.fstat(following)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != expected_mode
                    or opened.st_uid != os.geteuid()
                    or opened.st_gid != os.getegid()
                    or (metadata.st_dev, metadata.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    _fail("payload-directory-metadata-invalid")
            except PayloadError:
                if following >= 0:
                    os.close(following)
                raise
            except OSError:
                if following >= 0:
                    os.close(following)
                _fail("payload-directory-open-failed")
            os.close(current)
            current = following
        return current
    except PayloadError:
        os.close(current)
        raise


def _write_file_at(root_fd: int, relative: str, data: bytes, mode: int) -> None:
    parts = relative.split("/")
    parent_fd = _ensure_output_parent(root_fd, parts[:-1])
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                parts[-1],
                _open_flags(directory=False, write=True) | os.O_CREAT | os.O_EXCL,
                mode,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    _fail("payload-write-failed")
                offset += written
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != mode
                or metadata.st_size != len(data)
            ):
                _fail("payload-file-metadata-invalid")
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = bytearray()
            while len(observed) <= len(data):
                chunk = os.read(descriptor, min(65_536, len(data) + 1 - len(observed)))
                if not chunk:
                    break
                observed.extend(chunk)
            if bytes(observed) != data:
                _fail("payload-copy-mismatch")
            os.fsync(parent_fd)
        except FileExistsError:
            _fail("payload-path-collision")
        except PayloadError:
            raise
        except OSError:
            _fail("payload-write-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _role_material(
    native: TreeSnapshot,
    templates: TreeSnapshot,
    private: TreeSnapshot,
) -> dict[str, FileSnapshot]:
    sources = {
        "native": native.by_relative(),
        "template": templates.by_relative(),
        "private": private.by_relative(),
    }
    return {
        role: sources[kind][relative]
        for role, (kind, relative) in ROLE_SOURCE.items()
    }


def _manifest_document(
    materials: Mapping[str, FileSnapshot], service_gid: int
) -> dict[str, object]:
    return {
        "schema": installation.SCHEMA,
        "version": installation.VERSION,
        "roles": [
            {
                "role": contract.role,
                "path": contract.path,
                "sha256": materials[contract.role].sha256,
                "size": materials[contract.role].size,
                "mode": contract.mode,
                "uid": 0,
                "gid": (
                    service_gid
                    if contract.role in installation.SERVICE_GROUP_ROLES
                    else 0
                ),
            }
            for contract in installation.ROLE_CONTRACTS
        ],
    }


def _projection_manifest(
    manifest: dict[str, object], staged: TreeSnapshot
) -> dict[str, object]:
    staged_by_path = staged.by_relative()
    roles: list[dict[str, object]] = []
    for expected in manifest["roles"]:  # type: ignore[index,union-attr]
        entry = dict(expected)  # type: ignore[arg-type]
        observed = staged_by_path[entry["path"][1:]]  # type: ignore[index]
        entry["uid"] = observed.uid
        entry["gid"] = observed.gid
        roles.append(entry)
    return {
        "schema": manifest["schema"],
        "version": manifest["version"],
        "roles": roles,
    }


def _material_digest(snapshot: TreeSnapshot) -> str:
    return _digest(
        _canonical_bytes(
            [
                {
                    "path": item.relative,
                    "sha256": item.sha256,
                    "size": item.size,
                    "mode": item.mode,
                }
                for item in snapshot.files
            ]
        )
    )


def _receipt_document(
    *,
    native: TreeSnapshot,
    templates: TreeSnapshot,
    private: TreeSnapshot,
    manifest_digest: str,
    service_gid: int,
    audit: installation.InstallationAuditResult,
) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "version": 2,
        "authoritative": False,
        "production_ready": False,
        "readiness_authority": False,
        "payload_signed": False,
        "installs_or_repairs": False,
        "writes_payload_output": True,
        "payload_material_writes_only_within_output_parent": True,
        "performs_installation_writes": False,
        "root_install_performed": False,
        "package_signature_verified": False,
        "verifies_signatures": False,
        "builder_identity_authenticated": False,
        "input_authentication_verified": False,
        "native_bundle_authenticated": False,
        "private_material_authenticated": False,
        "production_ownership_verified": False,
        "receipt_published_last": True,
        "role_count": len(installation.ROLE_CONTRACTS),
        "service_gid_projection": service_gid,
        "input_integrity": {
            "native_build_receipt_sha256": native.by_relative()[
                "build-receipt.json"
            ].sha256,
            "native_bundle_material_sha256": _material_digest(native),
            "package_templates_material_sha256": _material_digest(templates),
            "private_material_sha256": _material_digest(private),
        },
        "installation_manifest_sha256": manifest_digest,
        "simulation_audit": {
            "mode": "simulation",
            "ownership": "actual-staging-ownership-projection-only",
            "disposition": audit.disposition,
            "all_files_match_expectations": audit.all_files_match_expectations,
            "observed_role_count": len(audit.observed_files),
            "blocker_count": len(audit.blockers),
            "authoritative": False,
            "performs_writes": False,
            "verifies_signatures": False,
            "readiness_authority": False,
        },
    }


def _final_directory_modes() -> dict[str, int]:
    modes = {"rootfs": 0o755}
    for contract in installation.ROLE_CONTRACTS:
        parts = contract.path[1:].split("/")
        for index in range(1, len(parts)):
            role_directory = "/".join(parts[:index])
            modes[f"rootfs/{role_directory}"] = _directory_mode(role_directory)
    return modes


def _stable_file_identity(snapshot: FileSnapshot) -> tuple[int, ...]:
    identity = snapshot.identity
    return (
        identity[0],
        identity[1],
        stat.S_IFMT(identity[2]),
        stat.S_IMODE(identity[2]),
        identity[3],
    )


def _verify_final_payload_tree(
    *,
    output_temp_path: str,
    output_temp_identity: tuple[int, ...],
    materials: Mapping[str, FileSnapshot],
    manifest_bytes: bytes,
    receipt_bytes: bytes,
    audited_rootfs: TreeSnapshot,
) -> None:
    specification: dict[str, int | frozenset[int] | None] = {
        f"rootfs/{contract.path[1:]}": contract.mode
        for contract in installation.ROLE_CONTRACTS
    }
    specification["installation-manifest.v2.json"] = 0o644
    specification["package-payload-receipt.v2.json"] = 0o644
    final = _snapshot_tree(
        output_temp_path,
        specification,
        directory_modes=_final_directory_modes(),
    )
    if (
        _stable_directory_identity_from_file_identity(final.root_identity)
        != output_temp_identity
    ):
        _fail("payload-temp-root-replaced")
    expected_bytes = {
        f"rootfs/{contract.path[1:]}": materials[contract.role].data
        for contract in installation.ROLE_CONTRACTS
    }
    expected_bytes["installation-manifest.v2.json"] = manifest_bytes
    expected_bytes["package-payload-receipt.v2.json"] = receipt_bytes
    final_files = final.by_relative()
    for relative, snapshot in final_files.items():
        if snapshot.data != expected_bytes[relative]:
            _fail("final-payload-byte-mismatch")

    audited_files = audited_rootfs.by_relative()
    for contract in installation.ROLE_CONTRACTS:
        role_relative = contract.path[1:]
        audited = audited_files[role_relative]
        current = final_files[f"rootfs/{role_relative}"]
        if (
            current.uid != audited.uid
            or current.gid != audited.gid
            or _stable_file_identity(current) != _stable_file_identity(audited)
        ):
            _fail("audited-file-metadata-changed")

    for relative in (
        "installation-manifest.v2.json",
        "package-payload-receipt.v2.json",
    ):
        metadata = final_files[relative]
        identity = metadata.identity
        if (
            metadata.uid != os.geteuid()
            or metadata.gid != os.getegid()
            or metadata.mode != 0o644
            or stat.S_IFMT(identity[2]) != stat.S_IFREG
            or stat.S_IMODE(identity[2]) != 0o644
            or identity[3] != 1
        ):
            _fail("payload-metadata-file-invalid")

    audited_directories = {
        "rootfs": audited_rootfs.root_identity,
        **{
            f"rootfs/{relative}": metadata[3]
            for relative, metadata in audited_rootfs.directories
        },
    }
    final_directories = {
        relative: metadata[3] for relative, metadata in final.directories
    }
    for relative, identity in audited_directories.items():
        if final_directories.get(relative) != identity:
            _fail("audited-directory-replaced")


def _revalidate_temp_root(
    parent_fd: int,
    name: str,
    pinned_fd: int,
    expected_identity: tuple[int, ...],
) -> None:
    try:
        pinned = os.fstat(pinned_fd)
        descriptor = os.open(
            name,
            _open_flags(directory=True),
            dir_fd=parent_fd,
        )
        try:
            current = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("payload-temp-root-revalidation-failed")
    if (
        _directory_identity(pinned) != expected_identity
        or _directory_identity(current) != expected_identity
        or stat.S_IMODE(current.st_mode) != 0o700
        or current.st_uid != os.geteuid()
        or current.st_gid != os.getegid()
    ):
        _fail("payload-temp-root-replaced")


def _revalidate_parent_path(
    parent: str,
    parent_fd: int,
    expected_identity: tuple[int, ...],
) -> None:
    try:
        path_metadata = os.lstat(parent)
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(
            path_metadata.st_mode
        ):
            _fail("output-parent-replaced")
        if os.path.realpath(parent) != parent:
            _fail("output-parent-replaced")
        descriptor = os.open(parent, _open_flags(directory=True))
        try:
            reopened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        pinned = os.fstat(parent_fd)
    except PayloadError:
        raise
    except OSError:
        _fail("output-parent-revalidation-failed")
    if not (
        _parent_path_identity(path_metadata)
        == _parent_path_identity(reopened)
        == _parent_path_identity(pinned)
        == expected_identity
    ):
        _fail("output-parent-replaced")


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = library.renameat2
    except (OSError, AttributeError):
        _fail("renameat2-unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            _fail("output-exists")
        if error == errno.EXDEV:
            _fail("output-cross-device")
        _fail("output-publish-failed")


def assemble_payload(
    *,
    native_bundle: str,
    private_bundle: str,
    service_gid: object,
    output: str,
    phase_hook: Callable[[str], None] | None = None,
) -> str:
    """Assemble and atomically publish an integrity-bound simulation payload."""

    gid = _validate_service_gid(service_gid)
    output_path = _path_text(output, "output-path-invalid")
    parent = os.path.dirname(output_path)
    output_name = os.path.basename(output_path)
    if output_name in {"", ".", ".."}:
        _fail("output-path-invalid")
    parent_fd, _initial_parent_identity = _open_controlled_directory(
        parent, writable=True
    )
    parent_identity: tuple[int, ...] | None = None
    output_temp_name: str | None = None
    output_temp_fd = -1
    output_temp_identity: tuple[int, ...] | None = None
    published = False

    def phase(name: str) -> None:
        if phase_hook is not None:
            phase_hook(name)

    try:
        try:
            if os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False):
                _fail("output-exists")
        except FileNotFoundError:
            pass
        except PayloadError:
            raise
        except OSError:
            _fail("output-preflight-failed")

        native = _snapshot_tree(
            _path_text(native_bundle, "native-bundle-path-invalid"),
            NATIVE_FILES,
            size_limits=NATIVE_SIZE_LIMITS,
        )
        _validate_native_build_receipt(native)
        templates = _snapshot_tree(
            str(TEMPLATE_ROOT),
            TEMPLATE_FILES,
            size_limits=TEMPLATE_SIZE_LIMITS,
        )
        private = _snapshot_tree(
            _path_text(private_bundle, "private-bundle-path-invalid"),
            PRIVATE_FILES,
            size_limits=PRIVATE_SIZE_LIMITS,
        )
        _validate_private_documents(private, templates)
        phase("inputs-snapshotted")

        if _snapshot_tree(
            native.root, NATIVE_FILES, size_limits=NATIVE_SIZE_LIMITS
        ) != native:
            _fail("native-bundle-concurrent-mutation")
        if _snapshot_tree(
            templates.root, TEMPLATE_FILES, size_limits=TEMPLATE_SIZE_LIMITS
        ) != templates:
            _fail("template-concurrent-mutation")
        if _snapshot_tree(
            private.root, PRIVATE_FILES, size_limits=PRIVATE_SIZE_LIMITS
        ) != private:
            _fail("private-bundle-concurrent-mutation")
        phase("native-integrity-validated")

        output_temp_name, output_temp_path, output_temp_fd = _make_private_temp(
            parent_fd, parent, f"{output_name}.assembling"
        )
        parent_identity = _parent_path_identity(os.fstat(parent_fd))
        output_temp_identity = _directory_identity(os.fstat(output_temp_fd))
        rootfs_fd = -1
        try:
            os.mkdir("rootfs", 0o755, dir_fd=output_temp_fd)
            os.fsync(output_temp_fd)
            rootfs_fd = os.open(
                "rootfs",
                _open_flags(directory=True),
                dir_fd=output_temp_fd,
            )
            os.fchmod(rootfs_fd, 0o755)
            rootfs_metadata = os.fstat(rootfs_fd)
            if (
                not stat.S_ISDIR(rootfs_metadata.st_mode)
                or stat.S_IMODE(rootfs_metadata.st_mode) != 0o755
                or rootfs_metadata.st_uid != os.geteuid()
                or rootfs_metadata.st_gid != os.getegid()
            ):
                _fail("payload-rootfs-metadata-invalid")
        except PayloadError:
            if rootfs_fd >= 0:
                os.close(rootfs_fd)
            raise
        except OSError:
            if rootfs_fd >= 0:
                os.close(rootfs_fd)
            _fail("payload-rootfs-create-failed")
        materials = _role_material(native, templates, private)
        try:
            for contract in installation.ROLE_CONTRACTS:
                material = materials[contract.role]
                _write_file_at(
                    rootfs_fd,
                    contract.path[1:],
                    material.data,
                    contract.mode,
                )
        finally:
            os.close(rootfs_fd)
        phase("role-files-copied")

        manifest = _manifest_document(materials, gid)
        manifest_bytes = installation.canonical_manifest_bytes(manifest)
        if len(manifest_bytes) > installation.MAX_MANIFEST_BYTES:
            _fail("installation-manifest-oversized")
        manifest_digest = _digest(manifest_bytes)
        _write_file_at(
            output_temp_fd,
            "installation-manifest.v2.json",
            manifest_bytes,
            0o644,
        )
        phase("manifest-written")

        rootfs_path = os.path.join(output_temp_path, "rootfs")
        rootfs_specification = {
            contract.path[1:]: contract.mode
            for contract in installation.ROLE_CONTRACTS
        }
        staged_payload = _snapshot_tree(rootfs_path, rootfs_specification)
        projection = _projection_manifest(manifest, staged_payload)
        for production_entry, simulation_entry in zip(
            manifest["roles"], projection["roles"], strict=True  # type: ignore[index]
        ):
            if {
                key: production_entry[key]
                for key in production_entry
                if key not in {"uid", "gid"}
            } != {
                key: simulation_entry[key]
                for key in simulation_entry
                if key not in {"uid", "gid"}
            }:
                _fail("simulation-projection-invalid")
        audit = installation.audit_installation(
            projection,
            mode="simulation",
            rootfs=rootfs_path,
        )
        if (
            not audit.all_files_match_expectations
            or audit.mode != "simulation"
            or audit.authoritative is not False
            or audit.performs_writes is not False
            or audit.verifies_signatures is not False
            or audit.readiness_authority is not False
            or len(audit.observed_files) != len(installation.ROLE_CONTRACTS)
        ):
            _fail("simulation-audit-failed")
        phase("simulation-audited")

        if _snapshot_tree(
            native.root, NATIVE_FILES, size_limits=NATIVE_SIZE_LIMITS
        ) != native:
            _fail("native-bundle-concurrent-mutation")
        if _snapshot_tree(
            templates.root, TEMPLATE_FILES, size_limits=TEMPLATE_SIZE_LIMITS
        ) != templates:
            _fail("template-concurrent-mutation")
        if _snapshot_tree(
            private.root, PRIVATE_FILES, size_limits=PRIVATE_SIZE_LIMITS
        ) != private:
            _fail("private-bundle-concurrent-mutation")
        _revalidate_parent_path(parent, parent_fd, parent_identity)
        phase("inputs-reverified")

        receipt = _receipt_document(
            native=native,
            templates=templates,
            private=private,
            manifest_digest=manifest_digest,
            service_gid=gid,
            audit=audit,
        )
        receipt_bytes = _canonical_bytes(receipt)
        if set(os.listdir(output_temp_fd)) != {
            "rootfs",
            "installation-manifest.v2.json",
        }:
            _fail("receipt-publication-order-invalid")
        _write_file_at(
            output_temp_fd,
            "package-payload-receipt.v2.json",
            receipt_bytes,
            0o644,
        )
        phase("receipt-written-last")
        if set(os.listdir(output_temp_fd)) != {
            "rootfs",
            "installation-manifest.v2.json",
            "package-payload-receipt.v2.json",
        }:
            _fail("payload-top-level-set-invalid")
        os.fsync(output_temp_fd)
        output_temp_identity = _directory_identity(os.fstat(output_temp_fd))
        _revalidate_parent_path(parent, parent_fd, parent_identity)
        _revalidate_temp_root(
            parent_fd,
            output_temp_name,
            output_temp_fd,
            output_temp_identity,
        )
        _verify_final_payload_tree(
            output_temp_path=output_temp_path,
            output_temp_identity=output_temp_identity,
            materials=materials,
            manifest_bytes=manifest_bytes,
            receipt_bytes=receipt_bytes,
            audited_rootfs=staged_payload,
        )
        _revalidate_temp_root(
            parent_fd,
            output_temp_name,
            output_temp_fd,
            output_temp_identity,
        )
        _revalidate_parent_path(parent, parent_fd, parent_identity)
        _rename_noreplace(parent_fd, output_temp_name, output_name)
        published = True
        _revalidate_temp_root(
            parent_fd,
            output_name,
            output_temp_fd,
            output_temp_identity,
        )
        _verify_final_payload_tree(
            output_temp_path=output_path,
            output_temp_identity=output_temp_identity,
            materials=materials,
            manifest_bytes=manifest_bytes,
            receipt_bytes=receipt_bytes,
            audited_rootfs=staged_payload,
        )
        _revalidate_parent_path(parent, parent_fd, parent_identity)
        try:
            os.fsync(parent_fd)
        except OSError:
            _fail("output-parent-durability-unknown")
        os.close(output_temp_fd)
        output_temp_fd = -1
        return output_path
    finally:
        try:
            if output_temp_fd >= 0:
                try:
                    output_temp_identity = _directory_identity(
                        os.fstat(output_temp_fd)
                    )
                except OSError:
                    output_temp_identity = None
                os.close(output_temp_fd)
            if output_temp_name is not None and not published:
                if output_temp_identity is None:
                    _fail("temporary-cleanup-identity-missing")
                _remove_temp(parent_fd, output_temp_name, output_temp_identity)
        finally:
            os.close(parent_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assemble an unsigned PropertyQuarry v2 payload tree."
    )
    parser.add_argument("--native-bundle", required=True)
    parser.add_argument("--private-bundle", required=True)
    parser.add_argument("--service-gid", required=True, type=int)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = assemble_payload(
            native_bundle=arguments.native_bundle,
            private_bundle=arguments.private_bundle,
            service_gid=arguments.service_gid,
            output=arguments.output,
        )
    except PayloadError as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
