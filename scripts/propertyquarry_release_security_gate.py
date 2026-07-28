#!/usr/bin/env python3
"""Reproducible dependency, image, and SBOM gate for PropertyQuarry releases.

The controller never installs scanners, pulls images, or updates vulnerability
databases. Flagship mode authenticates pip-audit inside the pinned release
Python tree, root-owned checksum-pinned Syft and Trivy binaries, fresh
root-owned Trivy databases, and already-local digest-pinned PropertyQuarry
images. Non-flagship mode records unavailable/advisory results without
breaking ordinary local development.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

if __package__:
    from scripts.propertyquarry_secure_file_io import (
        OutputExistsError,
        SecureFileIOError,
        atomic_write_bytes,
    )
else:
    from propertyquarry_secure_file_io import (
        OutputExistsError,
        SecureFileIOError,
        atomic_write_bytes,
    )


APP_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = APP_ROOT / "ea" / "requirements.lock"
DEFAULT_WAIVERS_PATH = APP_ROOT / "config" / "propertyquarry_security_waivers.json"
RECEIPT_SCHEMA = "propertyquarry.release_security_receipt.v1"
WAIVER_SCHEMA = "propertyquarry.security_waivers.v1"
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
IMAGE_REF_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")
LOCK_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==([^\s;]+)$"
)
PACKAGE_NORMALIZE_RE = re.compile(r"[-_.]+")
WAIVER_ID_RE = re.compile(r"^PQSEC-[0-9]{4}-[0-9]{3,}$")
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
MAX_WAIVER_LIFETIME = timedelta(days=30)
REQUIRED_TOOLS = ("pip-audit", "syft", "trivy")
RELEASE_PYTHON = (
    APP_ROOT / ".propertyquarry_release_tools" / "release-venv" / "bin" / "python"
)
RELEASE_PYTHON_LAUNCHER = APP_ROOT / "scripts" / "propertyquarry_release_python.sh"
PINNED_RELEASE_PYTHON_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
PINNED_PIP_AUDIT_VERSION = "2.10.1"
SECURITY_RUNTIME_SCHEMA = "propertyquarry.security-runtime.v1"
SECURITY_RUNTIME_MANIFEST = Path(
    "/etc/propertyquarry/security-runtime.v1.json"
)
TRIVY_CACHE_DIR = Path("/var/lib/propertyquarry-security/trivy")
MAX_SECURITY_RUNTIME_MANIFEST_BYTES = 64 * 1024
MAX_REQUIREMENTS_LOCK_BYTES = 16 * 1024 * 1024
MAX_SCANNER_BINARY_BYTES = 512 * 1024 * 1024
MAX_SCANNER_CONFIG_BYTES = 64 * 1024
MAX_TRIVY_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
MAX_TRIVY_RUNTIME_CACHE_BYTES = 512 * 1024 * 1024
MAX_TRIVY_RUNTIME_CACHE_MEMBERS = 64
DATABASE_CLOCK_SKEW = timedelta(minutes=5)
PINNED_SCANNERS: Mapping[str, Mapping[str, object]] = {
    "syft": {
        "path": Path("/opt/propertyquarry-security/bin/syft"),
        "version": "1.44.0",
        "sha256": (
            "23d4e25a32026ab27351c3c044a40bcc51311c00b8bb990aa204bec4b0bb19cd"
        ),
        "config_path": Path(
            "/etc/propertyquarry/security-scanners/syft.v1.yaml"
        ),
        "config_sha256": (
            "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
        ),
    },
    "trivy": {
        "path": Path("/opt/propertyquarry-security/bin/trivy"),
        "version": "0.72.0",
        "sha256": (
            "0e69edd134a3c338baa1a6806920773615d682b18cbc6a0cba2a3b658ef9b63e"
        ),
        "config_path": Path(
            "/etc/propertyquarry/security-scanners/trivy.v1.yaml"
        ),
        "config_sha256": (
            "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
        ),
    },
}
TRIVY_DATABASES: Mapping[str, Mapping[str, object]] = {
    "vulnerability": {
        "database_path": TRIVY_CACHE_DIR / "db" / "trivy.db",
        "metadata_path": TRIVY_CACHE_DIR / "db" / "metadata.json",
        "schema_version": 2,
    },
    "java": {
        "database_path": TRIVY_CACHE_DIR / "java-db" / "trivy-java.db",
        "metadata_path": TRIVY_CACHE_DIR / "java-db" / "metadata.json",
        "schema_version": 1,
    },
}


class SecurityGateError(RuntimeError):
    """Base release-security gate failure."""


class SecurityValidationError(SecurityGateError):
    """Invalid immutable identity, threshold, waiver, or scanner document."""


class ScannerExecutionError(SecurityGateError):
    """A required fixed scanner command failed."""


class _StrictJSONError(ValueError):
    """Internal signal for JSON extensions forbidden by the security gate."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class StableFileIdentity:
    path: str
    bytes: int
    sha256: str
    mode: int
    uid: int
    gid: int
    nlink: int
    payload: bytes | None = None

    def receipt_value(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "mode": f"{self.mode:04o}",
            "uid": self.uid,
            "gid": self.gid,
            "nlink": self.nlink,
        }


_DirectoryHandle = tuple[
    int,
    int | None,
    str | None,
    tuple[int, ...],
]


def _metadata_identity(value: os.stat_result) -> tuple[int, ...]:
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
    """Bind a directory object and its authority-relevant metadata.

    Directory size, link count, and timestamps can change when an unrelated
    sibling is created in a shared ancestor. They are not identity fields and
    made otherwise stable file reads fail nondeterministically.
    """

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _validate_trusted_directory(
    value: os.stat_result,
    *,
    owners: frozenset[int],
    allowed_write_mask: int,
    final: bool,
    label: str,
) -> None:
    forbidden_write_mask = (
        stat.S_IWGRP | stat.S_IWOTH
    ) & ~allowed_write_mask
    peer_writable = bool(value.st_mode & forbidden_write_mask)
    sticky = bool(value.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid not in owners
        or (peer_writable and (final or not sticky))
    ):
        raise SecurityValidationError(
            f"{label} directory chain is not trusted-owner and immutable to peers"
        )


def stable_file_identity(
    path: Path,
    *,
    owners: frozenset[int],
    maximum_bytes: int,
    expected_sha256: str | None = None,
    expected_mode: int | None = None,
    allowed_write_mask: int = 0,
    capture_payload: bool = False,
    label: str,
) -> StableFileIdentity:
    """Authenticate one regular file through retained, no-follow descriptors."""

    if maximum_bytes <= 0:
        raise SecurityValidationError(f"{label} maximum byte count is invalid")
    candidate = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    if (
        not path.is_absolute()
        or candidate != path
        or candidate == Path("/")
        or candidate.name in {"", ".", ".."}
    ):
        raise SecurityValidationError(f"{label} path is not canonical and absolute")
    if (
        expected_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise SecurityValidationError(f"{label} expected digest is invalid")
    if expected_mode is not None and not 0 <= expected_mode <= 0o7777:
        raise SecurityValidationError(f"{label} expected mode is invalid")
    if allowed_write_mask & ~(stat.S_IWGRP | stat.S_IWOTH):
        raise SecurityValidationError(f"{label} allowed write mask is invalid")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_handles: list[_DirectoryHandle] = []
    descriptor = -1
    try:
        root_descriptor = os.open("/", directory_flags)
        root_metadata = os.fstat(root_descriptor)
        root_identity = _directory_identity(root_metadata)
        directory_handles.append(
            (root_descriptor, None, None, root_identity)
        )
        _validate_trusted_directory(
            root_metadata,
            owners=owners,
            allowed_write_mask=allowed_write_mask,
            final=candidate.parent == Path("/"),
            label=label,
        )

        for index, component in enumerate(candidate.parent.parts[1:]):
            parent_descriptor = directory_handles[-1][0]
            child_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            child_metadata = os.fstat(child_descriptor)
            child_identity = _directory_identity(child_metadata)
            directory_handles.append(
                (
                    child_descriptor,
                    parent_descriptor,
                    component,
                    child_identity,
                )
            )
            child_named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _validate_trusted_directory(
                child_metadata,
                owners=owners,
                allowed_write_mask=allowed_write_mask,
                final=index == len(candidate.parent.parts[1:]) - 1,
                label=label,
            )
            if _directory_identity(child_named) != child_identity:
                raise SecurityValidationError(
                    f"{label} directory changed while it was opened"
                )

        parent_descriptor = directory_handles[-1][0]
        descriptor = os.open(
            candidate.name,
            file_flags,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        opened_identity = _metadata_identity(opened)
        named = os.stat(
            candidate.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        mode = stat.S_IMODE(opened.st_mode)
        forbidden_file_mode = (
            (stat.S_IWGRP | stat.S_IWOTH) & ~allowed_write_mask
        ) | stat.S_ISUID | stat.S_ISGID
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid not in owners
            or opened.st_nlink != 1
            or opened.st_size <= 0
            or opened.st_size > maximum_bytes
            or opened.st_mode & forbidden_file_mode
            or (expected_mode is not None and mode != expected_mode)
            or _metadata_identity(named) != opened_identity
        ):
            raise SecurityValidationError(
                f"{label} is not the expected trusted regular file"
            )

        digest = hashlib.sha256()
        payload_chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SecurityValidationError(f"{label} exceeds its size bound")
            digest.update(chunk)
            if capture_payload:
                payload_chunks.append(chunk)

        observed_sha256 = digest.hexdigest()
        after_opened = os.fstat(descriptor)
        after_named = os.stat(
            candidate.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            total != opened.st_size
            or _metadata_identity(after_opened) != opened_identity
            or _metadata_identity(after_named) != opened_identity
        ):
            raise SecurityValidationError(f"{label} changed while it was read")
        if (
            expected_sha256 is not None
            and observed_sha256 != expected_sha256
        ):
            raise SecurityValidationError(f"{label} digest differs from its pin")

        for (
            directory_descriptor,
            ancestor_descriptor,
            component,
            identity,
        ) in directory_handles:
            opened_directory = os.fstat(directory_descriptor)
            if ancestor_descriptor is None:
                named_directory = os.lstat("/")
            else:
                named_directory = os.stat(
                    str(component),
                    dir_fd=ancestor_descriptor,
                    follow_symlinks=False,
                )
            if (
                _directory_identity(opened_directory) != identity
                or _directory_identity(named_directory) != identity
            ):
                raise SecurityValidationError(
                    f"{label} directory chain changed while it was read"
                )

        return StableFileIdentity(
            path=str(candidate),
            bytes=total,
            sha256=observed_sha256,
            mode=mode,
            uid=opened.st_uid,
            gid=opened.st_gid,
            nlink=opened.st_nlink,
            payload=b"".join(payload_chunks) if capture_payload else None,
        )
    except SecurityValidationError:
        raise
    except (OSError, ValueError) as exc:
        raise SecurityValidationError(
            f"{label} cannot be authenticated without following links"
        ) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for directory_descriptor, _, _, _ in reversed(directory_handles):
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


class ScannerRunner(Protocol):
    def available(self, executable: str) -> bool: ...

    def identity(
        self,
        executable: str,
        *,
        now: datetime,
    ) -> Mapping[str, object]: ...

    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> CommandResult: ...


class SubprocessScannerRunner:
    def __init__(
        self,
        *,
        system_owners: frozenset[int] = frozenset({0}),
        release_owners: frozenset[int] | None = None,
    ) -> None:
        self._system_owners = system_owners
        self._release_owners = (
            frozenset({0, os.geteuid()})
            if release_owners is None
            else release_owners
        )
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = (
            None
        )
        self._pip_audit_cache: Path | None = None
        self._trivy_runtime_cache: Path | None = None

    def _private_cache_root(self) -> Path:
        if self._temporary_directory is not None:
            return Path(self._temporary_directory.name)
        try:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="propertyquarry-security."
            )
            root = Path(temporary_directory.name)
            metadata = root.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                temporary_directory.cleanup()
                raise OSError
        except OSError as exc:
            raise ScannerExecutionError(
                "private scanner cache cannot be created"
            ) from exc
        self._temporary_directory = temporary_directory
        return root

    def _pip_audit_cache_dir(self) -> Path:
        if self._pip_audit_cache is not None:
            return self._pip_audit_cache
        try:
            cache = self._private_cache_root() / "pip-audit-cache"
            cache.mkdir(mode=0o700)
            cache.chmod(0o700)
        except OSError as exc:
            raise ScannerExecutionError(
                "private pip-audit cache cannot be created"
            ) from exc
        self._pip_audit_cache = cache
        return cache

    def _audit_trivy_runtime_cache(self, cache: Path) -> None:
        expected_links = {
            "db": TRIVY_CACHE_DIR / "db",
            "java-db": TRIVY_CACHE_DIR / "java-db",
        }
        try:
            root = cache.lstat()
            names = {path.name for path in cache.iterdir()}
            if (
                stat.S_ISLNK(root.st_mode)
                or not stat.S_ISDIR(root.st_mode)
                or stat.S_IMODE(root.st_mode) != 0o700
                or root.st_uid != os.geteuid()
                or not set(expected_links).issubset(names)
                or names.difference({*expected_links, "fanal"})
            ):
                raise ScannerExecutionError(
                    "private Trivy runtime cache was mutated"
                )
            for name, target in expected_links.items():
                link = cache / name
                metadata = link.lstat()
                if (
                    not stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or os.readlink(link) != str(target)
                ):
                    raise ScannerExecutionError(
                        "private Trivy runtime cache was mutated"
                    )
            fanal = cache / "fanal"
            if "fanal" not in names:
                return
            fanal_metadata = fanal.lstat()
            if (
                stat.S_ISLNK(fanal_metadata.st_mode)
                or not stat.S_ISDIR(fanal_metadata.st_mode)
                or stat.S_IMODE(fanal_metadata.st_mode) != 0o700
                or fanal_metadata.st_uid != os.geteuid()
            ):
                raise ScannerExecutionError(
                    "private Trivy runtime cache was mutated"
                )
            aggregate_size = 0
            members = list(fanal.iterdir())
            if len(members) > MAX_TRIVY_RUNTIME_CACHE_MEMBERS:
                raise ScannerExecutionError(
                    "private Trivy runtime cache was mutated"
                )
            for member in members:
                metadata = member.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o644}
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or metadata.st_size < 0
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in member.name
                    )
                ):
                    raise ScannerExecutionError(
                        "private Trivy runtime cache was mutated"
                    )
                aggregate_size += metadata.st_size
                if aggregate_size > MAX_TRIVY_RUNTIME_CACHE_BYTES:
                    raise ScannerExecutionError(
                        "private Trivy runtime cache was mutated"
                    )
        except ScannerExecutionError:
            raise
        except OSError as exc:
            raise ScannerExecutionError(
                "private Trivy runtime cache cannot be authenticated"
            ) from exc

    def _trivy_cache_dir(self) -> Path:
        if self._trivy_runtime_cache is not None:
            self._audit_trivy_runtime_cache(self._trivy_runtime_cache)
            return self._trivy_runtime_cache
        try:
            cache = self._private_cache_root() / "trivy-cache"
            cache.mkdir(mode=0o700)
            cache.chmod(0o700)
            for name in ("db", "java-db"):
                source = TRIVY_CACHE_DIR / name
                if source.resolve(strict=True) != source:
                    raise OSError
                metadata = source.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in self._system_owners
                    or metadata.st_mode & 0o022
                ):
                    raise OSError
                os.symlink(source, cache / name, target_is_directory=True)
        except OSError as exc:
            raise ScannerExecutionError(
                "private Trivy runtime cache cannot be created"
            ) from exc
        self._trivy_runtime_cache = cache
        self._audit_trivy_runtime_cache(cache)
        return cache

    def close(self) -> None:
        temporary_directory = self._temporary_directory
        self._temporary_directory = None
        self._pip_audit_cache = None
        self._trivy_runtime_cache = None
        if temporary_directory is not None:
            try:
                temporary_directory.cleanup()
            except OSError as exc:
                raise ScannerExecutionError(
                    "private scanner cache cannot be removed"
                ) from exc

    def __enter__(self) -> SubprocessScannerRunner:
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        self.close()

    def available(self, executable: str) -> bool:
        try:
            if executable == "pip-audit":
                return (
                    Path(sys.executable) == RELEASE_PYTHON
                    and package_version("pip-audit")
                    == PINNED_PIP_AUDIT_VERSION
                )
            if executable in PINNED_SCANNERS:
                return Path(PINNED_SCANNERS[executable]["path"]).is_file()
            return False
        except (OSError, PackageNotFoundError):
            return False

    def _runtime_manifest(
        self,
    ) -> tuple[dict[str, object], StableFileIdentity]:
        manifest_identity = stable_file_identity(
            SECURITY_RUNTIME_MANIFEST,
            owners=self._system_owners,
            maximum_bytes=MAX_SECURITY_RUNTIME_MANIFEST_BYTES,
            expected_mode=0o644,
            capture_payload=True,
            label="security runtime manifest",
        )
        assert manifest_identity.payload is not None
        try:
            raw_manifest = manifest_identity.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityValidationError(
                "security runtime manifest is not UTF-8"
            ) from exc
        payload = parse_json_document(
            raw_manifest,
            document_name="security runtime manifest",
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "tools", "trivy_databases"}
            or payload.get("schema") != SECURITY_RUNTIME_SCHEMA
        ):
            raise SecurityValidationError(
                "security runtime manifest differs from the v1 contract"
            )

        tools = payload.get("tools")
        if not isinstance(tools, dict) or set(tools) != set(PINNED_SCANNERS):
            raise SecurityValidationError(
                "security runtime manifest scanner set is invalid"
            )
        for tool, expected in PINNED_SCANNERS.items():
            record = tools.get(tool)
            if (
                not isinstance(record, dict)
                or set(record)
                != {
                    "path",
                    "version",
                    "sha256",
                    "config_path",
                    "config_sha256",
                }
                or record.get("path") != str(expected["path"])
                or record.get("version") != expected["version"]
                or record.get("sha256") != expected["sha256"]
                or record.get("config_path")
                != str(expected["config_path"])
                or record.get("config_sha256")
                != expected["config_sha256"]
            ):
                raise SecurityValidationError(
                    f"security runtime manifest {tool} pin is invalid"
                )

        databases = payload.get("trivy_databases")
        if (
            not isinstance(databases, dict)
            or set(databases) != {"cache_dir", *TRIVY_DATABASES}
            or databases.get("cache_dir") != str(TRIVY_CACHE_DIR)
        ):
            raise SecurityValidationError(
                "security runtime manifest Trivy database set is invalid"
            )
        database_keys = {
            "database_path",
            "database_sha256",
            "metadata_path",
            "metadata_sha256",
            "schema_version",
            "updated_at",
            "downloaded_at",
            "next_update",
        }
        for database_name, expected in TRIVY_DATABASES.items():
            record = databases.get(database_name)
            if (
                not isinstance(record, dict)
                or set(record) != database_keys
                or record.get("database_path")
                != str(expected["database_path"])
                or record.get("metadata_path")
                != str(expected["metadata_path"])
                or record.get("schema_version")
                != expected["schema_version"]
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(record.get("database_sha256") or ""),
                )
                is None
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(record.get("metadata_sha256") or ""),
                )
                is None
                or any(
                    not isinstance(record.get(field), str)
                    or not str(record[field]).strip()
                    for field in (
                        "updated_at",
                        "downloaded_at",
                        "next_update",
                    )
                )
            ):
                raise SecurityValidationError(
                    "security runtime manifest "
                    f"{database_name} database pin is invalid"
                )
        return payload, manifest_identity

    def _binary_identity(
        self,
        executable: str,
    ) -> dict[str, object]:
        expected = PINNED_SCANNERS[executable]
        identity = stable_file_identity(
            Path(expected["path"]),
            owners=self._system_owners,
            maximum_bytes=MAX_SCANNER_BINARY_BYTES,
            expected_sha256=str(expected["sha256"]),
            expected_mode=0o755,
            label=f"{executable} scanner binary",
        )
        configuration = stable_file_identity(
            Path(expected["config_path"]),
            owners=self._system_owners,
            maximum_bytes=MAX_SCANNER_CONFIG_BYTES,
            expected_sha256=str(expected["config_sha256"]),
            expected_mode=0o644,
            label=f"{executable} scanner configuration",
        )
        return {
            "kind": "root_owned_sha256_pinned_binary",
            "version": expected["version"],
            "execution": [identity.path],
            "binary": identity.receipt_value(),
            "configuration": configuration.receipt_value(),
        }

    def _pip_audit_identity(self) -> dict[str, object]:
        if Path(sys.executable) != RELEASE_PYTHON:
            raise SecurityValidationError(
                "pip-audit must run through the authenticated release interpreter"
            )
        try:
            observed_version = package_version("pip-audit")
        except PackageNotFoundError as exc:
            raise SecurityValidationError(
                "pip-audit is absent from the authenticated release environment"
            ) from exc
        if observed_version != PINNED_PIP_AUDIT_VERSION:
            raise SecurityValidationError(
                "pip-audit version differs from the authenticated release pin"
            )
        interpreter_identity = stable_file_identity(
            RELEASE_PYTHON,
            owners=self._release_owners,
            maximum_bytes=64 * 1024 * 1024,
            expected_sha256=PINNED_RELEASE_PYTHON_SHA256,
            expected_mode=0o755,
            label="authenticated release interpreter",
        )
        launcher_identity = stable_file_identity(
            RELEASE_PYTHON_LAUNCHER,
            owners=self._release_owners,
            maximum_bytes=1024 * 1024,
            expected_mode=0o755,
            allowed_write_mask=stat.S_IWGRP,
            label="authenticated release interpreter launcher",
        )
        execution = [
            launcher_identity.path,
            "-I",
            "-B",
            "-m",
            "pip_audit",
        ]
        return {
            "kind": "authenticated_release_python_module",
            "version": observed_version,
            "execution": execution,
            "interpreter": interpreter_identity.receipt_value(),
            "launcher": launcher_identity.receipt_value(),
        }

    def _trivy_database_identities(
        self,
        *,
        now: datetime,
        manifest: Mapping[str, object],
    ) -> dict[str, object]:
        databases = manifest["trivy_databases"]
        assert isinstance(databases, dict)
        result: dict[str, object] = {}
        for database_name, expected in TRIVY_DATABASES.items():
            record = databases[database_name]
            assert isinstance(record, dict)
            metadata_identity = stable_file_identity(
                Path(expected["metadata_path"]),
                owners=self._system_owners,
                maximum_bytes=1024 * 1024,
                expected_sha256=str(record["metadata_sha256"]),
                expected_mode=0o644,
                capture_payload=True,
                label=f"Trivy {database_name} database metadata",
            )
            assert metadata_identity.payload is not None
            try:
                metadata_text = metadata_identity.payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecurityValidationError(
                    f"Trivy {database_name} database metadata is not UTF-8"
                ) from exc
            metadata = parse_json_document(
                metadata_text,
                document_name=f"Trivy {database_name} database metadata",
            )
            expected_metadata = {
                "Version": record["schema_version"],
                "UpdatedAt": record["updated_at"],
                "DownloadedAt": record["downloaded_at"],
                "NextUpdate": record["next_update"],
            }
            if (
                not isinstance(metadata, dict)
                or set(metadata) != set(expected_metadata)
                or metadata != expected_metadata
            ):
                raise SecurityValidationError(
                    f"Trivy {database_name} database metadata differs from "
                    "the root-owned manifest"
                )

            updated_at = parse_timestamp(
                metadata["UpdatedAt"],
                field_name=(
                    f"Trivy {database_name} database metadata UpdatedAt"
                ),
            )
            downloaded_at = parse_timestamp(
                metadata["DownloadedAt"],
                field_name=(
                    f"Trivy {database_name} database metadata DownloadedAt"
                ),
            )
            next_update = parse_timestamp(
                metadata["NextUpdate"],
                field_name=(
                    f"Trivy {database_name} database metadata NextUpdate"
                ),
            )
            if (
                updated_at > downloaded_at + DATABASE_CLOCK_SKEW
                or downloaded_at > now + DATABASE_CLOCK_SKEW
                or next_update <= updated_at
                or now >= next_update
            ):
                raise SecurityValidationError(
                    f"Trivy {database_name} database is stale or has "
                    "inconsistent timestamps"
                )

            database_identity = stable_file_identity(
                Path(expected["database_path"]),
                owners=self._system_owners,
                maximum_bytes=MAX_TRIVY_DATABASE_BYTES,
                expected_sha256=str(record["database_sha256"]),
                expected_mode=0o644,
                label=f"Trivy {database_name} database",
            )
            result[database_name] = {
                "schema_version": record["schema_version"],
                "updated_at": isoformat(updated_at),
                "downloaded_at": isoformat(downloaded_at),
                "next_update": isoformat(next_update),
                "metadata": metadata_identity.receipt_value(),
                "database": database_identity.receipt_value(),
            }
        return result

    def identity(
        self,
        executable: str,
        *,
        now: datetime,
    ) -> Mapping[str, object]:
        if executable == "pip-audit":
            return self._pip_audit_identity()
        if executable not in PINNED_SCANNERS:
            raise SecurityValidationError(
                f"unsupported scanner identity requested: {executable}"
            )
        manifest, manifest_identity = self._runtime_manifest()
        identity = self._binary_identity(executable)
        identity["runtime_manifest"] = manifest_identity.receipt_value()
        if executable == "trivy":
            identity["cache_dir"] = str(TRIVY_CACHE_DIR)
            identity["databases"] = self._trivy_database_identities(
                now=now,
                manifest=manifest,
            )
        return identity

    def _translated_command(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            raise ScannerExecutionError("scanner command is empty")
        executable = argv[0]
        if executable == "pip-audit":
            identity = self._pip_audit_identity()
            return [
                *identity["execution"],  # type: ignore[misc]
                "--cache-dir",
                str(self._pip_audit_cache_dir()),
                *argv[1:],
            ]
        if executable == "syft":
            identity = self._binary_identity(executable)
            return [
                str(identity["execution"][0]),  # type: ignore[index]
                "--config",
                str(identity["configuration"]["path"]),  # type: ignore[index]
                *argv[1:],
            ]
        if executable == "trivy":
            identity = self._binary_identity(executable)
            return [
                str(identity["execution"][0]),  # type: ignore[index]
                "--cache-dir",
                str(self._trivy_cache_dir()),
                "--config",
                str(identity["configuration"]["path"]),  # type: ignore[index]
                *argv[1:],
            ]
        raise ScannerExecutionError(
            f"unsupported scanner command: {executable}"
        )

    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        command = self._translated_command(argv)
        trivy_runtime_cache = (
            self._trivy_runtime_cache
            if argv and argv[0] == "trivy"
            else None
        )
        command_env = {
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
        if argv and argv[0] == "syft":
            command_env["SYFT_CHECK_FOR_APP_UPDATE"] = "false"
        if argv and argv[0] == "trivy":
            command_env["TRIVY_SKIP_VERSION_CHECK"] = "true"
        try:
            completed = subprocess.run(
                command,
                cwd=APP_ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=command_env,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScannerExecutionError(
                f"scanner command timed out after {timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise ScannerExecutionError("could not start a required scanner") from exc
        finally:
            if trivy_runtime_cache is not None:
                self._audit_trivy_runtime_cache(trivy_runtime_cache)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class GateConfig:
    release_commit_sha: str
    web_image: str
    render_image: str
    severity_threshold: str
    flagship: bool
    waivers_path: Path
    artifacts_dir: Path
    receipt_path: Path
    timeout_seconds: int
    overwrite_receipt: bool = False


@dataclass(frozen=True)
class Finding:
    source: str
    target: str
    vulnerability_id: str
    package: str
    installed_version: str
    fixed_version: str
    severity: str
    effective_severity: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.source, self.target, self.vulnerability_id, self.package)

    def receipt_value(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "vulnerability_id": self.vulnerability_id,
            "package": self.package,
            "installed_version": self.installed_version,
            "fixed_version": self.fixed_version,
            "severity": self.severity,
            "effective_severity": self.effective_severity,
        }


@dataclass(frozen=True)
class Waiver:
    waiver_id: str
    source: str
    target: str
    vulnerability_id: str
    package: str
    severity: str
    release_commit_sha: str
    owner: str
    approved_by: str
    reason: str
    created_at: datetime
    expires_at: datetime

    @property
    def finding_key(self) -> tuple[str, str, str, str]:
        return (self.source, self.target, self.vulnerability_id, self.package)

    def receipt_value(self) -> dict[str, str]:
        return {
            "id": self.waiver_id,
            "source": self.source,
            "target": self.target,
            "vulnerability_id": self.vulnerability_id,
            "package": self.package,
            "severity": self.severity,
            "owner": self.owner,
            "approved_by": self.approved_by,
            "created_at": isoformat(self.created_at),
            "expires_at": isoformat(self.expires_at),
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(raw: object, *, field_name: str) -> datetime:
    value = str(raw or "").strip()
    if not value:
        raise SecurityValidationError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecurityValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecurityValidationError(f"{field_name} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def positive_int(raw: object, *, field_name: str, default: int) -> int:
    value = str(raw or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) <= 0:
        raise SecurityValidationError(f"{field_name} must be a positive integer")
    return int(value)


def normalize_release_sha(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(value):
        raise SecurityValidationError("release commit must be a full 40-character Git SHA")
    return value


def normalize_image_ref(raw: str, *, field_name: str) -> str:
    value = str(raw or "").strip()
    if not IMAGE_REF_RE.fullmatch(value):
        raise SecurityValidationError(
            f"{field_name} must be an immutable image reference ending in @sha256:<64 hex>"
        )
    prefix, digest = value.rsplit("@", 1)
    return f"{prefix}@{digest.lower()}"


def normalize_threshold(raw: str) -> str:
    value = str(raw or "").strip().upper()
    if value not in SEVERITY_RANK:
        raise SecurityValidationError("severity threshold must be LOW, MEDIUM, HIGH, or CRITICAL")
    return value


def validate_config(config: GateConfig) -> GateConfig:
    release = normalize_release_sha(config.release_commit_sha)
    web_image = normalize_image_ref(config.web_image, field_name="web image")
    render_image = normalize_image_ref(config.render_image, field_name="render image")
    if web_image == render_image:
        raise SecurityValidationError("web and render images must have distinct immutable identities")
    threshold = normalize_threshold(config.severity_threshold)
    timeout = positive_int(config.timeout_seconds, field_name="scanner timeout", default=900)
    return GateConfig(
        release_commit_sha=release,
        web_image=web_image,
        render_image=render_image,
        severity_threshold=threshold,
        flagship=bool(config.flagship),
        waivers_path=config.waivers_path,
        artifacts_dir=config.artifacts_dir,
        receipt_path=config.receipt_path,
        timeout_seconds=timeout,
        overwrite_receipt=config.overwrite_receipt,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"path": str(path), "bytes": len(payload), "sha256": sha256_bytes(payload)}


def payload_identity(path: Path, payload: bytes) -> dict[str, object]:
    return {
        "path": str(path),
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def output_evidence(value: str) -> dict[str, object]:
    payload = str(value or "").encode("utf-8", errors="replace")
    return {"bytes": len(payload), "sha256": sha256_bytes(payload)}


def json_document_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SecurityValidationError(
            "security output is not finite JSON"
        ) from exc


def atomic_write_json(path: Path, payload: object, *, overwrite: bool = True) -> None:
    encoded = json_document_bytes(payload)
    try:
        atomic_write_bytes(path, encoded, overwrite=overwrite)
    except OutputExistsError as exc:
        raise SecurityValidationError(
            f"receipt already exists: {path}; choose a new path or use --overwrite-receipt"
        ) from exc
    except SecureFileIOError as exc:
        raise SecurityValidationError(
            f"security output cannot be published safely: {path}"
        ) from exc


def parse_json_document(raw: str, *, document_name: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                raise _StrictJSONError(f"contains duplicate object key {key!r}")
            parsed[key] = value
        return parsed

    def reject_non_finite_constant(value: str) -> object:
        raise _StrictJSONError(f"contains non-finite numeric constant {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite_constant,
        )
    except json.JSONDecodeError as exc:
        raise SecurityValidationError(f"{document_name} is not valid JSON") from exc
    except _StrictJSONError as exc:
        raise SecurityValidationError(f"{document_name} {exc}") from exc


def normalize_package_name(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    return PACKAGE_NORMALIZE_RE.sub("-", value).lower()


def parse_requirements_lock_bytes(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecurityValidationError("requirements lock is not UTF-8") from exc
    requirements: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        selected = raw_line.partition("#")[0].strip()
        if not selected:
            continue
        match = LOCK_REQUIREMENT_RE.fullmatch(selected)
        if not match:
            raise SecurityValidationError(
                f"requirements lock line {line_number} must be an exact name==version pin"
            )
        package = normalize_package_name(match.group(1))
        version = match.group(2)
        if package in requirements:
            raise SecurityValidationError(
                f"requirements lock contains duplicate normalized package {package!r}"
            )
        requirements[package] = version
    if not requirements:
        raise SecurityValidationError("requirements lock must contain at least one exact pin")
    return requirements


def parse_requirements_lock(path: Path = LOCK_PATH) -> dict[str, str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SecurityValidationError("requirements lock cannot be read") from exc
    return parse_requirements_lock_bytes(payload)


def load_waivers(
    path: Path,
    *,
    release_commit_sha: str,
    source_targets: Mapping[str, str],
    now: datetime,
) -> list[Waiver]:
    if not path.is_file():
        raise SecurityValidationError(f"waiver file is missing: {path}")
    payload = parse_json_document(path.read_text(encoding="utf-8"), document_name="waiver file")
    if not isinstance(payload, dict) or payload.get("schema") != WAIVER_SCHEMA:
        raise SecurityValidationError(f"waiver file schema must be {WAIVER_SCHEMA}")
    raw_waivers = payload.get("waivers")
    if not isinstance(raw_waivers, list):
        raise SecurityValidationError("waiver file waivers must be a list")

    waivers: list[Waiver] = []
    seen_ids: set[str] = set()
    seen_findings: set[tuple[str, str, str, str]] = set()
    for index, raw_waiver in enumerate(raw_waivers):
        field = f"waivers[{index}]"
        if not isinstance(raw_waiver, dict):
            raise SecurityValidationError(f"{field} must be an object")
        waiver_id = str(raw_waiver.get("id") or "").strip()
        if not WAIVER_ID_RE.fullmatch(waiver_id):
            raise SecurityValidationError(f"{field}.id must match PQSEC-YYYY-NNN")
        if waiver_id in seen_ids:
            raise SecurityValidationError(f"duplicate waiver id: {waiver_id}")
        seen_ids.add(waiver_id)

        source = str(raw_waiver.get("source") or "").strip()
        if source not in source_targets:
            raise SecurityValidationError(f"{field}.source is not an allowed scanner target")
        target = str(raw_waiver.get("target") or "").strip()
        if target != source_targets[source]:
            raise SecurityValidationError(
                f"{field}.target does not match the immutable target for {source}"
            )
        vulnerability_id = str(raw_waiver.get("vulnerability_id") or "").strip()
        package = str(raw_waiver.get("package") or "").strip()
        severity = str(raw_waiver.get("severity") or "").strip().upper()
        bound_release = normalize_release_sha(str(raw_waiver.get("release_commit_sha") or ""))
        owner = str(raw_waiver.get("owner") or "").strip()
        approved_by = str(raw_waiver.get("approved_by") or "").strip()
        reason = str(raw_waiver.get("reason") or "").strip()
        if not vulnerability_id or not package:
            raise SecurityValidationError(f"{field} requires vulnerability_id and package")
        if severity not in {*SEVERITY_RANK, "UNKNOWN"}:
            raise SecurityValidationError(f"{field}.severity is invalid")
        if bound_release != release_commit_sha:
            raise SecurityValidationError(f"{field} is not bound to this release commit")
        if not owner or not approved_by or len(reason) < 12:
            raise SecurityValidationError(
                f"{field} requires owner, approved_by, and a reason of at least 12 characters"
            )
        if owner == approved_by:
            raise SecurityValidationError(
                f"{field}.approved_by must be independent from the waiver owner"
            )
        created_at = parse_timestamp(raw_waiver.get("created_at"), field_name=f"{field}.created_at")
        expires_at = parse_timestamp(raw_waiver.get("expires_at"), field_name=f"{field}.expires_at")
        if created_at > now:
            raise SecurityValidationError(f"{field}.created_at cannot be in the future")
        if expires_at <= now:
            raise SecurityValidationError(f"{field} is expired")
        if expires_at <= created_at or expires_at - created_at > MAX_WAIVER_LIFETIME:
            raise SecurityValidationError(f"{field} must expire within 30 days of creation")

        waiver = Waiver(
            waiver_id=waiver_id,
            source=source,
            target=target,
            vulnerability_id=vulnerability_id,
            package=package,
            severity=severity,
            release_commit_sha=bound_release,
            owner=owner,
            approved_by=approved_by,
            reason=reason,
            created_at=created_at,
            expires_at=expires_at,
        )
        if waiver.finding_key in seen_findings:
            raise SecurityValidationError(f"multiple waivers target the same exact finding: {field}")
        seen_findings.add(waiver.finding_key)
        waivers.append(waiver)
    return waivers


def parse_pip_audit(
    payload: object,
    *,
    target: str,
    expected_requirements: Mapping[str, str],
) -> list[Finding]:
    if isinstance(payload, list):
        dependencies = payload
    elif isinstance(payload, dict) and isinstance(payload.get("dependencies"), list):
        dependencies = payload["dependencies"]
    else:
        raise SecurityValidationError(
            "pip-audit output must be a dependency list or contain a dependencies list"
        )

    observed: dict[str, tuple[str, dict[str, object]]] = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("vulns"), list):
            raise SecurityValidationError("pip-audit dependency entries must contain a vulns list")
        package = normalize_package_name(dependency.get("name"))
        installed = str(dependency.get("version") or "").strip()
        if not package or not installed:
            raise SecurityValidationError("pip-audit dependency entries require name and version")
        if package in observed:
            raise SecurityValidationError(
                f"pip-audit output contains duplicate normalized package {package!r}"
            )
        observed[package] = (installed, dependency)

    expected = dict(expected_requirements)
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    mismatched = sorted(
        package
        for package in set(expected) & set(observed)
        if observed[package][0] != expected[package]
    )
    if missing or unexpected or mismatched:
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if mismatched:
            details.append(
                "version mismatch: "
                + ", ".join(
                    f"{package}=={observed[package][0]} (expected {expected[package]})"
                    for package in mismatched
                )
            )
        raise SecurityValidationError(
            "pip-audit output does not exactly cover the selected requirements lock "
            f"({'; '.join(details)})"
        )

    findings: list[Finding] = []
    for package, (installed, dependency) in observed.items():
        for vulnerability in dependency["vulns"]:
            if not isinstance(vulnerability, dict):
                raise SecurityValidationError("pip-audit vulnerability entries must be objects")
            vulnerability_id = str(vulnerability.get("id") or "").strip()
            if not vulnerability_id:
                raise SecurityValidationError("pip-audit vulnerability entries require id")
            fixes = vulnerability.get("fix_versions") or []
            if not isinstance(fixes, list):
                raise SecurityValidationError("pip-audit fix_versions must be a list")
            findings.append(
                Finding(
                    source="pip-audit",
                    target=target,
                    vulnerability_id=vulnerability_id,
                    package=package,
                    installed_version=installed,
                    fixed_version=", ".join(str(item) for item in fixes),
                    severity="UNKNOWN",
                    effective_severity="CRITICAL",
                )
            )
    return findings


def validate_cyclonedx_sbom(payload: object, *, target_name: str, target: str) -> int:
    if not isinstance(payload, dict) or payload.get("bomFormat") != "CycloneDX":
        raise SecurityValidationError(f"{target_name} SBOM must be a CycloneDX JSON document")
    if not str(payload.get("specVersion") or "").strip():
        raise SecurityValidationError(f"{target_name} SBOM is missing specVersion")
    metadata = payload.get("metadata")
    root_component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(root_component, dict):
        raise SecurityValidationError(
            f"{target_name} SBOM must contain a metadata.component image identity"
        )
    if root_component.get("type") != "container":
        raise SecurityValidationError(
            f"{target_name} SBOM metadata.component must identify a container"
        )
    if not str(root_component.get("name") or "").strip():
        raise SecurityValidationError(
            f"{target_name} SBOM metadata.component.name is required"
        )
    expected_digest = target.rsplit("@", 1)[1].lower()
    observed_digest = str(root_component.get("version") or "").strip().lower()
    if observed_digest != expected_digest:
        raise SecurityValidationError(
            f"{target_name} SBOM does not prove the expected immutable image target"
        )
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise SecurityValidationError(f"{target_name} SBOM must contain at least one component")
    return len(components)


def parse_trivy(payload: object, *, source: str, target: str) -> list[Finding]:
    if not isinstance(payload, dict):
        raise SecurityValidationError(f"{source} output must be a JSON object")
    if payload.get("SchemaVersion") != 2:
        raise SecurityValidationError(f"{source} output must use Trivy SchemaVersion 2")
    if payload.get("ArtifactType") != "container_image":
        raise SecurityValidationError(
            f"{source} output must describe a Trivy container_image artifact"
        )
    artifact_name = str(payload.get("ArtifactName") or "").strip()
    metadata = payload.get("Metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise SecurityValidationError(f"{source} Metadata must be an object")
    repo_digests = metadata.get("RepoDigests") if isinstance(metadata, dict) else None
    if repo_digests is not None and not isinstance(repo_digests, list):
        raise SecurityValidationError(f"{source} Metadata.RepoDigests must be a list")
    reported_identities = [artifact_name]
    if isinstance(repo_digests, list):
        reported_identities.extend(
            str(identity or "").strip()
            for identity in repo_digests
            if isinstance(identity, str)
        )
    normalized_identities: set[str] = set()
    for identity in reported_identities:
        if IMAGE_REF_RE.fullmatch(identity):
            prefix, digest = identity.rsplit("@", 1)
            normalized_identities.add(f"{prefix}@{digest.lower()}")
    if target not in normalized_identities:
        raise SecurityValidationError(
            f"{source} output does not prove the expected immutable image target"
        )
    results = payload.get("Results")
    if not isinstance(results, list) or not results:
        raise SecurityValidationError(f"{source} output must contain a non-empty Results list")
    findings: list[Finding] = []
    for result in results:
        if not isinstance(result, dict):
            raise SecurityValidationError(f"{source} result entries must be objects")
        if not str(result.get("Target") or "").strip():
            raise SecurityValidationError(f"{source} result entries require Target")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise SecurityValidationError(f"{source} Vulnerabilities must be a list")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise SecurityValidationError(f"{source} vulnerability entries must be objects")
            vulnerability_id = str(vulnerability.get("VulnerabilityID") or "").strip()
            package = str(vulnerability.get("PkgName") or "").strip()
            raw_severity = str(vulnerability.get("Severity") or "UNKNOWN").strip().upper()
            if not vulnerability_id or not package:
                raise SecurityValidationError(
                    f"{source} vulnerability entries require VulnerabilityID and PkgName"
                )
            effective = raw_severity if raw_severity in SEVERITY_RANK else "CRITICAL"
            findings.append(
                Finding(
                    source=source,
                    target=target,
                    vulnerability_id=vulnerability_id,
                    package=package,
                    installed_version=str(vulnerability.get("InstalledVersion") or "").strip(),
                    fixed_version=str(vulnerability.get("FixedVersion") or "").strip(),
                    severity=raw_severity if raw_severity in SEVERITY_RANK else "UNKNOWN",
                    effective_severity=effective,
                )
            )
    return findings


def scanner_command(
    runner: ScannerRunner,
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> CommandResult:
    result = runner.run(tuple(argv), timeout_seconds=timeout_seconds)
    if result.returncode not in accepted_returncodes:
        raise ScannerExecutionError(
            f"{argv[0]} failed with exit code {result.returncode}; raw output was withheld"
        )
    return result


def scanner_version(tool: str, output: str) -> str:
    raw = str(output or "").strip()
    match = re.search(r"\b[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+._a-zA-Z0-9]*)?\b", raw)
    if not match:
        raise SecurityValidationError(f"{tool} version output is not recognizable")
    return match.group(0)


def expected_artifact_paths(config: GateConfig) -> tuple[Path, ...]:
    return (
        config.artifacts_dir / "dependencies.pip-audit.json",
        config.artifacts_dir / "web.sbom.cdx.json",
        config.artifacts_dir / "web.trivy.json",
        config.artifacts_dir / "render.sbom.cdx.json",
        config.artifacts_dir / "render.trivy.json",
    )


def preflight_output_bundle(config: GateConfig) -> None:
    paths = (config.receipt_path, *expected_artifact_paths(config))
    normalized = tuple(
        Path(os.path.abspath(os.path.normpath(os.fspath(path))))
        for path in paths
    )
    if len(set(normalized)) != len(normalized):
        raise SecurityValidationError(
            "security receipt and artifact paths must be distinct"
        )
    if config.overwrite_receipt:
        return
    for path in normalized:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SecurityValidationError(
                f"security output cannot be preflighted safely: {path}"
            ) from exc
        raise SecurityValidationError(
            f"security output already exists: {path}; choose a new output "
            "bundle or use --overwrite-receipt"
        )


def publish_artifact(
    path: Path,
    payload: bytes,
    *,
    overwrite: bool,
) -> None:
    try:
        atomic_write_bytes(path, payload, overwrite=overwrite)
    except OutputExistsError as exc:
        raise SecurityValidationError(
            f"security output already exists: {path}; choose a new output "
            "bundle or use --overwrite-receipt"
        ) from exc
    except SecureFileIOError as exc:
        raise SecurityValidationError(
            f"security output cannot be published safely: {path}"
        ) from exc


def runner_identities(
    runner: ScannerRunner,
    *,
    now: datetime,
) -> dict[str, dict[str, object]]:
    identities: dict[str, dict[str, object]] = {}
    for tool in REQUIRED_TOOLS:
        identity = dict(runner.identity(tool, now=now))
        if not identity:
            raise SecurityValidationError(
                f"{tool} runner returned no authenticated identity"
            )
        identities[tool] = identity
    manifest_identities = [
        identities[tool].get("runtime_manifest")
        for tool in ("syft", "trivy")
        if "runtime_manifest" in identities[tool]
    ]
    if (
        manifest_identities
        and (
            len(manifest_identities) != 2
            or manifest_identities[0] != manifest_identities[1]
        )
    ):
        raise SecurityValidationError(
            "scanner identities do not share one runtime manifest snapshot"
        )
    return identities


def blank_receipt(config: GateConfig) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "generated_at": isoformat(utc_now()),
        "mode": "flagship" if config.flagship else "advisory",
        "status": "initializing",
        "gate_passed": False,
        "severity_threshold": config.severity_threshold,
        "identities": {
            "release_commit_sha": config.release_commit_sha,
            "web_image": config.web_image,
            "render_image": config.render_image,
        },
        "network_contract": {
            "scanner_install_allowed": False,
            "registry_access_allowed": False,
            "image_source": "local_docker_digest_only",
            "trivy_database_updates_allowed": False,
            "trivy_database_freshness_required": True,
            "scanner_execution": "authenticated_absolute_paths_only",
        },
        "tools": {tool: {"available": False} for tool in REQUIRED_TOOLS},
        "artifacts": {},
        "findings": [],
        "summary": {
            "total": 0,
            "at_or_above_threshold": 0,
            "waived": 0,
            "blocking": 0,
        },
        "waivers": {"configured": 0, "applied": [], "unused": []},
    }


def run_security_gate(
    *,
    config: GateConfig,
    runner: ScannerRunner | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], int]:
    owns_runner = runner is None
    runner = runner or SubprocessScannerRunner()
    now = (now or utc_now()).astimezone(timezone.utc)
    receipt = blank_receipt(config)
    exit_code = 0
    artifact_payloads: dict[Path, bytes] = {}
    authenticated_identities: dict[str, dict[str, object]] | None = None
    lock_identity: StableFileIdentity | None = None
    try:
        preflight_output_bundle(config)
    except Exception:
        if owns_runner:
            close_runner = getattr(runner, "close", None)
            if callable(close_runner):
                close_runner()
        raise
    try:
        config = validate_config(config)
        receipt = blank_receipt(config)
        lock_identity = stable_file_identity(
            LOCK_PATH,
            owners=frozenset({0, os.geteuid()}),
            maximum_bytes=MAX_REQUIREMENTS_LOCK_BYTES,
            allowed_write_mask=stat.S_IWGRP,
            capture_payload=True,
            label="dependency requirements lock",
        )
        assert lock_identity.payload is not None
        expected_requirements = parse_requirements_lock_bytes(
            lock_identity.payload
        )
        dependency_target = f"sha256:{lock_identity.sha256}"
        source_targets = {
            "pip-audit": dependency_target,
            "trivy:web": config.web_image,
            "trivy:render": config.render_image,
        }
        waivers = load_waivers(
            config.waivers_path,
            release_commit_sha=config.release_commit_sha,
            source_targets=source_targets,
            now=now,
        )
        receipt["identities"]["dependency_lock"] = (
            lock_identity.receipt_value()
        )
        receipt["waivers"]["configured"] = len(waivers)

        missing_tools = [tool for tool in REQUIRED_TOOLS if not runner.available(tool)]
        for tool in REQUIRED_TOOLS:
            receipt["tools"][tool]["available"] = tool not in missing_tools
        if missing_tools:
            message = f"required scanners are missing: {', '.join(missing_tools)}"
            raise ScannerExecutionError(message)

        authenticated_identities = runner_identities(runner, now=now)
        for tool, identity in authenticated_identities.items():
            receipt["tools"][tool]["identity"] = identity

        version_commands = {
            "pip-audit": ("pip-audit", "--version"),
            "syft": ("syft", "--version"),
            "trivy": ("trivy", "--version"),
        }
        for tool, argv in version_commands.items():
            result = scanner_command(runner, argv, timeout_seconds=config.timeout_seconds)
            observed_version = scanner_version(tool, result.stdout)
            expected_version = receipt["tools"][tool]["identity"].get(
                "version"
            )
            if observed_version != expected_version:
                raise SecurityValidationError(
                    f"{tool} version output differs from its authenticated identity"
                )
            receipt["tools"][tool]["version"] = observed_version
            receipt["tools"][tool]["version_output"] = output_evidence(result.stdout)

        assert lock_identity is not None
        assert lock_identity.payload is not None
        try:
            with tempfile.TemporaryDirectory(
                prefix="propertyquarry-security-input.",
                dir="/tmp",
            ) as snapshot_directory:
                snapshot_path = (
                    Path(snapshot_directory) / "requirements.lock"
                )
                publish_artifact(
                    snapshot_path,
                    lock_identity.payload,
                    overwrite=False,
                )
                snapshot_identity = stable_file_identity(
                    snapshot_path,
                    owners=frozenset({0, os.geteuid()}),
                    maximum_bytes=MAX_REQUIREMENTS_LOCK_BYTES,
                    expected_sha256=lock_identity.sha256,
                    expected_mode=0o600,
                    label="private dependency-lock snapshot",
                )
                pip_result = scanner_command(
                    runner,
                    (
                        "pip-audit",
                        "--requirement",
                        str(snapshot_path),
                        "--no-deps",
                        "--disable-pip",
                        "--vulnerability-service",
                        "osv",
                        "--progress-spinner",
                        "off",
                        "--format",
                        "json",
                    ),
                    timeout_seconds=config.timeout_seconds,
                    accepted_returncodes=frozenset({0, 1}),
                )
                snapshot_after = stable_file_identity(
                    snapshot_path,
                    owners=frozenset({0, os.geteuid()}),
                    maximum_bytes=MAX_REQUIREMENTS_LOCK_BYTES,
                    expected_sha256=lock_identity.sha256,
                    expected_mode=0o600,
                    label="private dependency-lock snapshot",
                )
                if (
                    snapshot_after.receipt_value()
                    != snapshot_identity.receipt_value()
                ):
                    raise SecurityValidationError(
                        "private dependency-lock snapshot changed during audit"
                    )
        except OSError as exc:
            raise ScannerExecutionError(
                "private dependency-lock snapshot cannot be cleaned"
            ) from exc
        pip_payload = parse_json_document(pip_result.stdout, document_name="pip-audit output")
        findings = parse_pip_audit(
            pip_payload,
            target=dependency_target,
            expected_requirements=expected_requirements,
        )
        dependency_artifact = config.artifacts_dir / "dependencies.pip-audit.json"
        dependency_payload = json_document_bytes(pip_payload)
        artifact_payloads[dependency_artifact] = dependency_payload
        receipt["artifacts"]["dependencies"] = payload_identity(
            dependency_artifact,
            dependency_payload,
        )

        for target_name, image, source in (
            ("web", config.web_image, "trivy:web"),
            ("render", config.render_image, "trivy:render"),
        ):
            syft_result = scanner_command(
                runner,
                ("syft", f"docker:{image}", "--output", "cyclonedx-json"),
                timeout_seconds=config.timeout_seconds,
            )
            sbom_payload = parse_json_document(
                syft_result.stdout, document_name=f"{target_name} Syft SBOM"
            )
            component_count = validate_cyclonedx_sbom(
                sbom_payload,
                target_name=target_name,
                target=image,
            )
            sbom_path = config.artifacts_dir / f"{target_name}.sbom.cdx.json"
            sbom_bytes = json_document_bytes(sbom_payload)
            artifact_payloads[sbom_path] = sbom_bytes

            # Trivy's SBOM mode identifies the input file, not Syft's source image.
            # Local image mode gives the report its own immutable image identity.
            trivy_result = scanner_command(
                runner,
                (
                    "trivy",
                    "image",
                    "--image-src",
                    "docker",
                    "--skip-db-update",
                    "--skip-java-db-update",
                    "--skip-vex-repo-update",
                    "--skip-version-check",
                    "--offline-scan",
                    "--cache-backend",
                    "memory",
                    "--scanners",
                    "vuln",
                    "--format",
                    "json",
                    image,
                ),
                timeout_seconds=config.timeout_seconds,
            )
            trivy_payload = parse_json_document(
                trivy_result.stdout, document_name=f"{target_name} Trivy output"
            )
            image_findings = parse_trivy(trivy_payload, source=source, target=image)
            trivy_path = config.artifacts_dir / f"{target_name}.trivy.json"
            trivy_bytes = json_document_bytes(trivy_payload)
            artifact_payloads[trivy_path] = trivy_bytes
            findings.extend(image_findings)
            receipt["artifacts"][target_name] = {
                "image": image,
                "sbom": payload_identity(sbom_path, sbom_bytes),
                "sbom_format": "CycloneDX",
                "component_count": component_count,
                "vulnerability_scan": payload_identity(
                    trivy_path,
                    trivy_bytes,
                ),
            }

        assert authenticated_identities is not None
        if runner_identities(runner, now=now) != authenticated_identities:
            raise SecurityValidationError(
                "authenticated scanner runtime changed during the security scan"
            )
        lock_after = stable_file_identity(
            LOCK_PATH,
            owners=frozenset({0, os.geteuid()}),
            maximum_bytes=MAX_REQUIREMENTS_LOCK_BYTES,
            expected_sha256=lock_identity.sha256,
            allowed_write_mask=stat.S_IWGRP,
            label="dependency requirements lock",
        )
        if lock_after.receipt_value() != lock_identity.receipt_value():
            raise SecurityValidationError(
                "dependency requirements lock changed during the security scan"
            )

        findings = sorted(
            findings,
            key=lambda item: (
                -SEVERITY_RANK[item.effective_severity],
                item.source,
                item.vulnerability_id,
                item.package,
            ),
        )
        waiver_by_finding = {waiver.finding_key: waiver for waiver in waivers}
        threshold_rank = SEVERITY_RANK[config.severity_threshold]
        applicable = [
            finding
            for finding in findings
            if SEVERITY_RANK[finding.effective_severity] >= threshold_rank
        ]
        applied: list[Waiver] = []
        blocking: list[Finding] = []
        finding_rows: list[dict[str, object]] = []
        for finding in findings:
            waiver = waiver_by_finding.get(finding.key)
            waiver_applies = waiver is not None and waiver.severity == finding.severity
            if waiver_applies:
                applied.append(waiver)
            if finding in applicable and not waiver_applies:
                blocking.append(finding)
            row: dict[str, object] = finding.receipt_value()
            row["at_or_above_threshold"] = finding in applicable
            row["waiver_id"] = waiver.waiver_id if waiver_applies else None
            finding_rows.append(row)

        applied_ids = {waiver.waiver_id for waiver in applied}
        receipt["findings"] = finding_rows
        receipt["waivers"] = {
            "configured": len(waivers),
            "applied": [waiver.receipt_value() for waiver in applied],
            "unused": [
                waiver.receipt_value() for waiver in waivers if waiver.waiver_id not in applied_ids
            ],
        }
        receipt["summary"] = {
            "total": len(findings),
            "at_or_above_threshold": len(applicable),
            "waived": len(applied),
            "blocking": len(blocking),
            "by_effective_severity": {
                severity: sum(1 for item in findings if item.effective_severity == severity)
                for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            },
        }
        if blocking:
            receipt["status"] = "failed" if config.flagship else "advisory_findings"
            receipt["gate_passed"] = False
            exit_code = 1 if config.flagship else 0
        else:
            receipt["status"] = "pass"
            receipt["gate_passed"] = True
    except SecurityValidationError as exc:
        exit_code = 2
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
    except ScannerExecutionError as exc:
        if config.flagship:
            exit_code = 2
            receipt["status"] = "failed"
        else:
            exit_code = 0
            receipt["status"] = "advisory_unavailable"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
    except Exception:
        exit_code = 2 if config.flagship else 0
        receipt["status"] = "failed" if config.flagship else "advisory_unavailable"
        receipt["error"] = {
            "type": "UnexpectedSecurityGateError",
            "message": "unexpected security-gate failure; scanner output was withheld",
        }

    if owns_runner:
        close_runner = getattr(runner, "close", None)
        if callable(close_runner):
            try:
                close_runner()
            except Exception:
                exit_code = 2
                receipt["status"] = "failed"
                receipt["gate_passed"] = False
                receipt["error"] = {
                    "type": "ScannerCleanupError",
                    "message": "private scanner cleanup failed",
                }

    receipt["completed_at"] = isoformat(utc_now())
    try:
        for artifact_path in sorted(artifact_payloads, key=str):
            publish_artifact(
                artifact_path,
                artifact_payloads[artifact_path],
                overwrite=config.overwrite_receipt,
            )
    except SecurityValidationError as exc:
        exit_code = 2
        receipt["status"] = "failed"
        receipt["gate_passed"] = False
        receipt["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    atomic_write_json(
        config.receipt_path,
        receipt,
        overwrite=config.overwrite_receipt,
    )
    return receipt, exit_code


def default_output_paths(release_sha: str) -> tuple[Path, Path]:
    identity = release_sha if GIT_SHA_RE.fullmatch(str(release_sha or "")) else "invalid-release"
    root = APP_ROOT / "_completion" / "propertyquarry_release_security" / identity
    return root / "artifacts", root / "receipt.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gate a digest-pinned PropertyQuarry release with authenticated "
            "scanner and database identities."
        )
    )
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--render-image", required=True)
    parser.add_argument(
        "--severity-threshold",
        required=True,
        choices=("LOW", "MEDIUM", "HIGH", "CRITICAL"),
    )
    parser.add_argument("--flagship", action="store_true")
    parser.add_argument("--waivers", type=Path, default=DEFAULT_WAIVERS_PATH)
    parser.add_argument("--artifacts-dir", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--overwrite-receipt", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_artifacts, default_receipt = default_output_paths(args.release_sha)
    config = GateConfig(
        release_commit_sha=args.release_sha,
        web_image=args.web_image,
        render_image=args.render_image,
        severity_threshold=args.severity_threshold,
        flagship=args.flagship,
        waivers_path=args.waivers,
        artifacts_dir=args.artifacts_dir or default_artifacts,
        receipt_path=args.receipt or default_receipt,
        timeout_seconds=args.timeout_seconds,
        overwrite_receipt=args.overwrite_receipt,
    )
    try:
        receipt, exit_code = run_security_gate(config=config)
    except SecurityGateError as exc:
        print(f"PropertyQuarry security receipt error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"status": receipt.get("status"), "receipt": str(config.receipt_path)},
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
