#!/usr/bin/env python3
"""Bootstrap a local, scope-limited PropertyQuarry Ed25519 authority.

The bootstrap is an explicit trust-on-first-use operation.  It creates six
independent signing identities and one exact nine-file package-input bundle,
then publishes the complete state directory with a same-parent NOREPLACE
rename.  It does not authenticate or install a release package, contact
Docker, or grant authority outside the local Docker scope.
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
from typing import Callable, Final, NoReturn

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

try:  # Support repo imports and direct execution from scripts/.
    from scripts import propertyquarry_release_preflight_policy as preflight
except ModuleNotFoundError:  # pragma: no cover - direct CLI compatibility
    import propertyquarry_release_preflight_policy as preflight  # type: ignore[no-redef]


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT: Final = (
    REPOSITORY_ROOT
    / "state"
    / "runtime"
    / "propertyquarry-release-authority-v2"
)
BOOTSTRAP_SCHEMA: Final = (
    "propertyquarry.release-control.local-identity-bootstrap.v2"
)
BOOTSTRAP_SIGNATURE_DOMAIN: Final = BOOTSTRAP_SCHEMA.encode("ascii") + b"\0"
SCOPE_ID: Final = "propertyquarry-local-docker"
MAX_PATH_BYTES: Final = 4096
MAX_KEY_BYTES: Final = 4096
RENAME_NOREPLACE: Final = 1
_SHA1_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

KEY_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("request", "request-authority-v2"),
    ("response", "response-authority-v2"),
    ("lifecycle-cas", "lifecycle-cas-v2"),
    ("evidence", "evidence-authority-v2"),
    ("resource-mediator", "resource-mediator-v2"),
    ("package", "package-authority-v2"),
)
PACKAGE_INPUT_FILES: Final = frozenset(
    {
        "controller-v2.json",
        "watchdog-v2.json",
        "policy-v2.json",
        *(f"trust.d/{stem}.pem" for _role, stem in KEY_SPECS),
    }
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "version",
        "authority_scope",
        "candidate_sha",
        "workflow_sha",
        "private_key_count",
        "public_key_count",
        "package_input_file_count",
        "package_input_files",
        "package_input_file_sha256",
        "keys",
        "receipt_signature",
    }
)
_KEY_RECORD_KEYS: Final = frozenset(
    {
        "role",
        "key_id",
        "algorithm",
        "private_key_path",
        "private_key_sha256",
        "external_anchor_path",
        "external_anchor_sha256",
        "package_input_path",
        "package_input_sha256",
    }
)
_SIGNATURE_RECORD_KEYS: Final = frozenset(
    {"algorithm", "encoding", "key_id", "signed_message"}
)


class LocalIdentityError(ValueError):
    """A secret-free, deterministic bootstrap rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise LocalIdentityError(code)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("canonical-json-invalid")


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


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _framed(domain: bytes, raw: bytes) -> bytes:
    return domain + len(raw).to_bytes(8, "big", signed=False) + raw


def _absolute_path(value: object, code: str) -> str:
    if type(value) is not str or not value or not os.path.isabs(value):
        _fail(code)
    if value == "/" or os.path.normpath(value) != value:
        _fail(code)
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        _fail(code)
    try:
        if len(value.encode("utf-8")) > MAX_PATH_BYTES:
            _fail(code)
    except UnicodeEncodeError:
        _fail(code)
    return value


def _open_flags(*, directory: bool, write: bool = False) -> int:
    if any(not hasattr(os, name) for name in ("O_CLOEXEC", "O_NOFOLLOW")):
        _fail("platform-open-flags-unavailable")
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            _fail("platform-open-flags-unavailable")
        flags |= os.O_DIRECTORY
    elif hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
    )


def _parent_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_controlled_parent(path: str) -> tuple[int, tuple[int, ...]]:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            _fail("state-parent-type-invalid")
        if os.path.realpath(path) != path:
            _fail("state-parent-symlink-rejected")
        if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022:
            _fail("state-parent-metadata-unsafe")
        descriptor = os.open(path, _open_flags(directory=True))
        opened = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(opened):
            os.close(descriptor)
            _fail("state-parent-mutated")
        return descriptor, _parent_identity(opened)
    except LocalIdentityError:
        raise
    except OSError:
        _fail("state-parent-open-failed")


def _revalidate_parent(
    path: str,
    descriptor: int,
    expected: tuple[int, ...],
) -> None:
    reopened = -1
    try:
        path_metadata = os.lstat(path)
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(
            path_metadata.st_mode
        ):
            _fail("state-parent-replaced")
        if os.path.realpath(path) != path:
            _fail("state-parent-replaced")
        reopened = os.open(path, _open_flags(directory=True))
        if not (
            _parent_identity(path_metadata)
            == _parent_identity(os.fstat(reopened))
            == _parent_identity(os.fstat(descriptor))
            == expected
        ):
            _fail("state-parent-replaced")
    except LocalIdentityError:
        raise
    except OSError:
        _fail("state-parent-revalidation-failed")
    finally:
        if reopened >= 0:
            os.close(reopened)


def _mkdir_at(parent_fd: int, name: str, mode: int = 0o700) -> int:
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
        descriptor = os.open(name, _open_flags(directory=True), dir_fd=parent_fd)
        os.fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
        ):
            os.close(descriptor)
            _fail("state-directory-metadata-invalid")
        os.fsync(parent_fd)
        return descriptor
    except LocalIdentityError:
        raise
    except FileExistsError:
        _fail("state-path-collision")
    except OSError:
        _fail("state-directory-create-failed")


def _write_at(parent_fd: int, name: str, raw: bytes, mode: int) -> None:
    if type(raw) is not bytes or not raw:
        _fail("state-file-bytes-invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            _open_flags(directory=False, write=True) | os.O_CREAT | os.O_EXCL,
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                _fail("state-file-write-failed")
            offset += written
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_size != len(raw)
        ):
            _fail("state-file-metadata-invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = bytearray()
        while len(observed) <= len(raw):
            chunk = os.read(
                descriptor,
                min(65_536, len(raw) + 1 - len(observed)),
            )
            if not chunk:
                break
            observed.extend(chunk)
        if bytes(observed) != raw:
            _fail("state-file-copy-mismatch")
        os.fsync(parent_fd)
    except LocalIdentityError:
        raise
    except FileExistsError:
        _fail("state-path-collision")
    except OSError:
        _fail("state-file-write-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = library.renameat2
    except (OSError, AttributeError):
        _fail("renameat2-unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        _fail("state-exists")
    if error == errno.EXDEV:
        _fail("state-cross-device")
    _fail("state-publish-failed")


def _erase_directory_contents_fd(directory_fd: int) -> None:
    """Erase only descendants reached through an already-pinned directory fd."""

    if not hasattr(os, "O_PATH"):
        _fail("platform-open-flags-unavailable")

    def visit(current_fd: int, depth: int) -> None:
        if depth > 64:
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
                    if _file_identity(metadata) != _file_identity(opened):
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
                    if _file_identity(metadata) != _file_identity(opened):
                        _fail("temporary-cleanup-entry-mutated")
                    current = os.stat(
                        name,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if _file_identity(current) != _file_identity(opened):
                        _fail("temporary-cleanup-entry-mutated")
                    os.unlink(name, dir_fd=current_fd)
                    if os.fstat(child_fd).st_nlink != max(opened.st_nlink - 1, 0):
                        _fail("temporary-cleanup-entry-mutated")
            except LocalIdentityError:
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
        except LocalIdentityError:
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
    except LocalIdentityError:
        raise
    except OSError:
        _fail("temporary-cleanup-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _revalidate_temp_root(
    parent_fd: int,
    name: str,
    pinned_fd: int,
    expected_identity: tuple[int, ...],
) -> None:
    descriptor = -1
    try:
        path_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(
            path_metadata.st_mode
        ):
            _fail("temporary-root-replaced")
        descriptor = os.open(
            name,
            _open_flags(directory=True),
            dir_fd=parent_fd,
        )
        if not (
            _directory_identity(path_metadata)
            == _directory_identity(os.fstat(descriptor))
            == _directory_identity(os.fstat(pinned_fd))
            == expected_identity
        ):
            _fail("temporary-root-replaced")
    except LocalIdentityError:
        raise
    except OSError:
        _fail("temporary-root-revalidation-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _public_material(
    private_key: Ed25519PrivateKey,
) -> tuple[bytes, bytes, str]:
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return public_pem, public_der, _digest(public_der)


def _private_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _local_endpoint(path: str) -> str:
    return f"https://propertyquarry-local-authority.invalid/{path}"


def _controller_config(*, workflow_sha: str) -> dict[str, object]:
    def mediator(name: str) -> dict[str, str]:
        return {
            "endpoint": _local_endpoint(f"mediators/{name}"),
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "resource-mediator-v2.pem"
            ),
            "credential_name": "resource-mediator-client",
        }

    return {
        "schema": "propertyquarry.release-control.controller-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "identity_policy": {
            "repository": "ArchonMegalon/property",
            "ref": "refs/heads/main",
            "workflow_ref": (
                "ArchonMegalon/property/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": workflow_sha,
            "job": "propertyquarry-release-v2",
            "environment": "propertyquarry-production",
        },
        "oidc": {
            "allowed_request_url_origin": (
                "https://vstoken.actions.githubusercontent.com"
            ),
            "audience": "propertyquarry-release-control-v2",
        },
        "root_policy_path": (
            "/etc/propertyquarry-release-control/policy-v2.json"
        ),
        "package_trust_root_path": (
            "/etc/propertyquarry-release-control/trust.d/"
            "package-authority-v2.pem"
        ),
        "authorities": {
            "request": {
                "endpoint": _local_endpoint("authorities/request"),
                "request_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "request-authority-v2.pem"
                ),
                "response_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "response-authority-v2.pem"
                ),
                "credential_name": "request-authority-client",
            },
            "lifecycle_cas": {
                "endpoint": _local_endpoint("authorities/lifecycle-cas"),
                "trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "lifecycle-cas-v2.pem"
                ),
                "credential_name": "lifecycle-cas-client",
            },
            "evidence_store": {
                "endpoint": _local_endpoint("authorities/evidence"),
                "trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "evidence-authority-v2.pem"
                ),
                "credential_name": "evidence-store-client",
            },
        },
        "resource_mediators": {
            name: mediator(name.replace("_", "-"))
            for name in (
                "database",
                "launch_authority",
                "monitoring_delivery",
                "overlay",
                "public_tour",
                "runtime",
                "traffic",
            )
        },
        "state": {
            "cache_path": (
                "/var/lib/propertyquarry-release-control-v2/cache"
            ),
            "journal_path": (
                "/var/lib/propertyquarry-release-control-v2/journal"
            ),
            "receipt_path": (
                "/var/lib/propertyquarry-release-control-v2/receipts"
            ),
        },
        "limits": {
            "identity_token_limit_bytes": 16_384,
            "request_limit_bytes": 1_048_576,
            "response_limit_bytes": 1_048_576,
            "diagnostic_limit_bytes": 65_536,
            "callback_timeout_seconds": 30,
            "operation_timeout_seconds": 9_600,
        },
    }


def _watchdog_config() -> dict[str, object]:
    return {
        "schema": "propertyquarry.release-control.watchdog-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "root_policy_path": (
            "/etc/propertyquarry-release-control/policy-v2.json"
        ),
        "lifecycle_cas": {
            "endpoint": _local_endpoint("authorities/lifecycle-cas"),
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "lifecycle-cas-v2.pem"
            ),
            "credential_name": "watchdog-takeover-client",
        },
        "resource_recovery": {
            "endpoint": _local_endpoint("mediators/resource-recovery"),
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "resource-mediator-v2.pem"
            ),
            "credential_name": "resource-recovery-client",
        },
        "resource_kinds": [
            "database",
            "launch-authority",
            "monitoring-delivery",
            "overlay",
            "public-tour",
            "runtime",
            "traffic",
        ],
        "state": {
            "cache_path": (
                "/var/lib/propertyquarry-release-watchdog-v2/cache"
            )
        },
        "limits": {
            "poll_interval_seconds": 10,
            "callback_timeout_seconds": 30,
            "reconciliation_timeout_seconds": 1_800,
        },
    }


def _root_policy(*, candidate_sha: str, workflow_sha: str) -> dict[str, object]:
    return {
        "schema": "propertyquarry.release-root-policy.v2",
        "identity": {
            "audience": "propertyquarry-release-control-v2",
            "repository": "ArchonMegalon/property",
            "ref": "refs/heads/main",
            "candidate_sha": candidate_sha,
            "workflow_ref": (
                "ArchonMegalon/property/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": workflow_sha,
            "run_id": "local-authority-bootstrap-v2",
            "run_attempt": 1,
            "job": "propertyquarry-release-v2",
            "environment": "propertyquarry-production",
        },
        "required_checks": list(preflight.REQUIRED_CHECK_IDS),
        "decision_policy_digest": preflight.REQUIRED_CHECK_SET_DIGEST,
        "max_request_ttl": 900,
        "max_preflight_validity": 3_600,
    }


@dataclasses.dataclass(frozen=True, slots=True)
class BootstrapResult:
    state_root: str
    receipt_sha256: str
    receipt_signature_sha256: str
    package_key_id: str
    package_private_key: str
    package_external_anchor: str
    package_input: str

    def document(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def _expected_state_paths() -> tuple[set[str], set[str]]:
    directories = {"keys", "anchors", "package-input", "package-input/trust.d"}
    files = {
        "identity-bootstrap-receipt.v2.json",
        "identity-bootstrap-receipt.v2.sig",
        *(f"keys/{stem}.key" for _role, stem in KEY_SPECS),
        *(f"anchors/{stem}.pem" for _role, stem in KEY_SPECS),
        *(f"package-input/{path}" for path in PACKAGE_INPUT_FILES),
    }
    return directories, files


def _verify_state_tree(
    root_fd: int,
) -> tuple[
    tuple[tuple[str, tuple[int, ...]], ...],
    tuple[tuple[str, tuple[int, ...]], ...],
]:
    expected_directories, expected_files = _expected_state_paths()
    observed_directories: dict[str, tuple[int, ...]] = {}
    observed_files: dict[str, tuple[int, ...]] = {}

    def visit(directory_fd: int, prefix: str) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                entries = tuple(iterator)
        except OSError:
            _fail("state-tree-enumeration-failed")
        for entry in entries:
            name = entry.name
            if (
                type(name) is not str
                or name in {"", ".", ".."}
                or "/" in name
                or any(ord(character) < 0x20 for character in name)
            ):
                _fail("state-tree-name-invalid")
            relative = f"{prefix}/{name}" if prefix else name
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                _fail("state-tree-stat-failed")
            if stat.S_ISLNK(metadata.st_mode):
                _fail("state-symlink-rejected")
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in expected_directories:
                    _fail("state-tree-set-invalid")
                if (
                    stat.S_IMODE(metadata.st_mode) != 0o700
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_gid != os.getegid()
                ):
                    _fail("state-directory-metadata-invalid")
                child = os.open(
                    name,
                    _open_flags(directory=True),
                    dir_fd=directory_fd,
                )
                try:
                    if _directory_identity(metadata) != _directory_identity(
                        os.fstat(child)
                    ):
                        _fail("state-tree-mutated")
                    observed_directories[relative] = _file_identity(
                        os.fstat(child)
                    )
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in expected_files:
                    _fail("state-tree-set-invalid")
                expected_mode = (
                    0o600
                    if relative.startswith(("keys/", "anchors/", "package-input/"))
                    else 0o600
                )
                if (
                    metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != expected_mode
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_gid != os.getegid()
                ):
                    _fail("state-file-metadata-invalid")
                observed_files[relative] = _file_identity(metadata)
            else:
                _fail("state-special-file-rejected")

    visit(root_fd, "")
    if (
        set(observed_directories) != expected_directories
        or set(observed_files) != expected_files
    ):
        _fail("state-tree-set-invalid")
    return (
        tuple(sorted(observed_directories.items())),
        tuple(sorted(observed_files.items())),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _StateFileSnapshot:
    relative: str
    raw: bytes
    identity: tuple[int, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class _StateSnapshot:
    root_identity: tuple[int, ...]
    directories: tuple[tuple[str, tuple[int, ...]], ...]
    files: tuple[_StateFileSnapshot, ...]

    def by_relative(self) -> dict[str, _StateFileSnapshot]:
        return {item.relative: item for item in self.files}


def _state_file_limit(relative: str) -> int:
    if relative == "identity-bootstrap-receipt.v2.json":
        return 256 * 1024
    if relative == "identity-bootstrap-receipt.v2.sig":
        return 64
    if relative.startswith(("keys/", "anchors/")) or relative.startswith(
        "package-input/trust.d/"
    ):
        return MAX_KEY_BYTES
    return 256 * 1024


def _read_state_file(root_fd: int, relative: str) -> _StateFileSnapshot:
    components = relative.split("/")
    current = -1
    descriptor = -1
    try:
        current = os.dup(root_fd)
        for component in components[:-1]:
            metadata = os.stat(
                component,
                dir_fd=current,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                _fail("state-file-ancestor-invalid")
            following = os.open(
                component,
                _open_flags(directory=True),
                dir_fd=current,
            )
            if _file_identity(metadata) != _file_identity(os.fstat(following)):
                os.close(following)
                _fail("state-tree-mutated")
            os.close(current)
            current = following
        before = os.stat(
            components[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
        ):
            _fail("state-file-metadata-invalid")
        descriptor = os.open(
            components[-1],
            _open_flags(directory=False),
            dir_fd=current,
        )
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened):
            _fail("state-tree-mutated")
        maximum = _state_file_limit(relative)
        if opened.st_size < 1 or opened.st_size > maximum:
            _fail("state-file-size-invalid")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(
                descriptor,
                min(65_536, maximum + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        path_after = os.stat(
            components[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
        if (
            len(raw) != opened.st_size
            or len(raw) > maximum
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(after) != _file_identity(path_after)
        ):
            _fail("state-tree-mutated")
        return _StateFileSnapshot(relative, bytes(raw), _file_identity(after))
    except LocalIdentityError:
        raise
    except OSError:
        _fail("state-file-read-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)


def _snapshot_state(root_fd: int) -> _StateSnapshot:
    try:
        root_before = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or stat.S_IMODE(root_before.st_mode) != 0o700
            or root_before.st_uid != os.geteuid()
            or root_before.st_gid != os.getegid()
        ):
            _fail("state-root-metadata-invalid")
        root_identity = _file_identity(root_before)
        first_directories, first_files = _verify_state_tree(root_fd)
        _expected_directories, expected_files = _expected_state_paths()
        files = tuple(
            _read_state_file(root_fd, relative)
            for relative in sorted(expected_files)
        )
        second_directories, second_files = _verify_state_tree(root_fd)
        if (
            first_directories != second_directories
            or first_files != second_files
            or _file_identity(os.fstat(root_fd)) != root_identity
        ):
            _fail("state-tree-mutated")
        if tuple((item.relative, item.identity) for item in files) != first_files:
            _fail("state-tree-mutated")
        return _StateSnapshot(root_identity, first_directories, files)
    except LocalIdentityError:
        raise
    except OSError:
        _fail("state-snapshot-failed")


def _state_binding(
    snapshot: _StateSnapshot,
) -> tuple[tuple[int, ...], tuple[object, ...], tuple[object, ...]]:
    root = snapshot.root_identity
    stable_root = (
        root[0],
        root[1],
        stat.S_IFMT(root[2]),
        stat.S_IMODE(root[2]),
        root[3],
        root[4],
        root[5],
    )
    return stable_root, snapshot.directories, snapshot.files


class _DuplicateReceiptKey(ValueError):
    pass


def _unique_receipt_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateReceiptKey(key)
        result[key] = value
    return result


def _strict_receipt(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > 256 * 1024:
        _fail("receipt-json-invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_receipt_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateReceiptKey,
        ValueError,
        RecursionError,
    ):
        _fail("receipt-json-invalid")
    if type(value) is not dict or _canonical_bytes(value) != raw:
        _fail("receipt-json-invalid")
    return value


def _private_key_binding(raw: bytes) -> tuple[bytes, str]:
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        _fail("state-private-key-invalid")
    if not isinstance(key, Ed25519PrivateKey) or _private_pem(key) != raw:
        _fail("state-private-key-invalid")
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return public_der, _digest(public_der)


def _public_key_binding(raw: bytes) -> tuple[Ed25519PublicKey, bytes, str]:
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        _fail("state-public-key-invalid")
    if not isinstance(key, Ed25519PublicKey):
        _fail("state-public-key-invalid")
    canonical = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical != raw:
        _fail("state-public-key-invalid")
    return key, public_der, _digest(public_der)


def _verify_state_snapshot(
    snapshot: _StateSnapshot,
    *,
    expected_candidate_sha: str | None = None,
    expected_workflow_sha: str | None = None,
    expected_receipt_raw: bytes | None = None,
    expected_signature: bytes | None = None,
) -> dict[str, object]:
    files = snapshot.by_relative()
    receipt_raw = files["identity-bootstrap-receipt.v2.json"].raw
    signature = files["identity-bootstrap-receipt.v2.sig"].raw
    if (
        expected_receipt_raw is not None
        and receipt_raw != expected_receipt_raw
    ):
        _fail("state-receipt-binding-invalid")
    if expected_signature is not None and signature != expected_signature:
        _fail("state-receipt-binding-invalid")
    if len(signature) != 64:
        _fail("receipt-signature-invalid")
    receipt = _strict_receipt(receipt_raw)
    if set(receipt) != _RECEIPT_KEYS:
        _fail("receipt-contract-invalid")
    candidate_sha = receipt["candidate_sha"]
    workflow_sha = receipt["workflow_sha"]
    if (
        receipt["schema"] != BOOTSTRAP_SCHEMA
        or type(receipt["schema"]) is not str
        or receipt["version"] != 2
        or type(receipt["version"]) is not int
        or not _exact_equal(
            receipt["authority_scope"],
            {
                "kind": "local-docker",
                "scope_id": SCOPE_ID,
                "authoritative_for_identity_bootstrap": True,
                "external_production_authority": False,
                "public_launch_authority": False,
                "performs_release_effects": False,
            },
        )
        or type(candidate_sha) is not str
        or _SHA1_RE.fullmatch(candidate_sha) is None
        or type(workflow_sha) is not str
        or _SHA1_RE.fullmatch(workflow_sha) is None
        or (expected_candidate_sha is not None and candidate_sha != expected_candidate_sha)
        or (expected_workflow_sha is not None and workflow_sha != expected_workflow_sha)
        or receipt["private_key_count"] != len(KEY_SPECS)
        or type(receipt["private_key_count"]) is not int
        or receipt["public_key_count"] != len(KEY_SPECS)
        or type(receipt["public_key_count"]) is not int
        or receipt["package_input_file_count"] != len(PACKAGE_INPUT_FILES)
        or type(receipt["package_input_file_count"]) is not int
        or receipt["package_input_files"] != sorted(PACKAGE_INPUT_FILES)
    ):
        _fail("receipt-contract-invalid")

    package_digests = receipt["package_input_file_sha256"]
    if (
        type(package_digests) is not dict
        or set(package_digests) != set(PACKAGE_INPUT_FILES)
        or any(
            type(path) is not str
            or type(digest) is not str
            or _DIGEST_RE.fullmatch(digest) is None
            for path, digest in package_digests.items()
        )
    ):
        _fail("receipt-contract-invalid")
    for relative in PACKAGE_INPUT_FILES:
        actual = files[f"package-input/{relative}"].raw
        if package_digests[relative] != _digest(actual):
            _fail("state-package-input-binding-invalid")

    expected_configs = {
        "controller-v2.json": _canonical_bytes(
            _controller_config(workflow_sha=workflow_sha)
        ),
        "watchdog-v2.json": _canonical_bytes(_watchdog_config()),
        "policy-v2.json": _canonical_bytes(
            _root_policy(
                candidate_sha=candidate_sha,
                workflow_sha=workflow_sha,
            )
        ),
    }
    for relative, expected in expected_configs.items():
        if files[f"package-input/{relative}"].raw != expected:
            _fail("state-package-config-binding-invalid")

    key_records = receipt["keys"]
    if type(key_records) is not list or len(key_records) != len(KEY_SPECS):
        _fail("receipt-contract-invalid")
    observed_key_ids: set[str] = set()
    evidence_public: Ed25519PublicKey | None = None
    evidence_key_id: str | None = None
    for (role, stem), record_value in zip(KEY_SPECS, key_records):
        if type(record_value) is not dict or set(record_value) != _KEY_RECORD_KEYS:
            _fail("receipt-contract-invalid")
        record = record_value
        private_path = f"keys/{stem}.key"
        anchor_path = f"anchors/{stem}.pem"
        package_path = f"package-input/trust.d/{stem}.pem"
        package_relative = f"trust.d/{stem}.pem"
        digest_fields = (
            record["private_key_sha256"],
            record["external_anchor_sha256"],
            record["package_input_sha256"],
        )
        if (
            record["role"] != role
            or record["algorithm"] != "ed25519"
            or record["private_key_path"] != private_path
            or record["external_anchor_path"] != anchor_path
            or record["package_input_path"] != package_path
            or type(record["key_id"]) is not str
            or _DIGEST_RE.fullmatch(record["key_id"]) is None
            or any(
                type(value) is not str or _DIGEST_RE.fullmatch(value) is None
                for value in digest_fields
            )
        ):
            _fail("receipt-contract-invalid")
        private_raw = files[private_path].raw
        anchor_raw = files[anchor_path].raw
        package_raw = files[package_path].raw
        private_der, private_key_id = _private_key_binding(private_raw)
        public_key, anchor_der, anchor_key_id = _public_key_binding(anchor_raw)
        _package_key, package_der, package_key_id = _public_key_binding(package_raw)
        if (
            record["private_key_sha256"] != _digest(private_raw)
            or record["external_anchor_sha256"] != _digest(anchor_raw)
            or record["package_input_sha256"] != _digest(package_raw)
            or package_digests[package_relative] != _digest(package_raw)
            or not (
                private_der == anchor_der == package_der
                and private_key_id
                == anchor_key_id
                == package_key_id
                == record["key_id"]
            )
            or private_key_id in observed_key_ids
        ):
            _fail("state-key-binding-invalid")
        observed_key_ids.add(private_key_id)
        if role == "evidence":
            evidence_public = public_key
            evidence_key_id = private_key_id

    signature_record = receipt["receipt_signature"]
    if (
        evidence_public is None
        or evidence_key_id is None
        or type(signature_record) is not dict
        or set(signature_record) != _SIGNATURE_RECORD_KEYS
        or signature_record
        != {
            "algorithm": "ed25519",
            "encoding": "raw-64-byte",
            "key_id": evidence_key_id,
            "signed_message": (
                "domain-separated-uint64be-length-prefixed-canonical-json"
            ),
        }
    ):
        _fail("receipt-contract-invalid")
    try:
        evidence_public.verify(
            signature,
            _framed(BOOTSTRAP_SIGNATURE_DOMAIN, receipt_raw),
        )
    except InvalidSignature:
        _fail("receipt-signature-invalid")
    return receipt


def _directory_object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _rollback_published_state(
    parent_fd: int,
    *,
    name: str,
    pinned_fd: int,
) -> None:
    try:
        pinned = os.fstat(pinned_fd)
        pinned_object = _directory_object_identity(pinned)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.fsync(parent_fd)
        except OSError:
            _fail("published-rollback-durability-unknown")
        return
    except OSError:
        _fail("published-rollback-preflight-failed")
    if (
        not stat.S_ISDIR(pinned.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_object_identity(current) != pinned_object
    ):
        _fail("published-rollback-target-replaced")

    quarantine_name: str | None = None
    for _attempt in range(128):
        candidate = f".{name}.rollback.{secrets.token_hex(12)}"
        try:
            _rename_noreplace(parent_fd, name, candidate)
        except LocalIdentityError as error:
            if error.code == "state-exists":
                continue
            raise LocalIdentityError(f"published-rollback:{error.code}") from None
        quarantine_name = candidate
        break
    if quarantine_name is None:
        _fail("published-rollback-name-exhausted")

    quarantine_fd = -1
    try:
        quarantined = os.stat(
            quarantine_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        quarantine_fd = os.open(
            quarantine_name,
            _open_flags(directory=True),
            dir_fd=parent_fd,
        )
        opened = os.fstat(quarantine_fd)
        pinned_after = os.fstat(pinned_fd)
        if not (
            stat.S_ISDIR(quarantined.st_mode)
            and _directory_object_identity(quarantined) == pinned_object
            and _directory_object_identity(opened) == pinned_object
            and _directory_object_identity(pinned_after) == pinned_object
        ):
            _fail("published-rollback-target-replaced")
        cleanup_identity = _directory_identity(pinned_after)
    except LocalIdentityError:
        try:
            _rename_noreplace(parent_fd, quarantine_name, name)
        except LocalIdentityError:
            _fail("published-rollback-restore-failed")
        raise
    except OSError:
        _fail("published-rollback-revalidation-failed")
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)

    try:
        _remove_temp(parent_fd, quarantine_name, cleanup_identity)
    except LocalIdentityError as error:
        raise LocalIdentityError(f"published-rollback:{error.code}") from None
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        _fail("published-rollback-postcheck-failed")
    else:
        _fail("published-rollback-output-recreated")
    try:
        os.fsync(parent_fd)
    except OSError:
        _fail("published-rollback-durability-unknown")


def bootstrap_local_identity(
    *,
    state_root: str,
    candidate_sha: str,
    workflow_sha: str,
    phase_hook: Callable[[str], None] | None = None,
) -> BootstrapResult:
    """Atomically create one new local authority state directory."""

    target = _absolute_path(state_root, "state-root-invalid")
    if type(candidate_sha) is not str or _SHA1_RE.fullmatch(candidate_sha) is None:
        _fail("candidate-sha-invalid")
    if type(workflow_sha) is not str or _SHA1_RE.fullmatch(workflow_sha) is None:
        _fail("workflow-sha-invalid")
    parent = os.path.dirname(target)
    name = os.path.basename(target)
    if name in {"", ".", ".."}:
        _fail("state-root-invalid")
    parent_fd, parent_identity = _open_controlled_parent(parent)
    temp_name: str | None = None
    temp_fd = -1
    temp_identity: tuple[int, ...] | None = None
    published = False
    publication_complete = False

    def phase(value: str) -> None:
        if phase_hook is not None:
            phase_hook(value)

    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            _fail("state-preflight-failed")
        else:
            _fail("state-exists")

        for _attempt in range(128):
            candidate = f".{name}.initializing.{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError:
                _fail("temporary-directory-create-failed")
            temp_name = candidate
            temp_fd = os.open(
                candidate,
                _open_flags(directory=True),
                dir_fd=parent_fd,
            )
            os.fchmod(temp_fd, 0o700)
            temp_identity = _directory_identity(os.fstat(temp_fd))
            break
        if temp_name is None or temp_fd < 0 or temp_identity is None:
            _fail("temporary-name-exhausted")
        phase("temp-created")

        keys_fd = _mkdir_at(temp_fd, "keys")
        anchors_fd = _mkdir_at(temp_fd, "anchors")
        package_fd = _mkdir_at(temp_fd, "package-input")
        trust_fd = _mkdir_at(package_fd, "trust.d")
        generated: dict[
            str, tuple[Ed25519PrivateKey, bytes, bytes, bytes, str]
        ] = {}
        package_input_material: dict[str, bytes] = {}
        try:
            for role, stem in KEY_SPECS:
                private_key = Ed25519PrivateKey.generate()
                private_raw = _private_pem(private_key)
                public_pem, public_der, key_id = _public_material(private_key)
                if key_id in {item[4] for item in generated.values()}:
                    _fail("key-identity-collision")
                generated[role] = (
                    private_key,
                    private_raw,
                    public_pem,
                    public_der,
                    key_id,
                )
                _write_at(keys_fd, f"{stem}.key", private_raw, 0o600)
                _write_at(anchors_fd, f"{stem}.pem", public_pem, 0o600)
                _write_at(trust_fd, f"{stem}.pem", public_pem, 0o600)
                package_input_material[f"trust.d/{stem}.pem"] = public_pem
            phase("keys-written")

            controller_bytes = _canonical_bytes(
                _controller_config(workflow_sha=workflow_sha)
            )
            watchdog_bytes = _canonical_bytes(_watchdog_config())
            policy_bytes = _canonical_bytes(
                _root_policy(
                    candidate_sha=candidate_sha,
                    workflow_sha=workflow_sha,
                )
            )
            _write_at(package_fd, "controller-v2.json", controller_bytes, 0o600)
            _write_at(package_fd, "watchdog-v2.json", watchdog_bytes, 0o600)
            _write_at(package_fd, "policy-v2.json", policy_bytes, 0o600)
            package_input_material.update(
                {
                    "controller-v2.json": controller_bytes,
                    "watchdog-v2.json": watchdog_bytes,
                    "policy-v2.json": policy_bytes,
                }
            )
            phase("bundle-written")

            key_records = [
                {
                    "role": role,
                    "key_id": generated[role][4],
                    "algorithm": "ed25519",
                    "private_key_path": f"keys/{stem}.key",
                    "private_key_sha256": _digest(generated[role][1]),
                    "external_anchor_path": f"anchors/{stem}.pem",
                    "external_anchor_sha256": _digest(generated[role][2]),
                    "package_input_path": f"package-input/trust.d/{stem}.pem",
                    "package_input_sha256": _digest(generated[role][2]),
                }
                for role, stem in KEY_SPECS
            ]
            receipt = {
                "schema": BOOTSTRAP_SCHEMA,
                "version": 2,
                "authority_scope": {
                    "kind": "local-docker",
                    "scope_id": SCOPE_ID,
                    "authoritative_for_identity_bootstrap": True,
                    "external_production_authority": False,
                    "public_launch_authority": False,
                    "performs_release_effects": False,
                },
                "candidate_sha": candidate_sha,
                "workflow_sha": workflow_sha,
                "private_key_count": len(KEY_SPECS),
                "public_key_count": len(KEY_SPECS),
                "package_input_file_count": len(PACKAGE_INPUT_FILES),
                "package_input_files": sorted(PACKAGE_INPUT_FILES),
                "package_input_file_sha256": {
                    relative: _digest(package_input_material[relative])
                    for relative in sorted(PACKAGE_INPUT_FILES)
                },
                "keys": key_records,
                "receipt_signature": {
                    "algorithm": "ed25519",
                    "encoding": "raw-64-byte",
                    "key_id": generated["evidence"][4],
                    "signed_message": (
                        "domain-separated-uint64be-length-prefixed-canonical-json"
                    ),
                },
            }
            receipt_bytes = _canonical_bytes(receipt)
            receipt_signature = generated["evidence"][0].sign(
                _framed(BOOTSTRAP_SIGNATURE_DOMAIN, receipt_bytes)
            )
            if len(receipt_signature) != 64:
                _fail("receipt-signature-invalid")
            _write_at(
                temp_fd,
                "identity-bootstrap-receipt.v2.json",
                receipt_bytes,
                0o600,
            )
            _write_at(
                temp_fd,
                "identity-bootstrap-receipt.v2.sig",
                receipt_signature,
                0o600,
            )
            phase("receipt-written")
        finally:
            for descriptor in (trust_fd, package_fd, anchors_fd, keys_fd):
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            generated.clear()

        # Directory link count legitimately changed while the fixed child
        # directories were created.  Pin the completed root before the final
        # exact-tree audit and publication sequence.
        temp_identity = _directory_identity(os.fstat(temp_fd))
        initial_snapshot = _snapshot_state(temp_fd)
        _verify_state_snapshot(
            initial_snapshot,
            expected_candidate_sha=candidate_sha,
            expected_workflow_sha=workflow_sha,
            expected_receipt_raw=receipt_bytes,
            expected_signature=receipt_signature,
        )
        expected_state_binding = _state_binding(initial_snapshot)
        os.fsync(temp_fd)
        current_temp_identity = _directory_identity(os.fstat(temp_fd))
        if current_temp_identity != temp_identity:
            _fail("temporary-root-mutated")
        _revalidate_temp_root(parent_fd, temp_name, temp_fd, temp_identity)
        _revalidate_parent(parent, parent_fd, parent_identity)
        phase("before-publish")
        if _directory_identity(os.fstat(temp_fd)) != temp_identity:
            _fail("temporary-root-mutated")
        _revalidate_temp_root(parent_fd, temp_name, temp_fd, temp_identity)
        _revalidate_parent(parent, parent_fd, parent_identity)
        final_staged_snapshot = _snapshot_state(temp_fd)
        _verify_state_snapshot(
            final_staged_snapshot,
            expected_candidate_sha=candidate_sha,
            expected_workflow_sha=workflow_sha,
            expected_receipt_raw=receipt_bytes,
            expected_signature=receipt_signature,
        )
        if _state_binding(final_staged_snapshot) != expected_state_binding:
            _fail("state-pinned-identity-mutated")
        _revalidate_temp_root(parent_fd, temp_name, temp_fd, temp_identity)
        _revalidate_parent(parent, parent_fd, parent_identity)
        _rename_noreplace(parent_fd, temp_name, name)
        published = True
        _revalidate_temp_root(parent_fd, name, temp_fd, temp_identity)
        _revalidate_parent(parent, parent_fd, parent_identity)
        os.fsync(parent_fd)
        published_snapshot = _snapshot_state(temp_fd)
        _verify_state_snapshot(
            published_snapshot,
            expected_candidate_sha=candidate_sha,
            expected_workflow_sha=workflow_sha,
            expected_receipt_raw=receipt_bytes,
            expected_signature=receipt_signature,
        )
        if _state_binding(published_snapshot) != expected_state_binding:
            _fail("state-pinned-identity-mutated")
        _revalidate_temp_root(parent_fd, name, temp_fd, temp_identity)
        _revalidate_parent(parent, parent_fd, parent_identity)
        publication_complete = True

        result = BootstrapResult(
            state_root=target,
            receipt_sha256=_digest(receipt_bytes),
            receipt_signature_sha256=_digest(receipt_signature),
            package_key_id=next(
                item["key_id"]
                for item in key_records
                if item["role"] == "package"
            ),
            package_private_key=os.path.join(
                target, "keys", "package-authority-v2.key"
            ),
            package_external_anchor=os.path.join(
                target, "anchors", "package-authority-v2.pem"
            ),
            package_input=os.path.join(target, "package-input"),
        )
        return result
    finally:
        cleanup_error: LocalIdentityError | None = None
        try:
            if temp_fd >= 0:
                if published and not publication_complete:
                    _rollback_published_state(
                        parent_fd,
                        name=name,
                        pinned_fd=temp_fd,
                    )
                elif not published and temp_name is not None:
                    try:
                        cleanup_identity = _directory_identity(os.fstat(temp_fd))
                    except OSError:
                        _fail("temporary-cleanup-identity-missing")
                    _remove_temp(parent_fd, temp_name, cleanup_identity)
        except LocalIdentityError as error:
            cleanup_error = error
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            os.close(parent_fd)
        if cleanup_error is not None:
            raise cleanup_error


def verify_bootstrap_receipt(*, state_root: str) -> dict[str, object]:
    """Verify the public bootstrap receipt without exposing private material."""

    root = _absolute_path(state_root, "state-root-invalid")
    try:
        before = os.lstat(root)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            _fail("state-root-type-invalid")
        if os.path.realpath(root) != root:
            _fail("state-root-symlink-rejected")
        if (
            stat.S_IMODE(before.st_mode) != 0o700
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
        ):
            _fail("state-root-metadata-invalid")
        root_fd = os.open(root, _open_flags(directory=True))
    except LocalIdentityError:
        raise
    except OSError:
        _fail("state-root-open-failed")
    try:
        if _directory_identity(before) != _directory_identity(os.fstat(root_fd)):
            _fail("state-root-mutated")
        first_snapshot = _snapshot_state(root_fd)
        receipt = _verify_state_snapshot(first_snapshot)
        second_snapshot = _snapshot_state(root_fd)
        _verify_state_snapshot(second_snapshot)
        if first_snapshot != second_snapshot:
            _fail("state-tree-mutated")
        try:
            path_after = os.lstat(root)
        except OSError:
            _fail("state-root-revalidation-failed")
        if not (
            _directory_identity(path_after)
            == _directory_identity(os.fstat(root_fd))
            == _directory_identity(before)
        ):
            _fail("state-root-mutated")
        files = first_snapshot.by_relative()
        receipt_raw = files["identity-bootstrap-receipt.v2.json"].raw
        signature = files["identity-bootstrap-receipt.v2.sig"].raw
        return {
            "state_root": root,
            "receipt": receipt,
            "receipt_sha256": _digest(receipt_raw),
            "receipt_signature_sha256": _digest(signature),
        }
    finally:
        os.close(root_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap a local PropertyQuarry Ed25519 authority."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument(
        "--state-root",
        default=str(DEFAULT_STATE_ROOT),
    )
    bootstrap.add_argument("--candidate-sha", required=True)
    bootstrap.add_argument("--workflow-sha", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "bootstrap":
            result = bootstrap_local_identity(
                state_root=arguments.state_root,
                candidate_sha=arguments.candidate_sha,
                workflow_sha=arguments.workflow_sha,
            ).document()
        else:
            result = verify_bootstrap_receipt(state_root=arguments.state_root)
    except LocalIdentityError as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    print(_canonical_bytes(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
