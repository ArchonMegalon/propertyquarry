#!/usr/bin/env python3
"""Read-only, non-authoritative PropertyQuarry installation audit model.

This module verifies an exact, externally supplied byte/metadata manifest against
one fixed installation layout.  It never installs, repairs, authorizes, verifies
signatures, or asserts release readiness.  Production authority remains outside
the candidate checkout.
"""

from __future__ import annotations

import dataclasses
import enum
import errno
import hashlib
import json
import os
import re
import stat
from types import MappingProxyType
from typing import Any, Final, Literal


SCHEMA: Final = "propertyquarry.release-installation-manifest.v2"
RESULT_SCHEMA: Final = "propertyquarry.release-installation-audit-result.v2"
VERSION: Final = 2
AUTHORITATIVE: Final = False
PERFORMS_WRITES: Final = False
VERIFIES_SIGNATURES: Final = False
READINESS_AUTHORITY: Final = False
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_FILE_BYTES: Final = 128 * 1024 * 1024
MAX_JSON_DEPTH: Final = 32
MAX_PATH_BYTES: Final = 4096
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_ROLE_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,127}")
_MANIFEST_KEYS: Final = frozenset({"schema", "version", "roles"})
_ROLE_KEYS: Final = frozenset(
    {"role", "path", "sha256", "size", "mode", "uid", "gid"}
)


@dataclasses.dataclass(frozen=True, slots=True)
class RoleContract:
    role: str
    path: str
    mode: int
    production_uid: int = 0
    production_gid: int | None = 0


def _role(
    role: str,
    path: str,
    mode: int,
    *,
    production_gid: int | None = 0,
) -> RoleContract:
    return RoleContract(role, path, mode, 0, production_gid)


ROLE_CONTRACTS: Final[tuple[RoleContract, ...]] = (
    _role(
        "supervisor-executable",
        "/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2",
        0o755,
    ),
    _role(
        "controller-executable",
        "/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2",
        0o755,
    ),
    _role(
        "watchdog-executable",
        "/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2",
        0o755,
    ),
    _role(
        "systemd-socket-unit",
        "/usr/lib/systemd/system/propertyquarry-release-control-v2.socket",
        0o644,
    ),
    _role(
        "systemd-controller-template-unit",
        "/usr/lib/systemd/system/propertyquarry-release-control-v2@.service",
        0o644,
    ),
    _role(
        "systemd-watchdog-unit",
        "/usr/lib/systemd/system/propertyquarry-release-watchdog-v2.service",
        0o644,
    ),
    _role(
        "sysusers-config",
        "/usr/lib/sysusers.d/propertyquarry-release-control-v2.conf",
        0o644,
    ),
    _role(
        "tmpfiles-config",
        "/usr/lib/tmpfiles.d/propertyquarry-release-control-v2.conf",
        0o644,
    ),
    _role(
        "controller-schema",
        "/usr/share/propertyquarry-release-control-v2/schema/controller-v2.schema.json",
        0o644,
    ),
    _role(
        "watchdog-schema",
        "/usr/share/propertyquarry-release-control-v2/schema/watchdog-v2.schema.json",
        0o644,
    ),
    _role(
        "controller-config",
        "/etc/propertyquarry-release-control/controller-v2.json",
        0o640,
        production_gid=None,
    ),
    _role(
        "watchdog-config",
        "/etc/propertyquarry-release-control/watchdog-v2.json",
        0o640,
        production_gid=None,
    ),
    _role(
        "root-policy",
        "/etc/propertyquarry-release-control/policy-v2.json",
        0o640,
        production_gid=None,
    ),
    _role(
        "request-trust-root",
        "/etc/propertyquarry-release-control/trust.d/request-authority-v2.pem",
        0o640,
        production_gid=None,
    ),
    _role(
        "response-trust-root",
        "/etc/propertyquarry-release-control/trust.d/response-authority-v2.pem",
        0o640,
        production_gid=None,
    ),
    _role(
        "lifecycle-cas-trust-root",
        "/etc/propertyquarry-release-control/trust.d/lifecycle-cas-v2.pem",
        0o640,
        production_gid=None,
    ),
    _role(
        "evidence-trust-root",
        "/etc/propertyquarry-release-control/trust.d/evidence-authority-v2.pem",
        0o640,
        production_gid=None,
    ),
    _role(
        "resource-mediator-trust-root",
        "/etc/propertyquarry-release-control/trust.d/resource-mediator-v2.pem",
        0o640,
        production_gid=None,
    ),
    _role(
        "package-trust-root",
        "/etc/propertyquarry-release-control/trust.d/package-authority-v2.pem",
        0o640,
        production_gid=None,
    ),
)
ROLE_BY_NAME: Final = MappingProxyType(
    {contract.role: contract for contract in ROLE_CONTRACTS}
)
ROLE_NAMES: Final = tuple(contract.role for contract in ROLE_CONTRACTS)
SERVICE_GROUP_ROLES: Final = frozenset(
    {
        "controller-config",
        "watchdog-config",
        "root-policy",
        "request-trust-root",
        "response-trust-root",
        "lifecycle-cas-trust-root",
        "evidence-trust-root",
        "resource-mediator-trust-root",
        "package-trust-root",
    }
)


class InstallationModelError(ValueError):
    """Deterministic validation failure for caller-supplied expectations."""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}:{path}")


class BlockerCode(str, enum.Enum):
    MANIFEST_INVALID = "manifest-invalid"
    AUDIT_TARGET_INVALID = "audit-target-invalid"
    ROOTFS_OPEN_FAILED = "rootfs-open-failed"
    ROOTFS_SYMLINK_REJECTED = "rootfs-symlink-rejected"
    ROOTFS_NOT_DIRECTORY = "rootfs-not-directory"
    PATH_MISSING = "path-missing"
    PATH_SYMLINK_REJECTED = "path-symlink-rejected"
    ANCESTOR_NOT_DIRECTORY = "ancestor-not-directory"
    ANCESTOR_OWNER_MISMATCH = "ancestor-owner-mismatch"
    ANCESTOR_MODE_UNSAFE = "ancestor-mode-unsafe"
    PATH_OPEN_FAILED = "path-open-failed"
    NOT_REGULAR_FILE = "not-regular-file"
    HARDLINK_REJECTED = "hardlink-rejected"
    MODE_MISMATCH = "mode-mismatch"
    UID_MISMATCH = "uid-mismatch"
    GID_MISMATCH = "gid-mismatch"
    SIZE_MISMATCH = "size-mismatch"
    FILE_TOO_LARGE = "file-too-large"
    READ_FAILED = "read-failed"
    DIGEST_MISMATCH = "digest-mismatch"
    CONCURRENT_MUTATION = "concurrent-mutation"
    PLATFORM_UNSUPPORTED = "platform-unsupported"


@dataclasses.dataclass(frozen=True, slots=True)
class FileExpectation:
    role: str
    path: str
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int

    def document(self) -> dict[str, object]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class InstallationManifest:
    schema: str
    version: int
    roles: tuple[FileExpectation, ...]

    def document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "roles": [role.document() for role in self.roles],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ObservedFile:
    role: str
    path: str
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    link_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class InstallationBlocker:
    code: BlockerCode
    role: str | None
    path: str | None
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class InstallationAuditResult:
    schema: Literal["propertyquarry.release-installation-audit-result.v2"]
    version: Literal[2]
    mode: Literal["production", "simulation", "invalid"]
    rootfs: str
    authoritative: Literal[False]
    performs_writes: Literal[False]
    verifies_signatures: Literal[False]
    readiness_authority: Literal[False]
    disposition: Literal[
        "matches-expectations-non-authoritative", "blocked-non-authoritative"
    ]
    manifest_digest: str | None
    expected_roles: tuple[str, ...]
    observed_files: tuple[ObservedFile, ...]
    blockers: tuple[InstallationBlocker, ...]

    @property
    def all_files_match_expectations(self) -> bool:
        return not self.blockers and len(self.observed_files) == len(self.expected_roles)


class _DuplicateKey(ValueError):
    pass


class _PathFailure(Exception):
    def __init__(self, code: BlockerCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value)


def _reject(code: str, path: str) -> None:
    raise InstallationModelError(code, path)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _strict_copy_once(value: object, path: str, *, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        _reject("json-depth-exceeded", path)
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if _has_surrogate(value):
            _reject("json-surrogate-rejected", path)
        return value
    if type(value) is list:
        try:
            snapshot = tuple(value)
        except RuntimeError:
            _reject("manifest-mutated-during-snapshot", path)
        return [
            _strict_copy_once(item, f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(snapshot)
        ]
    if type(value) is dict:
        try:
            items = tuple(value.items())
        except RuntimeError:
            _reject("manifest-mutated-during-snapshot", path)
        result: dict[str, object] = {}
        for key, item in items:
            if type(key) is not str or _has_surrogate(key):
                _reject("json-key-invalid", path)
            result[key] = _strict_copy_once(
                item, f"{path}.{key}", depth=depth + 1
            )
        return result
    _reject("json-type-invalid", path)


def _exact_json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _exact_json_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _exact_json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _snapshot_document(value: object) -> object:
    first = _strict_copy_once(value, "manifest")
    second = _strict_copy_once(value, "manifest")
    if not _exact_json_equal(first, second):
        _reject("manifest-mutated-during-snapshot", "manifest")
    return second


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def decode_manifest_bytes(raw: object) -> object:
    if type(raw) is not bytes or not raw or len(raw) > MAX_MANIFEST_BYTES:
        _reject("manifest-bytes-invalid", "manifest")
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError, RecursionError):
        _reject("manifest-json-invalid", "manifest")


def _closed_object(value: object, keys: frozenset[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _reject("closed-schema-mismatch", path)
    return value


def _exact_string(value: object, path: str, *, maximum: int = MAX_PATH_BYTES) -> str:
    if type(value) is not str or not value:
        _reject("string-invalid", path)
    if _has_surrogate(value) or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        _reject("string-invalid", path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _reject("string-invalid", path)
    if len(encoded) > maximum:
        _reject("string-invalid", path)
    return value


def _exact_int(value: object, path: str, *, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        _reject("integer-invalid", path)
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def digest_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def canonical_manifest_bytes(value: object) -> bytes:
    """Return the one canonical encoding of a valid installation manifest.

    Package tooling can use this public boundary without depending on the
    module's private JSON encoder.  Validation and immutable snapshotting are
    performed before any bytes are returned.
    """

    return _canonical_bytes(parse_manifest(value).document())


def parse_manifest(value: object) -> InstallationManifest:
    decoded = decode_manifest_bytes(value) if type(value) is bytes else value
    document = _closed_object(
        _snapshot_document(decoded), _MANIFEST_KEYS, "manifest"
    )
    if document["schema"] != SCHEMA or type(document["schema"]) is not str:
        _reject("schema-invalid", "manifest.schema")
    if type(document["version"]) is not int or document["version"] != VERSION:
        _reject("version-invalid", "manifest.version")
    roles = document["roles"]
    if type(roles) is not list or len(roles) != len(ROLE_CONTRACTS):
        _reject("role-set-invalid", "manifest.roles")

    parsed: list[FileExpectation] = []
    for index, contract in enumerate(ROLE_CONTRACTS):
        path = f"manifest.roles[{index}]"
        entry = _closed_object(roles[index], _ROLE_KEYS, path)
        role_name = _exact_string(entry["role"], f"{path}.role", maximum=128)
        if not _ROLE_RE.fullmatch(role_name) or role_name != contract.role:
            _reject("role-set-invalid", f"{path}.role")
        role_path = _exact_string(entry["path"], f"{path}.path")
        if (
            role_path != contract.path
            or not role_path.startswith("/")
            or os.path.normpath(role_path) != role_path
            or any(part in {"", ".", ".."} for part in role_path[1:].split("/"))
        ):
            _reject("role-path-invalid", f"{path}.path")
        digest = _exact_string(entry["sha256"], f"{path}.sha256", maximum=71)
        if not _DIGEST_RE.fullmatch(digest):
            _reject("digest-invalid", f"{path}.sha256")
        size = _exact_int(entry["size"], f"{path}.size", maximum=MAX_FILE_BYTES)
        if size == 0:
            _reject("size-invalid", f"{path}.size")
        mode = _exact_int(entry["mode"], f"{path}.mode", maximum=0o7777)
        uid = _exact_int(entry["uid"], f"{path}.uid", maximum=(1 << 31) - 1)
        gid = _exact_int(entry["gid"], f"{path}.gid", maximum=(1 << 31) - 1)
        if mode != contract.mode:
            _reject("role-mode-invalid", f"{path}.mode")
        parsed.append(FileExpectation(role_name, role_path, digest, size, mode, uid, gid))
    return InstallationManifest(SCHEMA, VERSION, tuple(parsed))


def _production_manifest_group_blockers(
    manifest: InstallationManifest,
) -> tuple[InstallationBlocker, ...]:
    """Require one dedicated, non-root service group for private config/trust."""

    service_roles = tuple(
        role for role in manifest.roles if role.role in SERVICE_GROUP_ROLES
    )
    if len(service_roles) != len(SERVICE_GROUP_ROLES):
        return (
            InstallationBlocker(
                BlockerCode.MANIFEST_INVALID,
                None,
                None,
                "service-group-role-set",
            ),
        )
    service_gids = {role.gid for role in service_roles}
    if len(service_gids) == 1 and 0 not in service_gids:
        return ()
    return tuple(
        InstallationBlocker(
            BlockerCode.GID_MISMATCH,
            role.role,
            role.path,
            "production-service-group",
        )
        for role in service_roles
    )


def _safe_result_text(value: object) -> str:
    if (
        type(value) is not str
        or _has_surrogate(value)
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        return ""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if len(encoded) > MAX_PATH_BYTES:
        return ""
    return value


def _result(
    *,
    mode: Literal["production", "simulation", "invalid"],
    rootfs: str,
    manifest_digest: str | None,
    observed: tuple[ObservedFile, ...] = (),
    blockers: tuple[InstallationBlocker, ...] = (),
) -> InstallationAuditResult:
    matches = not blockers and len(observed) == len(ROLE_NAMES)
    return InstallationAuditResult(
        schema=RESULT_SCHEMA,
        version=VERSION,
        mode=mode,
        rootfs=rootfs,
        authoritative=False,
        performs_writes=False,
        verifies_signatures=False,
        readiness_authority=False,
        disposition=(
            "matches-expectations-non-authoritative"
            if matches
            else "blocked-non-authoritative"
        ),
        manifest_digest=manifest_digest,
        expected_roles=ROLE_NAMES,
        observed_files=observed,
        blockers=blockers,
    )


def _validate_target(mode: object, rootfs: object) -> tuple[str, str]:
    if type(mode) is not str or mode not in {"production", "simulation"}:
        _reject("audit-mode-invalid", "mode")
    root = _exact_string(rootfs, "rootfs")
    if not root.startswith("/") or os.path.normpath(root) != root:
        _reject("rootfs-path-invalid", "rootfs")
    if mode == "production" and root != "/":
        _reject("production-rootfs-must-be-root", "rootfs")
    if mode == "simulation" and root == "/":
        _reject("simulation-rootfs-must-be-isolated", "rootfs")
    return mode, root


def _open_flags(*, directory: bool, nonblock: bool = False) -> int:
    required = ("O_CLOEXEC", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required) or (
        directory and not hasattr(os, "O_DIRECTORY")
    ):
        raise _PathFailure(BlockerCode.PLATFORM_UNSUPPORTED, "open-flags")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    if nonblock and hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _map_open_error(error: OSError, *, rootfs: bool = False) -> _PathFailure:
    if error.errno == errno.ENOENT:
        return _PathFailure(
            BlockerCode.ROOTFS_OPEN_FAILED if rootfs else BlockerCode.PATH_MISSING,
            "missing",
        )
    if error.errno == errno.ELOOP:
        return _PathFailure(
            BlockerCode.ROOTFS_SYMLINK_REJECTED
            if rootfs
            else BlockerCode.PATH_SYMLINK_REJECTED,
            "symlink",
        )
    if error.errno == errno.ENOTDIR:
        return _PathFailure(
            BlockerCode.ROOTFS_NOT_DIRECTORY
            if rootfs
            else BlockerCode.ANCESTOR_NOT_DIRECTORY,
            "not-directory",
        )
    return _PathFailure(
        BlockerCode.ROOTFS_OPEN_FAILED if rootfs else BlockerCode.PATH_OPEN_FAILED,
        errno.errorcode.get(error.errno or 0, "OSERROR").lower(),
    )


def _open_isolated_root(rootfs: str) -> int:
    try:
        current = os.open("/", _open_flags(directory=True))
    except OSError as error:
        raise _map_open_error(error, rootfs=True) from None
    if rootfs == "/":
        return current
    try:
        for component in rootfs[1:].split("/"):
            try:
                metadata = os.stat(component, dir_fd=current, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise _PathFailure(
                        BlockerCode.ROOTFS_SYMLINK_REJECTED, "rootfs-ancestor"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise _PathFailure(
                        BlockerCode.ROOTFS_NOT_DIRECTORY, "rootfs-ancestor"
                    )
                following = os.open(
                    component, _open_flags(directory=True), dir_fd=current
                )
            except OSError as error:
                raise _map_open_error(error, rootfs=True) from None
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_role(
    root_fd: int, expectation: FileExpectation, *, production: bool
) -> tuple[int, int]:
    try:
        current = os.dup(root_fd)
    except OSError as error:
        raise _map_open_error(error) from None
    components = expectation.path[1:].split("/")
    try:
        for component in components[:-1]:
            try:
                metadata = os.stat(component, dir_fd=current, follow_symlinks=False)
            except OSError as error:
                raise _map_open_error(error) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise _PathFailure(BlockerCode.PATH_SYMLINK_REJECTED, "ancestor")
            if not stat.S_ISDIR(metadata.st_mode):
                raise _PathFailure(BlockerCode.ANCESTOR_NOT_DIRECTORY, "ancestor")
            if production and metadata.st_uid != 0:
                raise _PathFailure(BlockerCode.ANCESTOR_OWNER_MISMATCH, "uid")
            if production and stat.S_IMODE(metadata.st_mode) & 0o022:
                raise _PathFailure(BlockerCode.ANCESTOR_MODE_UNSAFE, "writable")
            try:
                following = os.open(
                    component, _open_flags(directory=True), dir_fd=current
                )
            except OSError as error:
                raise _map_open_error(error) from None
            os.close(current)
            current = following

        final_name = components[-1]
        try:
            metadata = os.stat(final_name, dir_fd=current, follow_symlinks=False)
        except OSError as error:
            raise _map_open_error(error) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _PathFailure(BlockerCode.PATH_SYMLINK_REJECTED, "final")
        # Reject special files before open so FIFOs, sockets, and device nodes can
        # never block the auditor or trigger device-specific open side effects.
        if not stat.S_ISREG(metadata.st_mode):
            raise _PathFailure(BlockerCode.NOT_REGULAR_FILE, "final-type")
        if metadata.st_nlink != 1:
            raise _PathFailure(BlockerCode.HARDLINK_REJECTED, "link-count")
        try:
            final_fd = os.open(
                final_name,
                _open_flags(directory=False, nonblock=True),
                dir_fd=current,
            )
        except OSError as error:
            raise _map_open_error(error) from None
        return current, final_fd
    except BaseException:
        os.close(current)
        raise


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _block(
    code: BlockerCode, expectation: FileExpectation, detail: str
) -> InstallationBlocker:
    return InstallationBlocker(code, expectation.role, expectation.path, detail)


def _audit_file(
    root_fd: int, expectation: FileExpectation, *, production: bool
) -> tuple[ObservedFile | None, InstallationBlocker | None]:
    contract = ROLE_BY_NAME[expectation.role]
    if production and (
        expectation.uid != contract.production_uid
        or (
            contract.production_gid is not None
            and expectation.gid != contract.production_gid
        )
    ):
        return None, _block(
            BlockerCode.UID_MISMATCH
            if expectation.uid != contract.production_uid
            else BlockerCode.GID_MISMATCH,
            expectation,
            "production-expectation",
        )
    try:
        parent_fd, file_fd = _open_role(root_fd, expectation, production=production)
    except _PathFailure as failure:
        return None, _block(failure.code, expectation, failure.detail)

    try:
        try:
            before = os.fstat(file_fd)
        except OSError:
            return None, _block(BlockerCode.READ_FAILED, expectation, "fstat")
        if not stat.S_ISREG(before.st_mode):
            return None, _block(BlockerCode.NOT_REGULAR_FILE, expectation, "type")
        if before.st_nlink != 1:
            return None, _block(BlockerCode.HARDLINK_REJECTED, expectation, "link-count")
        if stat.S_IMODE(before.st_mode) != expectation.mode:
            return None, _block(BlockerCode.MODE_MISMATCH, expectation, "mode")
        if before.st_uid != expectation.uid:
            return None, _block(BlockerCode.UID_MISMATCH, expectation, "uid")
        if before.st_gid != expectation.gid:
            return None, _block(BlockerCode.GID_MISMATCH, expectation, "gid")
        if before.st_size > MAX_FILE_BYTES:
            return None, _block(BlockerCode.FILE_TOO_LARGE, expectation, "size-bound")
        if before.st_size != expectation.size:
            return None, _block(BlockerCode.SIZE_MISMATCH, expectation, "size")

        digest = hashlib.sha256()
        total = 0
        try:
            os.lseek(file_fd, 0, os.SEEK_SET)
            while total <= MAX_FILE_BYTES:
                chunk = os.read(file_fd, min(65_536, MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        except OSError:
            return None, _block(BlockerCode.READ_FAILED, expectation, "read")
        if total > MAX_FILE_BYTES:
            return None, _block(BlockerCode.FILE_TOO_LARGE, expectation, "read-bound")
        try:
            after = os.fstat(file_fd)
        except OSError:
            return None, _block(BlockerCode.READ_FAILED, expectation, "post-fstat")
        if _stat_identity(before) != _stat_identity(after):
            return None, _block(
                BlockerCode.CONCURRENT_MUTATION, expectation, "metadata-changed"
            )
        if total != expectation.size:
            return None, _block(BlockerCode.SIZE_MISMATCH, expectation, "read-size")
        observed_digest = "sha256:" + digest.hexdigest()
        if observed_digest != expectation.sha256:
            return None, _block(BlockerCode.DIGEST_MISMATCH, expectation, "sha256")

        # Retraverse from the pinned root rather than reopening relative to the
        # old parent descriptor.  Otherwise an ancestor can be renamed and
        # replaced with byte-identical files while this audit silently keeps
        # validating the detached directory tree.
        try:
            verify_parent_fd, verify_fd = _open_role(
                root_fd, expectation, production=production
            )
        except _PathFailure:
            return None, _block(
                BlockerCode.CONCURRENT_MUTATION,
                expectation,
                "path-revalidation-failed",
            )
        try:
            try:
                current = os.fstat(verify_fd)
            finally:
                os.close(verify_fd)
                os.close(verify_parent_fd)
        except OSError:
            return None, _block(
                BlockerCode.CONCURRENT_MUTATION,
                expectation,
                "path-revalidation-failed",
            )
        if _stat_identity(before) != _stat_identity(current):
            return None, _block(
                BlockerCode.CONCURRENT_MUTATION, expectation, "path-changed"
            )
        return (
            ObservedFile(
                expectation.role,
                expectation.path,
                observed_digest,
                total,
                stat.S_IMODE(before.st_mode),
                before.st_uid,
                before.st_gid,
                before.st_dev,
                before.st_ino,
                before.st_nlink,
            ),
            None,
        )
    finally:
        os.close(file_fd)
        os.close(parent_fd)


def audit_installation(
    manifest_document: object,
    *,
    mode: object,
    rootfs: object,
) -> InstallationAuditResult:
    """Audit one fixed layout without writes or authority claims.

    Malformed expectations and filesystem failures are returned as typed blockers.
    Simulation results are always explicitly non-authoritative, even on a match.
    """

    safe_root = _safe_result_text(rootfs)
    safe_mode: Literal["production", "simulation", "invalid"] = "invalid"
    try:
        validated_mode, validated_root = _validate_target(mode, rootfs)
        safe_mode = validated_mode  # type: ignore[assignment]
        safe_root = validated_root
        manifest = parse_manifest(manifest_document)
        manifest_digest = digest_json(manifest.document())
    except InstallationModelError as error:
        return _result(
            mode=safe_mode,
            rootfs=safe_root,
            manifest_digest=None,
            blockers=(
                InstallationBlocker(
                    BlockerCode.MANIFEST_INVALID
                    if error.path.startswith("manifest")
                    else BlockerCode.AUDIT_TARGET_INVALID,
                    None,
                    None,
                    error.code,
                ),
            ),
        )

    if validated_mode == "production":
        group_blockers = _production_manifest_group_blockers(manifest)
        if group_blockers:
            return _result(
                mode=safe_mode,
                rootfs=safe_root,
                manifest_digest=manifest_digest,
                blockers=group_blockers,
            )

    try:
        root_fd = _open_isolated_root(validated_root)
    except _PathFailure as failure:
        return _result(
            mode=safe_mode,
            rootfs=safe_root,
            manifest_digest=manifest_digest,
            blockers=(InstallationBlocker(failure.code, None, None, failure.detail),),
        )

    try:
        root_identity = os.fstat(root_fd)
    except OSError:
        os.close(root_fd)
        return _result(
            mode=safe_mode,
            rootfs=safe_root,
            manifest_digest=manifest_digest,
            blockers=(
                InstallationBlocker(
                    BlockerCode.ROOTFS_OPEN_FAILED,
                    None,
                    safe_root,
                    "rootfs-fstat",
                ),
            ),
        )

    observed: list[ObservedFile] = []
    blockers: list[InstallationBlocker] = []
    try:
        for expectation in manifest.roles:
            observation, blocker = _audit_file(
                root_fd,
                expectation,
                production=validated_mode == "production",
            )
            if observation is not None:
                observed.append(observation)
            if blocker is not None:
                blockers.append(blocker)

        # The root descriptor can remain valid after the caller-visible rootfs
        # pathname has been renamed and replaced.  Reopen the pathname through
        # the same O_NOFOLLOW traversal and require the same directory inode.
        try:
            current_root_fd = _open_isolated_root(validated_root)
            try:
                current_root_identity = os.fstat(current_root_fd)
            finally:
                os.close(current_root_fd)
        except (_PathFailure, OSError):
            blockers.append(
                InstallationBlocker(
                    BlockerCode.CONCURRENT_MUTATION,
                    None,
                    safe_root,
                    "rootfs-path-revalidation-failed",
                )
            )
        else:
            if (root_identity.st_dev, root_identity.st_ino) != (
                current_root_identity.st_dev,
                current_root_identity.st_ino,
            ):
                blockers.append(
                    InstallationBlocker(
                        BlockerCode.CONCURRENT_MUTATION,
                        None,
                        safe_root,
                        "rootfs-path-changed",
                    )
                )
    finally:
        os.close(root_fd)
    return _result(
        mode=safe_mode,
        rootfs=safe_root,
        manifest_digest=manifest_digest,
        observed=tuple(observed),
        blockers=tuple(blockers),
    )


def describe_contract() -> dict[str, Any]:
    """Return the closed boundary without implying installation readiness."""

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "authoritative": False,
        "performs_writes": False,
        "installs_or_repairs": False,
        "verifies_signatures": False,
        "readiness_authority": False,
        "modes": ["production", "simulation"],
        "simulation_label": "isolated-rootfs-non-authoritative",
        "production_rootfs": "/",
        "production_private_group": "one-consistent-nonroot-gid",
        "private_service_group_roles": sorted(SERVICE_GROUP_ROLES),
        "traversal": "descriptor-relative-o-nofollow",
        "accepted_final_type": "single-link-regular-file",
        "hashing": "bounded-exact-byte-sha256",
        "maximum_manifest_bytes": MAX_MANIFEST_BYTES,
        "maximum_file_bytes": MAX_FILE_BYTES,
        "roles": [
            {
                "role": contract.role,
                "path": contract.path,
                "mode": contract.mode,
                "production_uid": contract.production_uid,
                "production_gid": contract.production_gid,
            }
            for contract in ROLE_CONTRACTS
        ],
    }


def main() -> int:
    print(json.dumps(describe_contract(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
