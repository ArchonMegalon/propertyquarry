#!/usr/bin/env python3
"""Materialize an authenticated PropertyQuarry payload as a local OCI image.

This lane is deliberately daemonless and has no network implementation.  It
creates a deterministic scratch image, a Docker-load archive, and an audit
receipt from the exact locally authenticated 19-role package wrapper.  Loading
or running that image is a separate, explicit operation.
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Final, Mapping

try:  # Support module and direct-script execution.
    from scripts import propertyquarry_release_installation_model as installation
    from scripts import propertyquarry_release_package_payload as package_payload
except ModuleNotFoundError:  # pragma: no cover - direct CLI coverage
    import propertyquarry_release_installation_model as installation
    import propertyquarry_release_package_payload as package_payload

try:
    from scripts import propertyquarry_release_authenticated_package as authenticated
except ModuleNotFoundError:  # pragma: no cover - fails closed at the integration edge
    try:
        import propertyquarry_release_authenticated_package as authenticated
    except ModuleNotFoundError:
        authenticated = None  # type: ignore[assignment]


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE_PATH: Final = REPOSITORY_ROOT / "compose.propertyquarry-release-control-v2.yml"
AUTHENTICATION_SCHEMA: Final = (
    "propertyquarry.release-control.local-package-authentication.v2"
)
PAYLOAD_TREE_SCHEMA: Final = "propertyquarry.release-control.payload-tree.v2"
RECEIPT_SCHEMA: Final = "propertyquarry.release-control.local-oci-receipt.v2"
OCI_LAYOUT_VERSION: Final = "1.0.0"
OCI_CONFIG_MEDIA_TYPE: Final = "application/vnd.oci.image.config.v1+json"
OCI_MANIFEST_MEDIA_TYPE: Final = "application/vnd.oci.image.manifest.v1+json"
OCI_LAYER_MEDIA_TYPE: Final = "application/vnd.oci.image.layer.v1.tar"
MAX_JSON_BYTES: Final = 256 * 1024
MAX_LAYER_BYTES: Final = 512 * 1024 * 1024
MAX_OUTPUT_BYTES: Final = 1024 * 1024 * 1024
RENAME_NOREPLACE: Final = 1
AUTHORITY_UID: Final = 65532
CALLER_UID: Final = 65534
LOCAL_AUTHORITY_PREFIX: Final = (
    "usr/share/propertyquarry-release-control-v2/local-authority"
)
RUNTIME_SOCKET_DIRECTORY: Final = "run/propertyquarry-release-control-v2"
RUNTIME_STATE_DIRECTORY: Final = "var/lib/propertyquarry-release-control-v2"
EXTERNAL_ANCHOR_MOUNT_TARGET: Final = (
    "run/secrets/propertyquarry-package-authority-v2.pem"
)
PRIVATE_KEY_MARKERS: Final = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "version",
        "authoritative_for_local_image_materialization",
        "external_production_authority",
        "public_launch_authority",
        "performs_release_effects",
        "loads_or_runs_docker",
        "network_implementation_present",
        "scratch_image",
        "scratch_execution_contract_verified",
        "architecture",
        "os",
        "role_count",
        "retained_authenticated_payload_file_count",
        "independent_active_role_audit",
        "projected_numeric_ownership_verified",
        "private_key_material_rejected",
        "authentication_sha256",
        "authentication_signature_sha256",
        "authenticated_payload_tree_digest",
        "installation_manifest_sha256",
        "package_payload_receipt_sha256",
        "native_build_receipt_sha256",
        "layer_digest",
        "image_id",
        "image_digest",
        "index_sha256",
        "docker_archive_sha256",
        "docker_archive_tag",
        "compose_sha256",
        "runtime_user",
        "authority_user",
        "caller_user",
    }
)


class MaterializationError(ValueError):
    """A deterministic, path- and secret-free materialization failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class LayerRecord:
    path: str
    mode: int
    uid: int
    gid: int
    data: bytes | None

    @property
    def is_directory(self) -> bool:
        return self.data is None


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedTarRecord:
    path: str
    mode: int
    uid: int
    gid: int
    data: bytes | None


@dataclasses.dataclass(frozen=True, slots=True)
class MaterializedImage:
    output: str
    image_id: str
    image_digest: str
    receipt_sha256: str
    docker_archive_sha256: str


@dataclasses.dataclass(frozen=True, slots=True)
class OciTreeSnapshot:
    layout: bytes
    index: bytes
    installation_manifest: bytes
    materialization_receipt: bytes
    compose: bytes
    docker_archive: bytes
    blobs: Mapping[str, bytes]
    identities: tuple[tuple[str, tuple[int, int]], ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class AuditedOciTree:
    receipt: Mapping[str, object]
    snapshot: OciTreeSnapshot


def _fail(code: str) -> None:
    raise MaterializationError(code)


def _canonical(value: object) -> bytes:
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


def _digest_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_canonical_json(data: bytes, code: str) -> object:
    if not data or len(data) > MAX_JSON_BYTES:
        _fail(code)
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if _canonical(value) != data:
        _fail(code)
    return value


def _require_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _read_regular(path: Path, *, size_limit: int, mode: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("authenticated-wrapper-file-open-failed")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > size_limit
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        ):
            _fail("authenticated-wrapper-file-invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                _fail("authenticated-wrapper-file-short-read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("authenticated-wrapper-file-grew")
        after = os.fstat(descriptor)
        identity = lambda value: (  # noqa: E731
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
        if identity(before) != identity(after):
            _fail("authenticated-wrapper-concurrent-mutation")
        return b"".join(chunks)
    except OSError:
        _fail("authenticated-wrapper-file-read-failed")
    finally:
        os.close(descriptor)


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


def _open_directory_at(parent_fd: int, name: str, *, mode: int) -> tuple[int, tuple[int, ...]]:
    if "/" in name or name in {"", ".", ".."}:
        _fail("oci-directory-name-invalid")
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
        ):
            _fail("oci-directory-metadata-invalid")
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        identity = _metadata_identity(before)
        if _metadata_identity(opened) != identity:
            _fail("oci-directory-replaced")
        return descriptor, identity
    except MaterializationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _fail("oci-directory-open-failed")


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    size_limit: int,
    mode: int,
    identities: dict[str, tuple[int, int]] | None = None,
    identity_name: str | None = None,
) -> bytes:
    if "/" in name or name in {"", ".", ".."}:
        _fail("oci-file-name-invalid")
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > size_limit
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
        ):
            _fail("oci-file-metadata-invalid")
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        identity = _metadata_identity(before)
        if _metadata_identity(opened) != identity:
            _fail("oci-file-replaced")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                _fail("oci-file-short-read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("oci-file-grew")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _metadata_identity(after) != identity
            or _metadata_identity(current) != identity
        ):
            _fail("oci-file-concurrent-mutation")
        if identities is not None:
            identities[identity_name or name] = (identity[0], identity[1])
        return b"".join(chunks)
    except MaterializationError:
        raise
    except OSError:
        _fail("oci-file-read-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_oci_tree(root_fd: int) -> None:
    """Durably sync the exact descriptor-rooted OCI directory chain."""

    blobs_fd = -1
    sha_fd = -1
    try:
        blobs_fd, _blobs_identity = _open_directory_at(
            root_fd, "blobs", mode=0o755
        )
        sha_fd, _sha_identity = _open_directory_at(
            blobs_fd, "sha256", mode=0o755
        )
        os.fsync(sha_fd)
        os.fsync(blobs_fd)
        os.fsync(root_fd)
    except MaterializationError:
        raise
    except OSError:
        _fail("oci-layout-fsync-failed")
    finally:
        if sha_fd >= 0:
            os.close(sha_fd)
        if blobs_fd >= 0:
            os.close(blobs_fd)


def _verify_wrapper_with_authority(wrapper: str, external_anchor: str) -> object:
    """Use phase A's native Python verifier without a secret-bearing subprocess."""

    verifier = getattr(authenticated, "verify_wrapper", None)
    if verifier is None or not callable(verifier):
        _fail("authentication-verifier-unavailable")
    try:
        return verifier(wrapper=wrapper, external_anchor=external_anchor)
    except TypeError:
        # Freeze a single positional fallback for the phase-A public API while
        # still refusing to infer or probe arbitrary verifier entry points.
        try:
            return verifier(wrapper, external_anchor)
        except Exception:
            _fail("package-authentication-failed")
    except Exception:
        _fail("package-authentication-failed")


def _validate_authentication_document(
    document: object,
    *,
    auth_bytes: bytes,
    signature_bytes: bytes,
) -> Mapping[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "version",
        "signature_profile",
        "authority_scope",
        "payload",
    }:
        _fail("authentication-document-invalid")
    if document["schema"] != AUTHENTICATION_SCHEMA or document["version"] != 2:
        _fail("authentication-document-invalid")
    profile = document["signature_profile"]
    if not isinstance(profile, dict) or set(profile) != {
        "algorithm",
        "encoding",
        "key_id",
        "signed_message",
    }:
        _fail("authentication-profile-invalid")
    if (
        profile["algorithm"] != "ed25519"
        or profile["encoding"] != "raw-64-byte"
        or profile["signed_message"]
        != "domain-separated-uint64be-length-prefixed-canonical-json"
    ):
        _fail("authentication-profile-invalid")
    _require_digest(profile["key_id"], "authentication-key-id-invalid")
    scope = document["authority_scope"]
    expected_scope = {
        "kind": "local-docker",
        "scope_id": "propertyquarry-local-docker",
        "authoritative_for_package_authentication": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "performs_release_effects": False,
    }
    if scope != expected_scope:
        _fail("authentication-authority-scope-invalid")
    payload = document["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "tree_digest",
        "file_count",
        "directory_count",
        "role_count",
        "installation_manifest_sha256",
        "package_payload_receipt_sha256",
        "native_build_receipt_sha256",
    }:
        _fail("authenticated-payload-binding-invalid")
    for key in (
        "tree_digest",
        "installation_manifest_sha256",
        "package_payload_receipt_sha256",
        "native_build_receipt_sha256",
    ):
        _require_digest(payload[key], "authenticated-payload-binding-invalid")
    if (
        payload["file_count"] != 21
        or payload["role_count"] != len(installation.ROLE_CONTRACTS)
        or not isinstance(payload["directory_count"], int)
        or isinstance(payload["directory_count"], bool)
        or payload["directory_count"] <= 0
    ):
        _fail("authenticated-payload-binding-invalid")
    if len(signature_bytes) != 64 or _canonical(document) != auth_bytes:
        _fail("authentication-document-invalid")
    return payload


def _payload_specification() -> dict[str, int]:
    specification = {
        f"rootfs/{contract.path[1:]}": contract.mode
        for contract in installation.ROLE_CONTRACTS
    }
    specification["installation-manifest.v2.json"] = 0o644
    specification["package-payload-receipt.v2.json"] = 0o644
    return specification


def _captured_payload_tree_digest(
    snapshot: package_payload.TreeSnapshot,
) -> str:
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
    tree = _canonical(
        {
            "schema": PAYLOAD_TREE_SCHEMA,
            "entries": sorted(entries, key=lambda item: str(item["path"])),
        }
    )
    framed = (
        PAYLOAD_TREE_SCHEMA.encode("ascii")
        + b"\0"
        + len(tree).to_bytes(8, "big", signed=False)
        + tree
    )
    return _digest(framed)


def _snapshot_authenticated_wrapper(
    *, wrapper: str, external_anchor: str
) -> tuple[
    package_payload.TreeSnapshot,
    bytes,
    bytes,
    Mapping[str, object],
    Mapping[str, object],
]:
    wrapper_path = Path(wrapper)
    try:
        top = os.lstat(wrapper_path)
        names = set(os.listdir(wrapper_path))
    except OSError:
        _fail("authenticated-wrapper-open-failed")
    if (
        not stat.S_ISDIR(top.st_mode)
        or stat.S_ISLNK(top.st_mode)
        or names != {"payload", "authentication.v2.json", "authentication.v2.sig"}
    ):
        _fail("authenticated-wrapper-tree-invalid")
    verification = _verify_wrapper_with_authority(str(wrapper_path), external_anchor)
    auth_bytes = _read_regular(
        wrapper_path / "authentication.v2.json", size_limit=MAX_JSON_BYTES, mode=0o644
    )
    signature_bytes = _read_regular(
        wrapper_path / "authentication.v2.sig", size_limit=64, mode=0o644
    )
    auth_document = _load_canonical_json(auth_bytes, "authentication-document-invalid")
    auth_payload = _validate_authentication_document(
        auth_document, auth_bytes=auth_bytes, signature_bytes=signature_bytes
    )
    try:
        snapshot = package_payload._snapshot_tree(
            str(wrapper_path / "payload"),
            _payload_specification(),
            directory_modes=package_payload._final_directory_modes(),
        )
    except package_payload.PayloadError:
        _fail("authenticated-payload-tree-invalid")
    if len(snapshot.files) != 21 or len(snapshot.directories) != auth_payload[
        "directory_count"
    ]:
        _fail("authenticated-wrapper-count-mismatch")
    if any(
        marker in item.data
        for item in snapshot.files
        for marker in PRIVATE_KEY_MARKERS
    ):
        _fail("private-key-material-rejected")
    if (
        stat.S_IMODE(snapshot.root_identity[2]) != 0o700
        or _captured_payload_tree_digest(snapshot) != auth_payload["tree_digest"]
    ):
        _fail("authenticated-payload-tree-mismatch")
    files = snapshot.by_relative()
    manifest_bytes = files["installation-manifest.v2.json"].data
    receipt_bytes = files["package-payload-receipt.v2.json"].data
    if (
        _digest(manifest_bytes) != auth_payload["installation_manifest_sha256"]
        or _digest(receipt_bytes) != auth_payload["package_payload_receipt_sha256"]
    ):
        _fail("authenticated-payload-digest-mismatch")
    try:
        manifest = installation.parse_manifest(
            _load_canonical_json(manifest_bytes, "installation-manifest-invalid")
        )
    except Exception:
        _fail("installation-manifest-invalid")
    if installation.canonical_manifest_bytes(manifest.document()) != manifest_bytes:
        _fail("installation-manifest-invalid")
    receipt = _load_canonical_json(receipt_bytes, "package-payload-receipt-invalid")
    if not isinstance(receipt, dict):
        _fail("package-payload-receipt-invalid")
    try:
        receipt_native_digest = receipt["input_integrity"][  # type: ignore[index]
            "native_build_receipt_sha256"
        ]
    except (KeyError, TypeError):
        _fail("package-payload-receipt-invalid")
    if (
        receipt.get("schema") != package_payload.RECEIPT_SCHEMA
        or receipt.get("role_count") != len(installation.ROLE_CONTRACTS)
        or receipt.get("installation_manifest_sha256")
        != auth_payload["installation_manifest_sha256"]
        or receipt_native_digest != auth_payload["native_build_receipt_sha256"]
    ):
        _fail("package-payload-receipt-invalid")
    expected_verification = {
        "wrapper": str(wrapper_path.resolve()),
        "authentication": auth_document,
        "authentication_sha256": _digest(auth_bytes),
        "signature_sha256": _digest(signature_bytes),
    }
    if verification != expected_verification:
        _fail("authentication-verifier-result-invalid")
    manifest_by_role = {item.role: item for item in manifest.roles}
    for contract in installation.ROLE_CONTRACTS:
        expected = manifest_by_role[contract.role]
        observed = files[f"rootfs/{contract.path[1:]}"]
        if (
            observed.mode != expected.mode
            or observed.size != expected.size
            or observed.sha256 != expected.sha256
            or _digest(observed.data) != expected.sha256
        ):
            _fail("authenticated-active-role-mismatch")
    return snapshot, auth_bytes, signature_bytes, auth_document, auth_payload


def _tar_name_fields(path: str, *, directory: bool) -> tuple[bytes, bytes]:
    if path.startswith("/") or "//" in path or path in {"", ".", ".."}:
        _fail("tar-path-invalid")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail("tar-path-invalid")
    rendered = path + ("/" if directory else "")
    try:
        encoded = rendered.encode("ascii")
    except UnicodeEncodeError:
        _fail("tar-path-invalid")
    if len(encoded) <= 100:
        return encoded, b""
    for index in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:index]).encode("ascii")
        name = "/".join(parts[index:]).encode("ascii")
        if directory:
            name += b"/"
        if len(prefix) <= 155 and len(name) <= 100:
            return name, prefix
    _fail("tar-path-too-long")


def _octal(value: int, width: int) -> bytes:
    if value < 0:
        _fail("tar-numeric-field-invalid")
    encoded = format(value, "o").encode("ascii")
    if len(encoded) > width - 1:
        _fail("tar-numeric-field-invalid")
    return b"0" * (width - 1 - len(encoded)) + encoded + b"\0"


def _tar_header(record: LayerRecord) -> bytes:
    name, prefix = _tar_name_fields(record.path, directory=record.is_directory)
    size = 0 if record.data is None else len(record.data)
    header = bytearray(512)
    header[0 : len(name)] = name
    header[100:108] = _octal(record.mode, 8)
    header[108:116] = _octal(record.uid, 8)
    header[116:124] = _octal(record.gid, 8)
    header[124:136] = _octal(size, 12)
    header[136:148] = _octal(0, 12)
    header[148:156] = b"        "
    header[156:157] = b"5" if record.is_directory else b"0"
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[329:337] = _octal(0, 8)
    header[337:345] = _octal(0, 8)
    header[345 : 345 + len(prefix)] = prefix
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}".encode("ascii") + b"\0 "
    return bytes(header)


def _build_tar(records: tuple[LayerRecord, ...]) -> bytes:
    output = io.BytesIO()
    for record in records:
        output.write(_tar_header(record))
        if record.data is not None:
            output.write(record.data)
            padding = (-len(record.data)) % 512
            output.write(b"\0" * padding)
        if output.tell() > MAX_OUTPUT_BYTES:
            _fail("tar-output-oversized")
    output.write(b"\0" * 1024)
    return output.getvalue()


def _parse_octal(field: bytes, code: str) -> int:
    if not field.endswith(b"\0") or any(byte not in b"01234567" for byte in field[:-1]):
        _fail(code)
    try:
        return int(field[:-1] or b"0", 8)
    except ValueError:
        _fail(code)


def _cstring(field: bytes, code: str) -> bytes:
    value, separator, tail = field.partition(b"\0")
    if separator and any(tail):
        _fail(code)
    return value


def _parse_tar(data: bytes) -> tuple[ParsedTarRecord, ...]:
    if not data or len(data) > MAX_OUTPUT_BYTES or len(data) % 512:
        _fail("tar-invalid")
    offset = 0
    records: list[ParsedTarRecord] = []
    seen: set[str] = set()
    while offset + 512 <= len(data):
        header = data[offset : offset + 512]
        if header == b"\0" * 512:
            if data[offset:] != b"\0" * 1024:
                _fail("tar-terminator-invalid")
            return tuple(records)
        offset += 512
        checksum_field = header[148:156]
        if len(checksum_field) != 8 or checksum_field[6:] != b"\0 ":
            _fail("tar-checksum-invalid")
        try:
            expected_checksum = int(checksum_field[:6], 8)
        except ValueError:
            _fail("tar-checksum-invalid")
        checksum_header = bytearray(header)
        checksum_header[148:156] = b"        "
        if sum(checksum_header) != expected_checksum:
            _fail("tar-checksum-invalid")
        if header[257:263] != b"ustar\0" or header[263:265] != b"00":
            _fail("tar-format-invalid")
        if any(header[157:257]) or any(header[265:329]) or any(header[500:512]):
            _fail("tar-metadata-invalid")
        if _parse_octal(header[329:337], "tar-device-invalid") != 0 or _parse_octal(
            header[337:345], "tar-device-invalid"
        ) != 0:
            _fail("tar-device-invalid")
        name_bytes = _cstring(header[0:100], "tar-name-invalid")
        prefix = _cstring(header[345:500], "tar-name-invalid")
        joined = prefix + (b"/" if prefix and name_bytes else b"") + name_bytes
        try:
            path = joined.decode("ascii")
        except UnicodeDecodeError:
            _fail("tar-name-invalid")
        kind = header[156:157]
        directory = kind == b"5"
        if directory:
            if not path.endswith("/"):
                _fail("tar-directory-name-invalid")
            path = path[:-1]
        elif kind != b"0":
            _fail("tar-type-invalid")
        _tar_name_fields(path, directory=directory)
        if path in seen:
            _fail("tar-duplicate-path")
        seen.add(path)
        mode = _parse_octal(header[100:108], "tar-mode-invalid")
        uid = _parse_octal(header[108:116], "tar-uid-invalid")
        gid = _parse_octal(header[116:124], "tar-gid-invalid")
        size = _parse_octal(header[124:136], "tar-size-invalid")
        if _parse_octal(header[136:148], "tar-time-invalid") != 0:
            _fail("tar-time-invalid")
        if directory and size != 0:
            _fail("tar-directory-size-invalid")
        if offset + size > len(data):
            _fail("tar-short-read")
        body = None if directory else data[offset : offset + size]
        offset += size
        padding = (-size) % 512
        if data[offset : offset + padding] != b"\0" * padding:
            _fail("tar-padding-invalid")
        offset += padding
        records.append(ParsedTarRecord(path, mode, uid, gid, body))
    _fail("tar-terminator-missing")


def _active_directory_metadata(path: str, service_gid: int) -> tuple[int, int, int]:
    private = path in {
        "etc/propertyquarry-release-control",
        "etc/propertyquarry-release-control/trust.d",
    }
    return (0o750 if private else 0o755, 0, service_gid if private else 0)


def _add_ancestors(
    directories: dict[str, tuple[int, int, int]],
    path: str,
    service_gid: int,
    *,
    preserve_existing: bool = False,
) -> None:
    parts = path.split("/")[:-1]
    for index in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:index])
        metadata = _active_directory_metadata(ancestor, service_gid)
        existing = directories.get(ancestor)
        if existing is not None:
            if existing != metadata and not preserve_existing:
                _fail("layer-directory-metadata-conflict")
            continue
        directories[ancestor] = metadata


def _layer_records(
    snapshot: package_payload.TreeSnapshot,
    auth_bytes: bytes,
    signature_bytes: bytes,
    manifest: installation.InstallationManifest,
) -> tuple[LayerRecord, ...]:
    by_relative = snapshot.by_relative()
    manifest_by_role = {entry.role: entry for entry in manifest.roles}
    service_gids = {
        entry.gid
        for entry in manifest.roles
        if entry.role in installation.SERVICE_GROUP_ROLES
    }
    if len(service_gids) != 1:
        _fail("service-gid-projection-invalid")
    service_gid = service_gids.pop()
    directories: dict[str, tuple[int, int, int]] = {}
    files: dict[str, LayerRecord] = {}

    for contract in installation.ROLE_CONTRACTS:
        expected = manifest_by_role[contract.role]
        source = by_relative[f"rootfs/{contract.path[1:]}"]
        path = contract.path[1:]
        _add_ancestors(directories, path, service_gid)
        files[path] = LayerRecord(path, expected.mode, expected.uid, expected.gid, source.data)

    for path, metadata in (
        ("run", (0o755, 0, 0)),
        ("run/secrets", (0o755, 0, 0)),
        (RUNTIME_SOCKET_DIRECTORY, (0o750, AUTHORITY_UID, service_gid)),
        ("var", (0o755, 0, 0)),
        ("var/lib", (0o755, 0, 0)),
        (RUNTIME_STATE_DIRECTORY, (0o700, AUTHORITY_UID, service_gid)),
    ):
        existing = directories.get(path)
        if existing is not None and existing != metadata:
            _fail("runtime-directory-metadata-conflict")
        directories[path] = metadata

    # Docker bind-mounts the durable public anchor over this inert empty file.
    # Keeping the exact file target in the scratch layer avoids relying on the
    # engine to create a destination inside a read-only root filesystem.
    files[EXTERNAL_ANCHOR_MOUNT_TARGET] = LayerRecord(
        EXTERNAL_ANCHOR_MOUNT_TARGET,
        0o444,
        0,
        0,
        b"",
    )

    retained_prefix = f"{LOCAL_AUTHORITY_PREFIX}/payload"
    _add_ancestors(directories, retained_prefix + "/placeholder", service_gid)
    # The wrapper root itself is excluded from phase A's tree digest. The
    # frozen native installed-authority verifier requires this one projected
    # boundary to be root:root 0755. Descendant modes remain exactly those
    # authenticated by phase A; private descendants stay group-restricted.
    directories[retained_prefix] = (0o755, 0, 0)
    snapshot_directory_modes = {
        relative: metadata[0] for relative, metadata in snapshot.directories
    }
    for relative, mode in snapshot_directory_modes.items():
        retained = f"{retained_prefix}/{relative}"
        _add_ancestors(
            directories,
            retained + "/placeholder",
            service_gid,
            preserve_existing=True,
        )
        active_suffix = relative.removeprefix("rootfs/")
        private = relative.startswith("rootfs/etc/propertyquarry-release-control")
        gid = service_gid if private else 0
        directories[retained] = (mode, 0, gid)

    role_by_source = {
        f"rootfs/{contract.path[1:]}": manifest_by_role[contract.role]
        for contract in installation.ROLE_CONTRACTS
    }
    for source in snapshot.files:
        retained = f"{retained_prefix}/{source.relative}"
        _add_ancestors(
            directories, retained, service_gid, preserve_existing=True
        )
        expected = role_by_source.get(source.relative)
        uid = expected.uid if expected is not None else 0
        gid = expected.gid if expected is not None else 0
        files[retained] = LayerRecord(retained, source.mode, uid, gid, source.data)

    for name, data in (
        ("authentication.v2.json", auth_bytes),
        ("authentication.v2.sig", signature_bytes),
    ):
        retained = f"{LOCAL_AUTHORITY_PREFIX}/{name}"
        _add_ancestors(
            directories, retained, service_gid, preserve_existing=True
        )
        files[retained] = LayerRecord(retained, 0o644, 0, 0, data)

    directory_records = tuple(
        LayerRecord(path, metadata[0], metadata[1], metadata[2], None)
        for path, metadata in sorted(directories.items())
    )
    file_records = tuple(files[path] for path in sorted(files))
    return directory_records + file_records


def _audit_layer(layer: bytes, expected: tuple[LayerRecord, ...]) -> None:
    parsed = _parse_tar(layer)
    normalized = tuple(
        ParsedTarRecord(item.path, item.mode, item.uid, item.gid, item.data)
        for item in expected
    )
    if parsed != normalized:
        _fail("independent-layer-audit-failed")
    active = {
        contract.path[1:]: contract for contract in installation.ROLE_CONTRACTS
    }
    parsed_files = {item.path: item for item in parsed if item.data is not None}
    if set(active) - set(parsed_files):
        _fail("independent-active-role-audit-failed")
    for path, contract in active.items():
        item = parsed_files[path]
        if item.mode != contract.mode or item.uid != contract.production_uid:
            _fail("independent-active-role-audit-failed")
        if contract.role in {
            "controller-executable",
            "supervisor-executable",
            "watchdog-executable",
        }:
            try:
                package_payload._validate_static_elf_bytes(item.data or b"")
            except package_payload.PayloadError:
                _fail("scratch-executable-contract-invalid")


def _oci_documents(
    *,
    layer: bytes,
    installation_manifest_digest: str,
    authentication_digest: str,
    signature_digest: str,
    tree_digest: str,
    service_gid: int,
) -> tuple[bytes, bytes, bytes, str]:
    layer_digest = _digest(layer)
    config = {
        "architecture": "amd64",
        "config": {
            "Env": ["PATH=/usr/libexec/propertyquarry-release-control"],
            "Labels": {
                "org.opencontainers.image.title": "PropertyQuarry release control v2",
                "org.opencontainers.image.vendor": "PropertyQuarry local authority",
                "propertyquarry.local.authentication.sha256": authentication_digest,
                "propertyquarry.local.authentication.signature.sha256": signature_digest,
                "propertyquarry.local.payload-tree.sha256": tree_digest,
                "propertyquarry.local.installation-manifest.sha256": installation_manifest_digest,
                "propertyquarry.local.role-count": "19",
            },
            "User": f"{AUTHORITY_UID}:{service_gid}",
            "WorkingDir": "/",
        },
        "os": "linux",
        "rootfs": {"diff_ids": [layer_digest], "type": "layers"},
    }
    config_bytes = _canonical(config)
    config_digest = _digest(config_bytes)
    manifest = {
        "config": {
            "digest": config_digest,
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "digest": layer_digest,
                "mediaType": OCI_LAYER_MEDIA_TYPE,
                "size": len(layer),
            }
        ],
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "schemaVersion": 2,
    }
    manifest_bytes = _canonical(manifest)
    manifest_digest = _digest(manifest_bytes)
    index = {
        "manifests": [
            {
                "digest": manifest_digest,
                "mediaType": OCI_MANIFEST_MEDIA_TYPE,
                "platform": {"architecture": "amd64", "os": "linux"},
                "size": len(manifest_bytes),
            }
        ],
        "schemaVersion": 2,
    }
    return config_bytes, manifest_bytes, _canonical(index), manifest_digest


def _docker_archive(
    *, config: bytes, layer: bytes, installation_manifest_digest: str
) -> tuple[bytes, str]:
    config_hex = _digest_hex(config)
    layer_hex = _digest_hex(layer)
    tag = (
        "propertyquarry-release-control-v2:local-"
        + installation_manifest_digest.removeprefix("sha256:")[:16]
    )
    manifest = [
        {
            "Config": f"{config_hex}.json",
            "Layers": [f"{layer_hex}/layer.tar"],
            "RepoTags": [tag],
        }
    ]
    records = (
        LayerRecord(layer_hex, 0o755, 0, 0, None),
        LayerRecord(f"{config_hex}.json", 0o644, 0, 0, config),
        LayerRecord(f"{layer_hex}/VERSION", 0o644, 0, 0, b"1.0"),
        LayerRecord(f"{layer_hex}/json", 0o644, 0, 0, _canonical({"id": layer_hex})),
        LayerRecord(f"{layer_hex}/layer.tar", 0o644, 0, 0, layer),
        LayerRecord("manifest.json", 0o644, 0, 0, _canonical(manifest)),
        LayerRecord(
            "repositories",
            0o644,
            0,
            0,
            _canonical(
                {
                    "propertyquarry-release-control-v2": {
                        tag.split(":", 1)[1]: layer_hex
                    }
                }
            ),
        ),
    )
    archive = _build_tar(records)
    parsed = _parse_tar(archive)
    if tuple(item.path for item in parsed) != tuple(item.path for item in records):
        _fail("docker-archive-audit-failed")
    by_path = {item.path: item.data for item in parsed}
    if (
        by_path.get(f"{config_hex}.json") != config
        or by_path.get(f"{layer_hex}/layer.tar") != layer
    ):
        _fail("docker-archive-audit-failed")
    return archive, tag


def _create_directory_at(parent_fd: int, name: str, mode: int) -> int:
    if "/" in name or name in {"", ".", ".."}:
        _fail("output-directory-name-invalid")
    descriptor = -1
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
            or _metadata_identity(opened) != _metadata_identity(current)
        ):
            _fail("output-directory-create-failed")
        return descriptor
    except MaterializationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _fail("output-directory-create-failed")


def _write_file_at(
    parent_fd: int, name: str, data: bytes, mode: int = 0o644
) -> None:
    if "/" in name or name in {"", ".", ".."}:
        _fail("output-file-name-invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                _fail("output-write-failed")
            view = view[count:]
        os.fsync(descriptor)
        completed = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(completed.st_mode)
            or completed.st_nlink != 1
            or stat.S_IMODE(completed.st_mode) != mode
            or completed.st_uid != os.geteuid()
            or completed.st_gid != os.getegid()
            or completed.st_size != len(data)
            or _metadata_identity(completed) != _metadata_identity(current)
        ):
            _fail("output-file-create-failed")
    except MaterializationError:
        raise
    except OSError:
        _fail("output-write-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = library.renameat2
    except (OSError, AttributeError):
        _fail("renameat2-unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    ):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            _fail("output-exists")
        _fail("output-publish-failed")


def _snapshot_oci_tree(root_fd: int) -> OciTreeSnapshot:
    expected_top = {
        "blobs",
        "control-plane.compose.yml",
        "docker-image.tar",
        "index.json",
        "installation-manifest.v2.json",
        "materialization-receipt.v2.json",
        "oci-layout",
    }
    blobs_fd = -1
    sha_fd = -1
    try:
        root_before = _metadata_identity(os.fstat(root_fd))
        object_identities: dict[str, tuple[int, int]] = {
            ".": (root_before[0], root_before[1])
        }
        if (
            not stat.S_ISDIR(root_before[2])
            or stat.S_IMODE(root_before[2]) != 0o700
            or root_before[4] != os.geteuid()
            or root_before[5] != os.getegid()
            or set(os.listdir(root_fd)) != expected_top
        ):
            _fail("oci-layout-tree-invalid")
        blobs_fd, blobs_identity = _open_directory_at(root_fd, "blobs", mode=0o755)
        object_identities["blobs"] = (blobs_identity[0], blobs_identity[1])
        if set(os.listdir(blobs_fd)) != {"sha256"}:
            _fail("oci-layout-tree-invalid")
        sha_fd, sha_identity = _open_directory_at(blobs_fd, "sha256", mode=0o755)
        object_identities["blobs/sha256"] = (sha_identity[0], sha_identity[1])
        layout = _read_regular_at(
            root_fd,
            "oci-layout",
            size_limit=4096,
            mode=0o644,
            identities=object_identities,
        )
        index = _read_regular_at(
            root_fd,
            "index.json",
            size_limit=MAX_JSON_BYTES,
            mode=0o644,
            identities=object_identities,
        )
        installation_manifest = _read_regular_at(
            root_fd,
            "installation-manifest.v2.json",
            size_limit=MAX_JSON_BYTES,
            mode=0o644,
            identities=object_identities,
        )
        materialization_receipt = _read_regular_at(
            root_fd,
            "materialization-receipt.v2.json",
            size_limit=MAX_JSON_BYTES,
            mode=0o644,
            identities=object_identities,
        )
        compose = _read_regular_at(
            root_fd,
            "control-plane.compose.yml",
            size_limit=MAX_JSON_BYTES,
            mode=0o644,
            identities=object_identities,
        )
        docker_archive = _read_regular_at(
            root_fd,
            "docker-image.tar",
            size_limit=MAX_OUTPUT_BYTES,
            mode=0o600,
            identities=object_identities,
        )
        index_document = _load_canonical_json(index, "oci-index-invalid")
        try:
            manifest_digest = _require_digest(
                index_document["manifests"][0]["digest"],  # type: ignore[index]
                "oci-index-invalid",
            )
        except (KeyError, IndexError, TypeError):
            _fail("oci-index-invalid")
        manifest_name = manifest_digest.removeprefix("sha256:")
        manifest = _read_regular_at(
            sha_fd,
            manifest_name,
            size_limit=MAX_JSON_BYTES,
            mode=0o644,
            identities=object_identities,
            identity_name=f"blobs/sha256/{manifest_name}",
        )
        manifest_document = _load_canonical_json(manifest, "oci-manifest-invalid")
        try:
            config_name = _require_digest(
                manifest_document["config"]["digest"],  # type: ignore[index]
                "oci-manifest-invalid",
            ).removeprefix("sha256:")
            layer_name = _require_digest(
                manifest_document["layers"][0]["digest"],  # type: ignore[index]
                "oci-manifest-invalid",
            ).removeprefix("sha256:")
        except (KeyError, IndexError, TypeError):
            _fail("oci-manifest-invalid")
        blob_names = {manifest_name, config_name, layer_name}
        if (
            len(blob_names) != 3
            or any(_HEX_RE.fullmatch(name) is None for name in blob_names)
            or set(os.listdir(sha_fd)) != blob_names
        ):
            _fail("oci-blob-set-invalid")
        blobs = {
            manifest_name: manifest,
            config_name: _read_regular_at(
                sha_fd,
                config_name,
                size_limit=MAX_JSON_BYTES,
                mode=0o644,
                identities=object_identities,
                identity_name=f"blobs/sha256/{config_name}",
            ),
            layer_name: _read_regular_at(
                sha_fd,
                layer_name,
                size_limit=MAX_LAYER_BYTES,
                mode=0o644,
                identities=object_identities,
                identity_name=f"blobs/sha256/{layer_name}",
            ),
        }
        if (
            set(os.listdir(root_fd)) != expected_top
            or set(os.listdir(blobs_fd)) != {"sha256"}
            or set(os.listdir(sha_fd)) != blob_names
            or _metadata_identity(os.fstat(root_fd)) != root_before
            or _metadata_identity(os.fstat(blobs_fd)) != blobs_identity
            or _metadata_identity(os.fstat(sha_fd)) != sha_identity
        ):
            _fail("oci-tree-concurrent-mutation")
        reopened_blobs, current_blobs = _open_directory_at(
            root_fd, "blobs", mode=0o755
        )
        try:
            reopened_sha, current_sha = _open_directory_at(
                reopened_blobs, "sha256", mode=0o755
            )
            os.close(reopened_sha)
        finally:
            os.close(reopened_blobs)
        if current_blobs != blobs_identity or current_sha != sha_identity:
            _fail("oci-directory-replaced")
        expected_identity_names = {
            ".",
            "blobs",
            "blobs/sha256",
            *(expected_top - {"blobs"}),
            *(f"blobs/sha256/{name}" for name in blob_names),
        }
        if set(object_identities) != expected_identity_names:
            _fail("oci-tree-identity-incomplete")
        return OciTreeSnapshot(
            layout,
            index,
            installation_manifest,
            materialization_receipt,
            compose,
            docker_archive,
            blobs,
            tuple(sorted(object_identities.items())),
        )
    except OSError:
        _fail("oci-layout-tree-invalid")
    finally:
        if sha_fd >= 0:
            os.close(sha_fd)
        if blobs_fd >= 0:
            os.close(blobs_fd)


def _audit_materialized_tree(
    root_fd: int,
    *,
    expected_identities: tuple[tuple[str, tuple[int, int]], ...] | None = None,
) -> AuditedOciTree:
    snapshot = _snapshot_oci_tree(root_fd)
    if (
        expected_identities is not None
        and snapshot.identities != expected_identities
    ):
        _fail("oci-tree-object-identity-changed")
    layout_bytes = snapshot.layout
    if _load_canonical_json(layout_bytes, "oci-layout-invalid") != {
        "imageLayoutVersion": OCI_LAYOUT_VERSION
    }:
        _fail("oci-layout-invalid")
    receipt_bytes = snapshot.materialization_receipt
    receipt = _load_canonical_json(receipt_bytes, "materialization-receipt-invalid")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        _fail("materialization-receipt-invalid")
    fixed_receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": 2,
        "authoritative_for_local_image_materialization": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "performs_release_effects": False,
        "loads_or_runs_docker": False,
        "network_implementation_present": False,
        "scratch_image": True,
        "scratch_execution_contract_verified": True,
        "architecture": "amd64",
        "os": "linux",
        "role_count": 19,
        "retained_authenticated_payload_file_count": 21,
        "independent_active_role_audit": True,
        "projected_numeric_ownership_verified": True,
        "private_key_material_rejected": True,
    }
    if any(receipt.get(key) != value for key, value in fixed_receipt.items()):
        _fail("materialization-receipt-invalid")
    for key in (
        "authentication_sha256",
        "authentication_signature_sha256",
        "authenticated_payload_tree_digest",
        "installation_manifest_sha256",
        "package_payload_receipt_sha256",
        "native_build_receipt_sha256",
        "layer_digest",
        "image_id",
        "image_digest",
        "index_sha256",
        "docker_archive_sha256",
        "compose_sha256",
    ):
        _require_digest(receipt.get(key), "materialization-receipt-invalid")
    index_bytes = snapshot.index
    if _digest(index_bytes) != receipt["index_sha256"]:
        _fail("oci-index-digest-mismatch")
    index = _load_canonical_json(index_bytes, "oci-index-invalid")
    if not isinstance(index, dict) or set(index) != {"manifests", "schemaVersion"}:
        _fail("oci-index-invalid")
    try:
        descriptor = index["manifests"][0]
        manifest_digest = _require_digest(descriptor["digest"], "oci-index-invalid")
        if len(index["manifests"]) != 1 or index["schemaVersion"] != 2:
            _fail("oci-index-invalid")
    except (KeyError, IndexError, TypeError):
        _fail("oci-index-invalid")
    manifest_bytes = snapshot.blobs[manifest_digest.removeprefix("sha256:")]
    if _digest(manifest_bytes) != manifest_digest:
        _fail("oci-manifest-digest-mismatch")
    if manifest_digest != receipt["image_digest"]:
        _fail("materialization-receipt-invalid")
    manifest = _load_canonical_json(manifest_bytes, "oci-manifest-invalid")
    if not isinstance(manifest, dict) or set(manifest) != {
        "config",
        "layers",
        "mediaType",
        "schemaVersion",
    }:
        _fail("oci-manifest-invalid")
    try:
        config_descriptor = manifest["config"]
        layer_descriptor = manifest["layers"][0]
        config_digest = _require_digest(config_descriptor["digest"], "oci-manifest-invalid")
        layer_digest = _require_digest(layer_descriptor["digest"], "oci-manifest-invalid")
        if len(manifest["layers"]) != 1 or manifest["schemaVersion"] != 2:
            _fail("oci-manifest-invalid")
    except (KeyError, IndexError, TypeError):
        _fail("oci-manifest-invalid")
    config_bytes = snapshot.blobs[config_digest.removeprefix("sha256:")]
    layer_bytes = snapshot.blobs[layer_digest.removeprefix("sha256:")]
    if _digest(config_bytes) != config_digest or _digest(layer_bytes) != layer_digest:
        _fail("oci-blob-digest-mismatch")
    if config_digest != receipt["image_id"] or layer_digest != receipt["layer_digest"]:
        _fail("materialization-receipt-invalid")
    installation_manifest_bytes = snapshot.installation_manifest
    if _digest(installation_manifest_bytes) != receipt["installation_manifest_sha256"]:
        _fail("installation-manifest-digest-mismatch")
    manifest_document = installation.parse_manifest(
        _load_canonical_json(
            installation_manifest_bytes, "installation-manifest-invalid"
        )
    )
    service_gids = {
        expected.gid
        for expected in manifest_document.roles
        if expected.role in installation.SERVICE_GROUP_ROLES
    }
    if len(service_gids) != 1:
        _fail("service-gid-projection-invalid")
    service_gid = service_gids.pop()
    authority_user = f"{AUTHORITY_UID}:{service_gid}"
    caller_user = f"{CALLER_UID}:{service_gid}"
    if (
        receipt["runtime_user"] != authority_user
        or receipt["authority_user"] != authority_user
        or receipt["caller_user"] != caller_user
    ):
        _fail("materialization-receipt-invalid")
    parsed_layer = _parse_tar(layer_bytes)
    layer_files = {item.path: item for item in parsed_layer if item.data is not None}
    for expected in manifest_document.roles:
        item = layer_files.get(expected.path[1:])
        if (
            item is None
            or item.mode != expected.mode
            or item.uid != expected.uid
            or item.gid != expected.gid
            or item.data is None
            or len(item.data) != expected.size
            or _digest(item.data) != expected.sha256
        ):
            _fail("independent-active-role-audit-failed")
        if expected.role in {
            "controller-executable",
            "supervisor-executable",
            "watchdog-executable",
        }:
            try:
                package_payload._validate_static_elf_bytes(item.data)
            except package_payload.PayloadError:
                _fail("scratch-executable-contract-invalid")
    retained_manifest = layer_files.get(
        f"{LOCAL_AUTHORITY_PREFIX}/payload/installation-manifest.v2.json"
    )
    retained_receipt = layer_files.get(
        f"{LOCAL_AUTHORITY_PREFIX}/payload/package-payload-receipt.v2.json"
    )
    retained_authentication = layer_files.get(
        f"{LOCAL_AUTHORITY_PREFIX}/authentication.v2.json"
    )
    retained_signature = layer_files.get(f"{LOCAL_AUTHORITY_PREFIX}/authentication.v2.sig")
    if (
        retained_manifest is None
        or retained_manifest.data != installation_manifest_bytes
        or retained_receipt is None
        or retained_receipt.data is None
        or retained_authentication is None
        or retained_authentication.data is None
        or retained_signature is None
        or retained_signature.data is None
    ):
        _fail("retained-authentication-evidence-invalid")
    if (
        _digest(retained_receipt.data) != receipt["package_payload_receipt_sha256"]
        or _digest(retained_authentication.data) != receipt["authentication_sha256"]
        or _digest(retained_signature.data)
        != receipt["authentication_signature_sha256"]
    ):
        _fail("retained-authentication-evidence-invalid")
    auth_document = _load_canonical_json(
        retained_authentication.data, "authentication-document-invalid"
    )
    auth_payload = _validate_authentication_document(
        auth_document,
        auth_bytes=retained_authentication.data,
        signature_bytes=retained_signature.data,
    )
    if (
        auth_payload["tree_digest"] != receipt["authenticated_payload_tree_digest"]
        or auth_payload["installation_manifest_sha256"]
        != receipt["installation_manifest_sha256"]
        or auth_payload["package_payload_receipt_sha256"]
        != receipt["package_payload_receipt_sha256"]
        or auth_payload["native_build_receipt_sha256"]
        != receipt["native_build_receipt_sha256"]
    ):
        _fail("retained-authentication-evidence-invalid")
    package_receipt = _load_canonical_json(
        retained_receipt.data, "package-payload-receipt-invalid"
    )
    try:
        retained_native_digest = package_receipt["input_integrity"][  # type: ignore[index]
            "native_build_receipt_sha256"
        ]
    except (KeyError, TypeError):
        _fail("package-payload-receipt-invalid")
    if retained_native_digest != receipt["native_build_receipt_sha256"]:
        _fail("package-payload-receipt-invalid")
    expected_config, expected_manifest, expected_index, expected_image_digest = (
        _oci_documents(
            layer=layer_bytes,
            installation_manifest_digest=str(receipt["installation_manifest_sha256"]),
            authentication_digest=str(receipt["authentication_sha256"]),
            signature_digest=str(receipt["authentication_signature_sha256"]),
            tree_digest=str(receipt["authenticated_payload_tree_digest"]),
            service_gid=service_gid,
        )
    )
    if (
        config_bytes != expected_config
        or manifest_bytes != expected_manifest
        or index_bytes != expected_index
        or manifest_digest != expected_image_digest
    ):
        _fail("oci-public-metadata-invalid")
    docker_archive = snapshot.docker_archive
    expected_archive, expected_tag = _docker_archive(
        config=config_bytes,
        layer=layer_bytes,
        installation_manifest_digest=str(receipt["installation_manifest_sha256"]),
    )
    if (
        docker_archive != expected_archive
        or receipt["docker_archive_sha256"] != _digest(docker_archive)
        or receipt["docker_archive_tag"] != expected_tag
        or receipt["compose_sha256"] != _digest(snapshot.compose)
    ):
        _fail("materialization-receipt-invalid")
    blob_names = set(snapshot.blobs)
    if blob_names != {
        config_digest.removeprefix("sha256:"),
        layer_digest.removeprefix("sha256:"),
        manifest_digest.removeprefix("sha256:"),
    }:
        _fail("oci-blob-set-invalid")
    return AuditedOciTree(receipt, snapshot)


def _audit_materialized_output(root_fd: int) -> Mapping[str, object]:
    return _audit_materialized_tree(root_fd).receipt


def materialize(
    *,
    wrapper: str,
    external_anchor: str,
    output: str,
    phase_hook: Callable[[str], None] | None = None,
) -> MaterializedImage:
    """Authenticate, materialize, independently audit, and atomically publish."""

    phase = phase_hook or (lambda _name: None)
    first = _snapshot_authenticated_wrapper(
        wrapper=wrapper, external_anchor=external_anchor
    )
    snapshot, auth_bytes, signature_bytes, _auth_document, auth_payload = first
    phase("authenticated-input-snapshotted")
    by_relative = snapshot.by_relative()
    manifest_bytes = by_relative["installation-manifest.v2.json"].data
    manifest = installation.parse_manifest(
        _load_canonical_json(manifest_bytes, "installation-manifest-invalid")
    )
    service_gids = {
        item.gid
        for item in manifest.roles
        if item.role in installation.SERVICE_GROUP_ROLES
    }
    if len(service_gids) != 1:
        _fail("service-gid-projection-invalid")
    service_gid = service_gids.pop()
    records = _layer_records(snapshot, auth_bytes, signature_bytes, manifest)
    layer = _build_tar(records)
    if len(layer) > MAX_LAYER_BYTES:
        _fail("oci-layer-oversized")
    _audit_layer(layer, records)
    phase("independent-layer-audited")
    authentication_digest = _digest(auth_bytes)
    signature_digest = _digest(signature_bytes)
    config, oci_manifest, index, image_digest = _oci_documents(
        layer=layer,
        installation_manifest_digest=_digest(manifest_bytes),
        authentication_digest=authentication_digest,
        signature_digest=signature_digest,
        tree_digest=str(auth_payload["tree_digest"]),
        service_gid=service_gid,
    )
    docker_archive, docker_tag = _docker_archive(
        config=config,
        layer=layer,
        installation_manifest_digest=_digest(manifest_bytes),
    )
    image_id = _digest(config)
    layer_digest = _digest(layer)
    docker_digest = _digest(docker_archive)
    compose_bytes = _read_regular(COMPOSE_PATH, size_limit=MAX_JSON_BYTES)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": 2,
        "authoritative_for_local_image_materialization": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "performs_release_effects": False,
        "loads_or_runs_docker": False,
        "network_implementation_present": False,
        "scratch_image": True,
        "scratch_execution_contract_verified": True,
        "architecture": "amd64",
        "os": "linux",
        "role_count": len(installation.ROLE_CONTRACTS),
        "retained_authenticated_payload_file_count": 21,
        "independent_active_role_audit": True,
        "projected_numeric_ownership_verified": True,
        "private_key_material_rejected": True,
        "authentication_sha256": authentication_digest,
        "authentication_signature_sha256": signature_digest,
        "authenticated_payload_tree_digest": auth_payload["tree_digest"],
        "installation_manifest_sha256": _digest(manifest_bytes),
        "package_payload_receipt_sha256": auth_payload[
            "package_payload_receipt_sha256"
        ],
        "native_build_receipt_sha256": auth_payload["native_build_receipt_sha256"],
        "layer_digest": layer_digest,
        "image_id": image_id,
        "image_digest": image_digest,
        "index_sha256": _digest(index),
        "docker_archive_sha256": docker_digest,
        "docker_archive_tag": docker_tag,
        "compose_sha256": _digest(compose_bytes),
        # Keep runtime_user as the compatibility alias consumed by the
        # one-shot container gate; the persistent runtime uses the explicit
        # authority and caller principals below.
        "runtime_user": f"{AUTHORITY_UID}:{service_gid}",
        "authority_user": f"{AUTHORITY_UID}:{service_gid}",
        "caller_user": f"{CALLER_UID}:{service_gid}",
    }
    receipt_bytes = _canonical(receipt)
    try:
        output_path = Path(package_payload._path_text(output, "output-path-invalid"))
    except package_payload.PayloadError as error:
        raise MaterializationError(error.code) from None
    parent = output_path.parent
    if output_path.name in {"", ".", ".."}:
        _fail("output-path-invalid")
    parent_fd = -1
    temporary_fd = -1
    temporary_name: str | None = None
    temporary_identity: tuple[int, ...] | None = None
    published = False
    try:
        try:
            parent_fd, _initial_parent_identity = (
                package_payload._open_controlled_directory(str(parent), writable=True)
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        try:
            os.stat(output_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            _fail("output-preflight-failed")
        else:
            _fail("output-exists")
        try:
            temporary_name, _temporary_path, temporary_fd = (
                package_payload._make_private_temp(
                    parent_fd,
                    str(parent),
                    f"{output_path.name}.oci-assembling",
                )
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        parent_identity = package_payload._parent_path_identity(os.fstat(parent_fd))
        temporary_identity = package_payload._directory_identity(
            os.fstat(temporary_fd)
        )
        blobs_fd = -1
        sha_fd = -1
        try:
            blobs_fd = _create_directory_at(temporary_fd, "blobs", 0o755)
            sha_fd = _create_directory_at(blobs_fd, "sha256", 0o755)
            _write_file_at(
                temporary_fd,
                "oci-layout",
                _canonical({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
            )
            _write_file_at(temporary_fd, "index.json", index)
            _write_file_at(
                temporary_fd, "installation-manifest.v2.json", manifest_bytes
            )
            _write_file_at(
                temporary_fd, "materialization-receipt.v2.json", receipt_bytes
            )
            _write_file_at(
                temporary_fd, "control-plane.compose.yml", compose_bytes
            )
            _write_file_at(
                temporary_fd, "docker-image.tar", docker_archive, 0o600
            )
            for data in (config, oci_manifest, layer):
                _write_file_at(sha_fd, _digest_hex(data), data)
        finally:
            if sha_fd >= 0:
                os.close(sha_fd)
            if blobs_fd >= 0:
                os.close(blobs_fd)
        phase("output-written")
        staged_audit = _audit_materialized_tree(temporary_fd)
        staged_identities = staged_audit.snapshot.identities
        phase("output-independently-audited")
        second = _snapshot_authenticated_wrapper(
            wrapper=wrapper, external_anchor=external_anchor
        )
        if second[:3] != first[:3] or second[4] != first[4]:
            _fail("authenticated-wrapper-concurrent-mutation")
        phase("authenticated-input-reverified")
        _fsync_oci_tree(temporary_fd)
        _audit_materialized_tree(
            temporary_fd,
            expected_identities=staged_identities,
        )
        temporary_identity = package_payload._directory_identity(
            os.fstat(temporary_fd)
        )
        try:
            package_payload._revalidate_parent_path(
                str(parent), parent_fd, parent_identity
            )
            package_payload._revalidate_temp_root(
                parent_fd,
                temporary_name,
                temporary_fd,
                temporary_identity,
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        _rename_noreplace(parent_fd, temporary_name, output_path.name)
        # From this point until the post-rename descriptor audit and parent
        # fsync pass, the destination remains rollback-owned by this still-open
        # directory descriptor. Keeping ``published`` false makes every error
        # remove only the name that rebinds to that exact inode.
        temporary_name = output_path.name
        phase("after-rename-before-audit")
        try:
            package_payload._revalidate_temp_root(
                parent_fd,
                output_path.name,
                temporary_fd,
                temporary_identity,
            )
            package_payload._revalidate_parent_path(
                str(parent), parent_fd, parent_identity
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        _audit_materialized_tree(
            temporary_fd,
            expected_identities=staged_identities,
        )
        os.fsync(parent_fd)
        phase("parent-directory-fsynced")
        try:
            package_payload._revalidate_temp_root(
                parent_fd,
                output_path.name,
                temporary_fd,
                temporary_identity,
            )
            package_payload._revalidate_parent_path(
                str(parent), parent_fd, parent_identity
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        _audit_materialized_tree(
            temporary_fd,
            expected_identities=staged_identities,
        )
        try:
            package_payload._revalidate_temp_root(
                parent_fd,
                output_path.name,
                temporary_fd,
                temporary_identity,
            )
            package_payload._revalidate_parent_path(
                str(parent), parent_fd, parent_identity
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        published = True
        return MaterializedImage(
            str(output_path), image_id, image_digest, _digest(receipt_bytes), docker_digest
        )
    finally:
        cleanup_error: MaterializationError | None = None
        if temporary_fd >= 0:
            try:
                temporary_identity = package_payload._directory_identity(
                    os.fstat(temporary_fd)
                )
            except OSError:
                temporary_identity = None
            os.close(temporary_fd)
        if (
            parent_fd >= 0
            and not published
            and temporary_name is not None
            and temporary_identity is not None
        ):
            try:
                package_payload._remove_temp(
                    parent_fd, temporary_name, temporary_identity
                )
            except package_payload.PayloadError as error:
                cleanup_error = MaterializationError(error.code)
        if parent_fd >= 0:
            os.close(parent_fd)
        if cleanup_error is not None:
            raise cleanup_error


def verify(
    output: str,
    *,
    wrapper: str,
    external_anchor: str,
) -> Mapping[str, object]:
    """Re-audit image bytes and reauthenticate the separately retained source."""

    descriptor = -1
    try:
        try:
            output_path = package_payload._path_text(
                output, "oci-output-path-invalid"
            )
            descriptor, _identity = package_payload._open_controlled_directory(
                output_path, writable=False
            )
        except package_payload.PayloadError as error:
            raise MaterializationError(error.code) from None
        return verify_pinned(
            descriptor,
            wrapper=wrapper,
            external_anchor=external_anchor,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_pinned(
    root_fd: int,
    *,
    wrapper: str,
    external_anchor: str,
) -> Mapping[str, object]:
    """Verify one already descriptor-pinned OCI output root."""

    initial_audit = _audit_materialized_tree(root_fd)
    receipt = initial_audit.receipt
    snapshot, auth_bytes, signature_bytes, _document, auth_payload = (
        _snapshot_authenticated_wrapper(
            wrapper=wrapper, external_anchor=external_anchor
        )
    )
    files = snapshot.by_relative()
    manifest_bytes = files["installation-manifest.v2.json"].data
    package_receipt_bytes = files["package-payload-receipt.v2.json"].data
    manifest = installation.parse_manifest(
        _load_canonical_json(manifest_bytes, "installation-manifest-invalid")
    )
    expected_layer = _build_tar(
        _layer_records(snapshot, auth_bytes, signature_bytes, manifest)
    )
    if (
        _digest(auth_bytes) != receipt["authentication_sha256"]
        or _digest(signature_bytes) != receipt["authentication_signature_sha256"]
        or auth_payload["tree_digest"] != receipt["authenticated_payload_tree_digest"]
        or _digest(manifest_bytes) != receipt["installation_manifest_sha256"]
        or _digest(package_receipt_bytes)
        != receipt["package_payload_receipt_sha256"]
        or _digest(expected_layer) != receipt["layer_digest"]
    ):
        _fail("authenticated-source-image-binding-mismatch")
    final_audit = _audit_materialized_tree(
        root_fd,
        expected_identities=initial_audit.snapshot.identities,
    )
    if final_audit.receipt != receipt:
        _fail("oci-verification-concurrent-mutation")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daemonless authenticated PropertyQuarry local OCI materializer."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("materialize")
    build.add_argument("--wrapper", required=True)
    build.add_argument("--external-anchor", required=True)
    build.add_argument("--output", required=True)
    audit = commands.add_parser("verify")
    audit.add_argument("--output", required=True)
    audit.add_argument("--wrapper", required=True)
    audit.add_argument("--external-anchor", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "materialize":
            result = materialize(
                wrapper=arguments.wrapper,
                external_anchor=arguments.external_anchor,
                output=arguments.output,
            )
            print(
                _canonical(
                    {
                        "docker_archive_sha256": result.docker_archive_sha256,
                        "image_digest": result.image_digest,
                        "image_id": result.image_id,
                        "output": result.output,
                        "receipt_sha256": result.receipt_sha256,
                    }
                ).decode("ascii")
            )
        else:
            print(
                _canonical(
                    verify(
                        arguments.output,
                        wrapper=arguments.wrapper,
                        external_anchor=arguments.external_anchor,
                    )
                ).decode("ascii")
            )
    except MaterializationError as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
