#!/usr/bin/env python3
"""Authenticate an exact PropertyQuarry 19-role payload for local Docker use.

The existing payload and its explicitly non-authoritative receipts remain
unchanged.  This module copies that payload beneath an outer wrapper and signs
one closed authentication document with a separately supplied Ed25519 private
key.  Verification always starts from an external public anchor; the public key
inside the payload is checked for equality but is never a bootstrap trust root.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import secrets
import stat
from typing import Callable, Final, Mapping, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

try:  # Support repo imports and direct execution from scripts/.
    from scripts import propertyquarry_release_installation_model as installation
    from scripts import propertyquarry_release_package_payload as payload_model
except ModuleNotFoundError:  # pragma: no cover - direct CLI compatibility
    import propertyquarry_release_installation_model as installation  # type: ignore[no-redef]
    import propertyquarry_release_package_payload as payload_model  # type: ignore[no-redef]


AUTHENTICATION_SCHEMA: Final = (
    "propertyquarry.release-control.local-package-authentication.v2"
)
TREE_SCHEMA: Final = "propertyquarry.release-control.payload-tree.v2"
AUTHENTICATION_DOMAIN: Final = AUTHENTICATION_SCHEMA.encode("ascii") + b"\0"
TREE_DOMAIN: Final = TREE_SCHEMA.encode("ascii") + b"\0"
SCOPE_ID: Final = "propertyquarry-local-docker"
AUTHENTICATION_FILE: Final = "authentication.v2.json"
SIGNATURE_FILE: Final = "authentication.v2.sig"
PAYLOAD_DIRECTORY: Final = "payload"
MAX_KEY_BYTES: Final = 4096
MAX_AUTHENTICATION_BYTES: Final = 256 * 1024
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

_AUTHENTICATION_KEYS: Final = frozenset(
    {"schema", "version", "signature_profile", "authority_scope", "payload"}
)
_SIGNATURE_PROFILE_KEYS: Final = frozenset(
    {"algorithm", "encoding", "key_id", "signed_message"}
)
_AUTHORITY_SCOPE_KEYS: Final = frozenset(
    {
        "kind",
        "scope_id",
        "authoritative_for_package_authentication",
        "external_production_authority",
        "public_launch_authority",
        "performs_release_effects",
    }
)
_PAYLOAD_RECORD_KEYS: Final = frozenset(
    {
        "tree_digest",
        "file_count",
        "directory_count",
        "role_count",
        "installation_manifest_sha256",
        "package_payload_receipt_sha256",
        "native_build_receipt_sha256",
    }
)


class AuthenticatedPackageError(ValueError):
    """A secret-free deterministic wrapper rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise AuthenticatedPackageError(code)


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


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _framed(domain: bytes, raw: bytes) -> bytes:
    return domain + len(raw).to_bytes(8, "big", signed=False) + raw


def _absolute_path(value: object, code: str) -> str:
    try:
        return payload_model._path_text(value, code)
    except payload_model.PayloadError as error:
        raise AuthenticatedPackageError(error.code) from None


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _strict_json(raw: bytes, code: str) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_AUTHENTICATION_BYTES:
        _fail(code)
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
        _fail(code)
    if type(value) is not dict:
        _fail(code)
    try:
        canonical = _canonical_bytes(value)
    except AuthenticatedPackageError:
        _fail(code)
    if canonical != raw:
        _fail(code)
    return value


def _closed(
    value: object,
    keys: frozenset[str],
    code: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(code)
    return value


def _payload_file_specification() -> dict[str, int]:
    specification = {
        f"rootfs/{contract.path[1:]}": contract.mode
        for contract in installation.ROLE_CONTRACTS
    }
    specification["installation-manifest.v2.json"] = 0o644
    specification["package-payload-receipt.v2.json"] = 0o644
    return specification


PAYLOAD_FILES: Final = _payload_file_specification()
PAYLOAD_DIRECTORY_MODES: Final = payload_model._final_directory_modes()
PAYLOAD_SIZE_LIMITS: Final = {
    relative: (
        installation.MAX_MANIFEST_BYTES
        if relative == "installation-manifest.v2.json"
        else (
            payload_model.MAX_JSON_BYTES
            if relative == "package-payload-receipt.v2.json"
            else installation.MAX_FILE_BYTES
        )
    )
    for relative in PAYLOAD_FILES
}
WRAPPER_FILES: Final = {
    **{f"payload/{relative}": mode for relative, mode in PAYLOAD_FILES.items()},
    AUTHENTICATION_FILE: 0o644,
    SIGNATURE_FILE: 0o644,
}
WRAPPER_DIRECTORY_MODES: Final = {
    "payload": 0o700,
    **{
        f"payload/{relative}": mode
        for relative, mode in PAYLOAD_DIRECTORY_MODES.items()
    },
}
WRAPPER_SIZE_LIMITS: Final = {
    **{
        f"payload/{relative}": maximum
        for relative, maximum in PAYLOAD_SIZE_LIMITS.items()
    },
    AUTHENTICATION_FILE: MAX_AUTHENTICATION_BYTES,
    SIGNATURE_FILE: 64,
}


@dataclasses.dataclass(frozen=True, slots=True)
class _StableFile:
    path: str
    raw: bytes
    identity: tuple[int, ...]
    mode: int
    uid: int
    gid: int


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


def _stable_file(
    path: str,
    *,
    maximum: int,
    private: bool,
    code: str,
) -> _StableFile:
    validated = _absolute_path(path, f"{code}-path-invalid")
    descriptor = -1
    try:
        before = os.lstat(validated)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            _fail(f"{code}-type-invalid")
        if os.path.realpath(validated) != validated:
            _fail(f"{code}-symlink-rejected")
        mode = stat.S_IMODE(before.st_mode)
        allowed_owners = {0, os.geteuid()}
        if (
            before.st_nlink != 1
            or before.st_uid not in allowed_owners
            or before.st_size < 1
            or before.st_size > maximum
            or mode & 0o022
            or (private and (before.st_uid != os.geteuid() or mode not in {0o400, 0o600}))
        ):
            _fail(f"{code}-metadata-invalid")
        descriptor = os.open(
            validated,
            payload_model._open_flags(directory=False),
        )
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened):
            _fail(f"{code}-mutated")
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
        if (
            len(raw) != opened.st_size
            or len(raw) > maximum
            or _file_identity(opened) != _file_identity(after)
        ):
            _fail(f"{code}-mutated")
        path_after = os.lstat(validated)
        if _file_identity(after) != _file_identity(path_after):
            _fail(f"{code}-mutated")
        return _StableFile(
            validated,
            bytes(raw),
            _file_identity(after),
            mode,
            after.st_uid,
            after.st_gid,
        )
    except AuthenticatedPackageError:
        raise
    except OSError:
        _fail(f"{code}-read-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_public_anchor(snapshot: _StableFile) -> tuple[Ed25519PublicKey, str]:
    try:
        key = serialization.load_pem_public_key(snapshot.raw)
    except (TypeError, ValueError):
        _fail("external-anchor-invalid")
    if not isinstance(key, Ed25519PublicKey):
        _fail("external-anchor-invalid")
    canonical_pem = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical_pem != snapshot.raw:
        _fail("external-anchor-invalid")
    return key, _digest(public_der)


def _load_private_key(snapshot: _StableFile) -> tuple[Ed25519PrivateKey, str]:
    try:
        key = serialization.load_pem_private_key(snapshot.raw, password=None)
    except (TypeError, ValueError):
        _fail("private-key-invalid")
    if not isinstance(key, Ed25519PrivateKey):
        _fail("private-key-invalid")
    canonical_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical_pem != snapshot.raw:
        _fail("private-key-invalid")
    return key, _digest(public_der)


def _snapshot_payload(root: str) -> payload_model.TreeSnapshot:
    try:
        snapshot = payload_model._snapshot_tree(
            _absolute_path(root, "payload-path-invalid"),
            PAYLOAD_FILES,
            directory_modes=PAYLOAD_DIRECTORY_MODES,
            size_limits=PAYLOAD_SIZE_LIMITS,
        )
    except payload_model.PayloadError as error:
        raise AuthenticatedPackageError(f"payload:{error.code}") from None
    if stat.S_IMODE(snapshot.root_identity[2]) != 0o700:
        _fail("payload-root-mode-invalid")
    return snapshot


def _tree_entries(snapshot: payload_model.TreeSnapshot) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = [
        {
            "path": relative,
            "type": "directory",
            "mode": metadata[0],
        }
        for relative, metadata in snapshot.directories
    ]
    entries.extend(
        {
            "path": item.relative,
            "type": "file",
            "mode": item.mode,
            "size": item.size,
            "sha256": item.sha256,
        }
        for item in snapshot.files
    )
    return sorted(entries, key=lambda item: str(item["path"]))


def _tree_digest(snapshot: payload_model.TreeSnapshot) -> str:
    tree_document = {
        "schema": TREE_SCHEMA,
        "entries": _tree_entries(snapshot),
    }
    raw = _canonical_bytes(tree_document)
    return _digest(_framed(TREE_DOMAIN, raw))


def _payload_projection(
    snapshot: payload_model.TreeSnapshot,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """Compare payload semantics while deliberately excluding inode identity."""

    directories = tuple(
        (relative, metadata[0], metadata[1], metadata[2])
        for relative, metadata in snapshot.directories
    )
    files = tuple(
        (
            item.relative,
            item.data,
            item.sha256,
            item.size,
            item.mode,
            item.uid,
            item.gid,
        )
        for item in snapshot.files
    )
    return directories, files


def _package_contract(
    snapshot: payload_model.TreeSnapshot,
) -> tuple[installation.InstallationManifest, dict[str, object], str, str, str]:
    files = snapshot.by_relative()
    manifest_raw = files["installation-manifest.v2.json"].data
    receipt_raw = files["package-payload-receipt.v2.json"].data
    try:
        manifest = installation.parse_manifest(manifest_raw)
        canonical_manifest = installation.canonical_manifest_bytes(
            manifest.document()
        )
    except installation.InstallationModelError:
        _fail("installation-manifest-invalid")
    if canonical_manifest != manifest_raw:
        _fail("installation-manifest-not-canonical")
    if len(manifest.roles) != 19 or tuple(
        role.role for role in manifest.roles
    ) != installation.ROLE_NAMES:
        _fail("installation-manifest-role-set-invalid")
    for expected in manifest.roles:
        observed = files[f"rootfs/{expected.path[1:]}"]
        if (
            observed.sha256 != expected.sha256
            or observed.size != expected.size
            or observed.mode != expected.mode
        ):
            _fail("installation-manifest-role-binding-invalid")

    receipt = _strict_json(receipt_raw, "package-payload-receipt-invalid")
    input_integrity = receipt.get("input_integrity")
    simulation_audit = receipt.get("simulation_audit")
    manifest_digest = _digest(manifest_raw)
    valid = (
        receipt.get("schema") == payload_model.RECEIPT_SCHEMA
        and receipt.get("version") == 2
        and receipt.get("authoritative") is False
        and receipt.get("production_ready") is False
        and receipt.get("readiness_authority") is False
        and receipt.get("payload_signed") is False
        and receipt.get("installs_or_repairs") is False
        and receipt.get("root_install_performed") is False
        and receipt.get("package_signature_verified") is False
        and receipt.get("verifies_signatures") is False
        and receipt.get("input_authentication_verified") is False
        and receipt.get("role_count") == 19
        and receipt.get("receipt_published_last") is True
        and receipt.get("installation_manifest_sha256") == manifest_digest
        and type(input_integrity) is dict
        and type(simulation_audit) is dict
        and simulation_audit.get("mode") == "simulation"
        and simulation_audit.get("all_files_match_expectations") is True
        and simulation_audit.get("observed_role_count") == 19
        and simulation_audit.get("blocker_count") == 0
    )
    if not valid:
        _fail("package-payload-receipt-invalid")
    native_receipt_digest = input_integrity.get("native_build_receipt_sha256")
    if (
        type(native_receipt_digest) is not str
        or _DIGEST_RE.fullmatch(native_receipt_digest) is None
    ):
        _fail("package-payload-receipt-invalid")
    return (
        manifest,
        receipt,
        manifest_digest,
        _digest(receipt_raw),
        native_receipt_digest,
    )


def _payload_package_key_id(
    snapshot: payload_model.TreeSnapshot,
) -> str:
    relative = (
        "rootfs"
        + installation.ROLE_BY_NAME["package-trust-root"].path
    )
    raw = snapshot.by_relative()[relative].data
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, ValueError):
        _fail("payload-package-anchor-invalid")
    if not isinstance(key, Ed25519PublicKey):
        _fail("payload-package-anchor-invalid")
    canonical_pem = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical_pem != raw:
        _fail("payload-package-anchor-invalid")
    return _digest(public_der)


def _authentication_document(
    snapshot: payload_model.TreeSnapshot,
    *,
    key_id: str,
) -> dict[str, object]:
    (
        _manifest,
        _receipt,
        manifest_digest,
        receipt_digest,
        native_receipt_digest,
    ) = _package_contract(snapshot)
    return {
        "schema": AUTHENTICATION_SCHEMA,
        "version": 2,
        "signature_profile": {
            "algorithm": "ed25519",
            "encoding": "raw-64-byte",
            "key_id": key_id,
            "signed_message": (
                "domain-separated-uint64be-length-prefixed-canonical-json"
            ),
        },
        "authority_scope": {
            "kind": "local-docker",
            "scope_id": SCOPE_ID,
            "authoritative_for_package_authentication": True,
            "external_production_authority": False,
            "public_launch_authority": False,
            "performs_release_effects": False,
        },
        "payload": {
            "tree_digest": _tree_digest(snapshot),
            "file_count": len(snapshot.files),
            "directory_count": len(snapshot.directories),
            "role_count": len(installation.ROLE_CONTRACTS),
            "installation_manifest_sha256": manifest_digest,
            "package_payload_receipt_sha256": receipt_digest,
            "native_build_receipt_sha256": native_receipt_digest,
        },
    }


def _validate_authentication_document(
    document: object,
    *,
    payload_snapshot: payload_model.TreeSnapshot,
    external_key_id: str,
) -> dict[str, object]:
    value = _closed(document, _AUTHENTICATION_KEYS, "authentication-contract-invalid")
    signature_profile = _closed(
        value["signature_profile"],
        _SIGNATURE_PROFILE_KEYS,
        "authentication-contract-invalid",
    )
    authority_scope = _closed(
        value["authority_scope"],
        _AUTHORITY_SCOPE_KEYS,
        "authentication-contract-invalid",
    )
    payload_record = _closed(
        value["payload"],
        _PAYLOAD_RECORD_KEYS,
        "authentication-contract-invalid",
    )
    if (
        value["schema"] != AUTHENTICATION_SCHEMA
        or type(value["schema"]) is not str
        or value["version"] != 2
        or type(value["version"]) is not int
        or signature_profile
        != {
            "algorithm": "ed25519",
            "encoding": "raw-64-byte",
            "key_id": external_key_id,
            "signed_message": (
                "domain-separated-uint64be-length-prefixed-canonical-json"
            ),
        }
        or authority_scope
        != {
            "kind": "local-docker",
            "scope_id": SCOPE_ID,
            "authoritative_for_package_authentication": True,
            "external_production_authority": False,
            "public_launch_authority": False,
            "performs_release_effects": False,
        }
        or payload_record
        != _authentication_document(
            payload_snapshot,
            key_id=external_key_id,
        )["payload"]
    ):
        _fail("authentication-contract-invalid")
    return value


def _snapshot_wrapper(root: str) -> payload_model.TreeSnapshot:
    try:
        snapshot = payload_model._snapshot_tree(
            _absolute_path(root, "wrapper-path-invalid"),
            WRAPPER_FILES,
            directory_modes=WRAPPER_DIRECTORY_MODES,
            size_limits=WRAPPER_SIZE_LIMITS,
        )
    except payload_model.PayloadError as error:
        raise AuthenticatedPackageError(f"wrapper:{error.code}") from None
    if stat.S_IMODE(snapshot.root_identity[2]) != 0o700:
        _fail("wrapper-root-mode-invalid")
    return snapshot


def _snapshot_wrapper_from_fd(
    root_fd: int,
    *,
    root_label: str,
) -> payload_model.TreeSnapshot:
    """Audit an exact wrapper through a pinned directory descriptor."""

    descriptor = -1
    try:
        descriptor = os.dup(root_fd)
        root_metadata = os.fstat(descriptor)
        root_identity = payload_model._identity(root_metadata)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or root_metadata.st_uid != os.geteuid()
            or root_metadata.st_gid != os.getegid()
        ):
            _fail("wrapper-descriptor-root-invalid")
        first_files, first_directories = payload_model._enumerate_tree(
            descriptor,
            WRAPPER_FILES,
        )
        if (
            first_files != set(WRAPPER_FILES)
            or set(first_directories) != set(WRAPPER_DIRECTORY_MODES)
        ):
            _fail("wrapper-descriptor-tree-invalid")
        for relative, metadata in first_directories.items():
            if (
                metadata[0] != WRAPPER_DIRECTORY_MODES[relative]
                or metadata[1] != os.geteuid()
                or metadata[2] != os.getegid()
            ):
                _fail("wrapper-descriptor-directory-invalid")
        files = tuple(
            payload_model._snapshot_file(
                descriptor,
                relative,
                mode,
                WRAPPER_SIZE_LIMITS[relative],
            )
            for relative, mode in WRAPPER_FILES.items()
        )
        second_files, second_directories = payload_model._enumerate_tree(
            descriptor,
            WRAPPER_FILES,
        )
        if (
            second_files != first_files
            or second_directories != first_directories
            or payload_model._identity(os.fstat(descriptor)) != root_identity
        ):
            _fail("wrapper-descriptor-mutated")
        return payload_model.TreeSnapshot(
            root_label,
            root_identity,
            files,
            tuple(sorted(first_directories.items())),
        )
    except AuthenticatedPackageError:
        raise
    except payload_model.PayloadError as error:
        raise AuthenticatedPackageError(
            f"wrapper-descriptor:{error.code}"
        ) from None
    except OSError:
        _fail("wrapper-descriptor-audit-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _payload_from_wrapper_snapshot(
    wrapper_snapshot: payload_model.TreeSnapshot,
) -> payload_model.TreeSnapshot:
    directory_by_relative = dict(wrapper_snapshot.directories)
    payload_root = directory_by_relative.get(PAYLOAD_DIRECTORY)
    if payload_root is None:
        _fail("wrapper-payload-directory-missing")
    prefix = PAYLOAD_DIRECTORY + "/"
    files = tuple(
        dataclasses.replace(
            item,
            relative=item.relative.removeprefix(prefix),
        )
        for item in wrapper_snapshot.files
        if item.relative.startswith(prefix)
    )
    directories = tuple(
        (
            relative.removeprefix(prefix),
            metadata,
        )
        for relative, metadata in wrapper_snapshot.directories
        if relative.startswith(prefix)
    )
    if (
        len(files) != len(PAYLOAD_FILES)
        or len(directories) != len(PAYLOAD_DIRECTORY_MODES)
    ):
        _fail("wrapper-payload-projection-invalid")
    return payload_model.TreeSnapshot(
        os.path.join(wrapper_snapshot.root, PAYLOAD_DIRECTORY),
        payload_root[3],
        files,
        directories,
    )


def _verify_wrapper_snapshot(
    snapshot: payload_model.TreeSnapshot,
    *,
    public_key: Ed25519PublicKey,
    external_key_id: str,
    wrapper_label: str,
) -> dict[str, object]:
    wrapper_files = snapshot.by_relative()
    authentication_raw = wrapper_files[AUTHENTICATION_FILE].data
    signature = wrapper_files[SIGNATURE_FILE].data
    if len(signature) != 64:
        _fail("authentication-signature-invalid")
    payload_snapshot = _payload_from_wrapper_snapshot(snapshot)
    if _payload_package_key_id(payload_snapshot) != external_key_id:
        _fail("payload-package-anchor-mismatch")
    document = _strict_json(authentication_raw, "authentication-json-invalid")
    document = _validate_authentication_document(
        document,
        payload_snapshot=payload_snapshot,
        external_key_id=external_key_id,
    )
    try:
        public_key.verify(
            signature,
            _framed(AUTHENTICATION_DOMAIN, authentication_raw),
        )
    except InvalidSignature:
        _fail("authentication-signature-invalid")
    return {
        "wrapper": wrapper_label,
        "authentication": document,
        "authentication_sha256": _digest(authentication_raw),
        "signature_sha256": _digest(signature),
    }


def _wrapper_binding(
    snapshot: payload_model.TreeSnapshot,
) -> tuple[tuple[int, ...], tuple[object, ...], tuple[object, ...]]:
    """Bind the pinned root inode and every descendant without its rename ctime."""

    return (
        payload_model._stable_directory_identity_from_file_identity(
            snapshot.root_identity
        ),
        snapshot.files,
        snapshot.directories,
    )


def _audit_expected_pinned_wrapper(
    root_fd: int,
    *,
    root_label: str,
    public_key: Ed25519PublicKey,
    external_key_id: str,
    expected_payload: payload_model.TreeSnapshot,
    expected_authentication: bytes,
    expected_signature: bytes,
    expected_binding: tuple[
        tuple[int, ...], tuple[object, ...], tuple[object, ...]
    ]
    | None = None,
) -> tuple[
    payload_model.TreeSnapshot,
    tuple[tuple[int, ...], tuple[object, ...], tuple[object, ...]],
]:
    """Verify the exact signed wrapper through its already-pinned root fd."""

    snapshot = _snapshot_wrapper_from_fd(root_fd, root_label=root_label)
    files = snapshot.by_relative()
    payload_snapshot = _payload_from_wrapper_snapshot(snapshot)
    if (
        files[AUTHENTICATION_FILE].data != expected_authentication
        or files[SIGNATURE_FILE].data != expected_signature
        or _payload_projection(payload_snapshot)
        != _payload_projection(expected_payload)
    ):
        _fail("wrapper-pinned-binding-invalid")
    verification = _verify_wrapper_snapshot(
        snapshot,
        public_key=public_key,
        external_key_id=external_key_id,
        wrapper_label=root_label,
    )
    if (
        verification["authentication_sha256"] != _digest(expected_authentication)
        or verification["signature_sha256"] != _digest(expected_signature)
    ):
        _fail("wrapper-pinned-verification-invalid")
    binding = _wrapper_binding(snapshot)
    if expected_binding is not None and binding != expected_binding:
        _fail("wrapper-pinned-identity-mutated")
    return snapshot, binding


def _directory_object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Fields that continue to identify a directory after hostile metadata edits."""

    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _rollback_published_wrapper(
    parent_fd: int,
    *,
    output_name: str,
    pinned_fd: int,
) -> None:
    """Quarantine and delete only the exact root inode held by ``pinned_fd``."""

    try:
        pinned = os.fstat(pinned_fd)
        pinned_object = _directory_object_identity(pinned)
        current = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
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
        candidate = f".{output_name}.rollback.{secrets.token_hex(12)}"
        try:
            payload_model._rename_noreplace(parent_fd, output_name, candidate)
        except payload_model.PayloadError as error:
            if error.code == "output-exists":
                continue
            raise AuthenticatedPackageError(
                f"published-rollback:{error.code}"
            ) from None
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
            payload_model._open_flags(directory=True),
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
        cleanup_identity = payload_model._directory_identity(pinned_after)
    except AuthenticatedPackageError:
        try:
            payload_model._rename_noreplace(
                parent_fd,
                quarantine_name,
                output_name,
            )
        except payload_model.PayloadError:
            _fail("published-rollback-restore-failed")
        raise
    except OSError:
        _fail("published-rollback-revalidation-failed")
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)

    try:
        payload_model._remove_temp(
            parent_fd,
            quarantine_name,
            cleanup_identity,
        )
    except payload_model.PayloadError as error:
        raise AuthenticatedPackageError(
            f"published-rollback:{error.code}"
        ) from None
    try:
        os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
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


def verify_wrapper(*, wrapper: str, external_anchor: str) -> dict[str, object]:
    """Verify one wrapper without writes or subprocesses."""

    root = _absolute_path(wrapper, "wrapper-path-invalid")
    anchor_first = _stable_file(
        external_anchor,
        maximum=MAX_KEY_BYTES,
        private=False,
        code="external-anchor",
    )
    public_key, external_key_id = _load_public_anchor(anchor_first)
    wrapper_first = _snapshot_wrapper(root)
    result = _verify_wrapper_snapshot(
        wrapper_first,
        public_key=public_key,
        external_key_id=external_key_id,
        wrapper_label=root,
    )

    anchor_second = _stable_file(
        anchor_first.path,
        maximum=MAX_KEY_BYTES,
        private=False,
        code="external-anchor",
    )
    wrapper_second = _snapshot_wrapper(root)
    if anchor_second != anchor_first or wrapper_second != wrapper_first:
        _fail("authentication-input-mutated")
    return result


def create_authenticated_wrapper(
    *,
    payload: str,
    private_key: str,
    external_anchor: str,
    output: str,
    phase_hook: Callable[[str], None] | None = None,
) -> str:
    """Copy, bind, sign, verify, and atomically publish one wrapper."""

    payload_root = _absolute_path(payload, "payload-path-invalid")
    output_path = _absolute_path(output, "output-path-invalid")
    output_parent = os.path.dirname(output_path)
    output_name = os.path.basename(output_path)
    parent_fd = -1
    temp_fd = -1
    temp_name: str | None = None
    temp_path: str | None = None
    temp_identity: tuple[int, ...] | None = None
    published = False
    publication_complete = False

    def phase(value: str) -> None:
        if phase_hook is not None:
            phase_hook(value)

    try:
        try:
            parent_fd, _initial_identity = payload_model._open_controlled_directory(
                output_parent,
                writable=True,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        try:
            os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            _fail("output-preflight-failed")
        else:
            _fail("output-exists")

        payload_first = _snapshot_payload(payload_root)
        anchor_first = _stable_file(
            external_anchor,
            maximum=MAX_KEY_BYTES,
            private=False,
            code="external-anchor",
        )
        public_key, external_key_id = _load_public_anchor(anchor_first)
        private_first = _stable_file(
            private_key,
            maximum=MAX_KEY_BYTES,
            private=True,
            code="private-key",
        )
        signer, private_key_id = _load_private_key(private_first)
        if private_key_id != external_key_id:
            _fail("private-key-anchor-mismatch")
        if _payload_package_key_id(payload_first) != external_key_id:
            _fail("payload-package-anchor-mismatch")
        phase("inputs-snapshotted")

        document = _authentication_document(
            payload_first,
            key_id=external_key_id,
        )
        authentication_raw = _canonical_bytes(document)
        signature = signer.sign(
            _framed(AUTHENTICATION_DOMAIN, authentication_raw)
        )
        if len(signature) != 64:
            _fail("authentication-signature-invalid")
        try:
            public_key.verify(
                signature,
                _framed(AUTHENTICATION_DOMAIN, authentication_raw),
            )
        except InvalidSignature:
            _fail("authentication-signature-invalid")

        try:
            temp_name, temp_path, temp_fd = payload_model._make_private_temp(
                parent_fd,
                output_parent,
                f"{output_name}.assembling",
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        parent_identity = payload_model._parent_path_identity(
            os.fstat(parent_fd)
        )
        temp_identity = payload_model._directory_identity(os.fstat(temp_fd))
        payload_fd = -1
        rootfs_fd = -1
        try:
            os.mkdir(PAYLOAD_DIRECTORY, 0o700, dir_fd=temp_fd)
            payload_fd = os.open(
                PAYLOAD_DIRECTORY,
                payload_model._open_flags(directory=True),
                dir_fd=temp_fd,
            )
            os.fchmod(payload_fd, 0o700)
            metadata = os.fstat(payload_fd)
            if (
                stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
            ):
                _fail("wrapper-payload-directory-invalid")
            os.mkdir("rootfs", 0o755, dir_fd=payload_fd)
            rootfs_fd = os.open(
                "rootfs",
                payload_model._open_flags(directory=True),
                dir_fd=payload_fd,
            )
            os.fchmod(rootfs_fd, 0o755)
            rootfs_metadata = os.fstat(rootfs_fd)
            if (
                stat.S_IMODE(rootfs_metadata.st_mode) != 0o755
                or rootfs_metadata.st_uid != os.geteuid()
                or rootfs_metadata.st_gid != os.getegid()
            ):
                _fail("wrapper-rootfs-directory-invalid")
            for item in payload_first.files:
                if item.relative.startswith("rootfs/"):
                    payload_model._write_file_at(
                        rootfs_fd,
                        item.relative.removeprefix("rootfs/"),
                        item.data,
                        item.mode,
                    )
                else:
                    payload_model._write_file_at(
                        payload_fd,
                        item.relative,
                        item.data,
                        item.mode,
                    )
            os.fsync(rootfs_fd)
            os.fsync(payload_fd)
        except AuthenticatedPackageError:
            raise
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        except OSError:
            _fail("wrapper-payload-copy-failed")
        finally:
            if rootfs_fd >= 0:
                os.close(rootfs_fd)
            if payload_fd >= 0:
                os.close(payload_fd)
        phase("payload-copied")

        try:
            payload_model._write_file_at(
                temp_fd,
                AUTHENTICATION_FILE,
                authentication_raw,
                0o644,
            )
            payload_model._write_file_at(
                temp_fd,
                SIGNATURE_FILE,
                signature,
                0o644,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        phase("authentication-written")

        if temp_path is None or temp_name is None or temp_identity is None:
            _fail("temporary-state-invalid")
        temp_identity = payload_model._directory_identity(os.fstat(temp_fd))
        _initial_wrapper, expected_wrapper_binding = (
            _audit_expected_pinned_wrapper(
                temp_fd,
                root_label=temp_path,
                public_key=public_key,
                external_key_id=external_key_id,
                expected_payload=payload_first,
                expected_authentication=authentication_raw,
                expected_signature=signature,
            )
        )
        if _snapshot_payload(payload_root) != payload_first:
            _fail("payload-input-mutated")
        if (
            _stable_file(
                anchor_first.path,
                maximum=MAX_KEY_BYTES,
                private=False,
                code="external-anchor",
            )
            != anchor_first
            or _stable_file(
                private_first.path,
                maximum=MAX_KEY_BYTES,
                private=True,
                code="private-key",
            )
            != private_first
        ):
            _fail("signing-input-mutated")
        try:
            payload_model._revalidate_parent_path(
                output_parent,
                parent_fd,
                parent_identity,
            )
            payload_model._revalidate_temp_root(
                parent_fd,
                temp_name,
                temp_fd,
                temp_identity,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        phase("before-publish")
        if _snapshot_payload(payload_root) != payload_first:
            _fail("payload-input-mutated")
        if (
            _stable_file(
                anchor_first.path,
                maximum=MAX_KEY_BYTES,
                private=False,
                code="external-anchor",
            )
            != anchor_first
            or _stable_file(
                private_first.path,
                maximum=MAX_KEY_BYTES,
                private=True,
                code="private-key",
            )
            != private_first
        ):
            _fail("signing-input-mutated")
        try:
            payload_model._revalidate_parent_path(
                output_parent,
                parent_fd,
                parent_identity,
            )
            payload_model._revalidate_temp_root(
                parent_fd,
                temp_name,
                temp_fd,
                temp_identity,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        _final_staged_wrapper, final_staged_binding = (
            _audit_expected_pinned_wrapper(
                temp_fd,
                root_label=temp_path,
                public_key=public_key,
                external_key_id=external_key_id,
                expected_payload=payload_first,
                expected_authentication=authentication_raw,
                expected_signature=signature,
                expected_binding=expected_wrapper_binding,
            )
        )
        if final_staged_binding != expected_wrapper_binding:
            _fail("wrapper-pinned-identity-mutated")
        try:
            payload_model._revalidate_parent_path(
                output_parent,
                parent_fd,
                parent_identity,
            )
            payload_model._revalidate_temp_root(
                parent_fd,
                temp_name,
                temp_fd,
                temp_identity,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        try:
            payload_model._rename_noreplace(parent_fd, temp_name, output_name)
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        published = True
        try:
            payload_model._revalidate_temp_root(
                parent_fd,
                output_name,
                temp_fd,
                temp_identity,
            )
            payload_model._revalidate_parent_path(
                output_parent,
                parent_fd,
                parent_identity,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        try:
            os.fsync(parent_fd)
        except OSError:
            _fail("output-parent-durability-unknown")
        _published_wrapper, published_binding = _audit_expected_pinned_wrapper(
            temp_fd,
            root_label=temp_path,
            public_key=public_key,
            external_key_id=external_key_id,
            expected_payload=payload_first,
            expected_authentication=authentication_raw,
            expected_signature=signature,
            expected_binding=expected_wrapper_binding,
        )
        if published_binding != expected_wrapper_binding:
            _fail("wrapper-pinned-identity-mutated")
        try:
            payload_model._revalidate_temp_root(
                parent_fd,
                output_name,
                temp_fd,
                temp_identity,
            )
            payload_model._revalidate_parent_path(
                output_parent,
                parent_fd,
                parent_identity,
            )
        except payload_model.PayloadError as error:
            raise AuthenticatedPackageError(error.code) from None
        publication_complete = True
        os.close(temp_fd)
        temp_fd = -1
        return output_path
    finally:
        cleanup_error: AuthenticatedPackageError | None = None
        try:
            if temp_fd >= 0:
                if published and not publication_complete:
                    _rollback_published_wrapper(
                        parent_fd,
                        output_name=output_name,
                        pinned_fd=temp_fd,
                    )
                elif not published and temp_name is not None:
                    try:
                        cleanup_identity = payload_model._directory_identity(
                            os.fstat(temp_fd)
                        )
                    except OSError:
                        _fail("temporary-cleanup-identity-missing")
                    try:
                        payload_model._remove_temp(
                            parent_fd,
                            temp_name,
                            cleanup_identity,
                        )
                    except payload_model.PayloadError as error:
                        raise AuthenticatedPackageError(error.code) from None
        except AuthenticatedPackageError as error:
            cleanup_error = error
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
        if cleanup_error is not None:
            raise cleanup_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sign or verify a local authenticated PropertyQuarry package."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sign = subparsers.add_parser("sign")
    sign.add_argument("--payload", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--external-anchor", required=True)
    sign.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--wrapper", required=True)
    verify.add_argument("--external-anchor", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "sign":
            wrapper = create_authenticated_wrapper(
                payload=arguments.payload,
                private_key=arguments.private_key,
                external_anchor=arguments.external_anchor,
                output=arguments.output,
            )
            result = verify_wrapper(
                wrapper=wrapper,
                external_anchor=arguments.external_anchor,
            )
        else:
            result = verify_wrapper(
                wrapper=arguments.wrapper,
                external_anchor=arguments.external_anchor,
            )
    except AuthenticatedPackageError as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    print(_canonical_bytes(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
