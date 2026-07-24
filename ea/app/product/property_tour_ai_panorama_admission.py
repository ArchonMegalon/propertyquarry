"""Controller-owned admission for one-time AI panorama installation.

This module verifies and consumes authority; it never creates it.  A permit is
accepted only when its Ed25519 signature, short lifetime, release context,
candidate identity, content digests, and fixed-root artifact paths all match.
The signing key is selected through the independently anchored release-control
keyring.  Replay state lives in one controller-owned, pre-provisioned ledger.

The returned frozen object is intentionally not authority by construction.
Callers must pass it through :func:`revalidate_ai_panorama_install_admission`
at the mutation boundary.  Revalidation reads the signed permit and the fixed
ledger again, so copying the dataclass, toggling a boolean, deleting the permit
or ledger, or selecting another ledger cannot authorize an install.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts import propertyquarry_deploy_drain_keyring as release_keyring


PERMIT_SCHEMA = "propertyquarry.ai-panorama-install-permit.v1"
PERMIT_VERSION = 1
PERMIT_AUDIENCE = "propertyquarry-ai-panorama-install-controller"
PERMIT_ISSUER = "propertyquarry-release-control"
PERMIT_OPERATION = "ai-panorama-install"
SIGNATURE_DOMAIN = b"propertyquarry.ai-panorama-install-permit.signature.v1\0"
LEDGER_SCHEMA = "propertyquarry.ai-panorama-install-consumption-ledger.v1"
LEDGER_AUTHORITY = "propertyquarry-release-control"
VOLUME_PROFILE_SCHEMA = "propertyquarry.public-tour-volume-profile.v1"
CANONICAL_PUBLIC_TOUR_VOLUME_NAME = "property_propertyquarry_public_tours"
CANONICAL_PUBLIC_TOUR_MOUNT_TARGET = "/data/public_property_tours"
CANONICAL_PUBLIC_TOUR_SETTING = "EA_PUBLIC_TOUR_DIR"
CANONICAL_PUBLIC_TOUR_STORAGE_KIND = "docker-named-volume"
CANONICAL_PUBLIC_TOUR_RUNTIME_UID = 10001
CANONICAL_PUBLIC_TOUR_RUNTIME_GID = 10001
CANONICAL_PUBLIC_ORIGIN = "https://propertyquarry.com"

MAX_PERMIT_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_TTL_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
MAX_LEDGER_ENTRIES = 100_000

CONTROL_ROOT = Path(
    "/var/lib/propertyquarry/release-control/ai-panorama-install"
)
PERMIT_ROOT = CONTROL_ROOT / "permits"
LEDGER_PATH = CONTROL_ROOT / "consumption-ledger.v1.json"
LEDGER_LOCK_PATH = CONTROL_ROOT / "consumption-ledger.v1.lock"
VOLUME_PROFILE_PATH = Path(
    "/etc/propertyquarry/release-control/public-tour-volume-profile.v1.json"
)
CONTROLLER_REQUIRED_UID = 0
CONTROLLER_FILE_MODE = 0o600
EXTERNAL_PROFILE_MODE = 0o444

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}\Z")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})\Z"
)
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z"
)
_GENESIS_DIGEST = "0" * 64


class AiPanoramaInstallPermitError(ValueError):
    """Fail-closed permit, path, keyring, or replay rejection."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise AiPanoramaInstallPermitError(code)


@dataclass(frozen=True, slots=True)
class _ControllerPaths:
    control_root: Path
    permit_root: Path
    ledger_path: Path
    ledger_lock_path: Path
    volume_profile_path: Path
    required_uid: int


_CONTROLLER_PATHS = _ControllerPaths(
    control_root=CONTROL_ROOT,
    permit_root=PERMIT_ROOT,
    ledger_path=LEDGER_PATH,
    ledger_lock_path=LEDGER_LOCK_PATH,
    volume_profile_path=VOLUME_PROFILE_PATH,
    required_uid=CONTROLLER_REQUIRED_UID,
)


@dataclass(frozen=True, slots=True)
class AiPanoramaInstallExpectedBindings:
    """Authenticated request and release context expected by the consumer."""

    subject: str
    authenticated_principal_id: str
    search_run_id: str
    candidate_ref: str
    external_id: str
    listing_url: str
    source_ref: str
    provider_key: str
    expected_slug: str
    expected_source_tree_sha256: str
    expected_tour_sha256: str
    expected_core_manifest_sha256: str
    expected_materialization_receipt_sha256: str
    expected_candidate_marker_sha256: str
    expected_publication_record_sha256: str
    artifact_relpath: str
    materialization_receipt_relpath: str
    request_id: str
    repository: str
    git_ref: str
    git_head_sha: str
    workflow_ref: str
    job: str
    environment: str
    review_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedAiPanoramaInstallAdmission:
    """Immutable projection of a currently verified signed permit."""

    operation: str
    subject: str
    authenticated_principal_id: str
    search_run_id: str
    candidate_ref: str
    external_id: str
    listing_url: str
    source_ref: str
    provider_key: str
    expected_slug: str
    expected_source_tree_sha256: str
    expected_tour_sha256: str
    expected_core_manifest_sha256: str
    expected_materialization_receipt_sha256: str
    expected_candidate_marker_sha256: str
    expected_publication_record_sha256: str
    source_bundle: Path
    materialization_receipt_path: Path
    incoming_root: Path
    public_tour_dir: Path
    public_tour_volume_name: str
    public_tour_mount_target: str
    public_tour_root_device: int
    public_tour_root_inode: int
    public_control_url: str
    artifact_relpath: str
    materialization_receipt_relpath: str
    permit_sha256: str
    request_id: str
    nonce: str
    repository: str
    git_ref: str
    git_head_sha: str
    workflow_ref: str
    job: str
    environment: str
    review_receipt_sha256: str
    issued_at: str
    expires_at: str
    key_id: str
    key_epoch: int
    key_sha256: str
    volume_profile_sha256: str
    permit_verified: bool
    nonce_consumed: bool
    _permit_relpath: str
    _permit_file_identity: tuple[int, ...]
    _source_bundle_identity: tuple[int, ...]
    _materialization_receipt_identity: tuple[int, ...]
    _controller_paths_sha256: str
    _signed_preimage_sha256: str
    _context_sha256: str
    _ledger_instance_id: str
    _ledger_sequence: int
    _ledger_entry_sha256: str


@dataclass(frozen=True, slots=True)
class _StableFile:
    data: bytes
    identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _VolumeProfile:
    environment: str
    artifact_root: Path
    artifact_root_identity: tuple[int, ...]
    public_tour_root: Path
    public_tour_root_identity: tuple[int, ...]
    public_tour_volume_name: str
    public_tour_mount_target: str
    volume_id: str
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AiPanoramaInstallPermitError("permit_noncanonical_value") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        _fail(code)


def _string(value: Any, code: str, *, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(code)
    return value


def _safe_id(value: Any, code: str) -> str:
    text = _string(value, code, maximum=256)
    if _SAFE_ID_RE.fullmatch(text) is None:
        _fail(code)
    return text


def _digest(value: Any, code: str) -> str:
    text = _string(value, code, maximum=64)
    if _DIGEST_RE.fullmatch(text) is None:
        _fail(code)
    return text


def _safe_relpath(value: Any, code: str) -> str:
    text = _string(value, code, maximum=512)
    if "\\" in text or text.startswith("/"):
        _fail(code)
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != text
        or any(part in {"", ".", ".."} or part.startswith(".") for part in candidate.parts)
    ):
        _fail(code)
    return text


def _https_url(value: Any, code: str) -> str:
    text = _string(value, code, maximum=4096)
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or urllib.parse.urlunsplit(parsed) != text
    ):
        _fail(code)
    return text


def _timestamp(value: Any, code: str) -> datetime:
    text = _string(value, code, maximum=40)
    if _TIMESTAMP_RE.fullmatch(text) is None:
        _fail(code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or not math.isfinite(parsed.timestamp())
    ):
        _fail(code)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("permit_duplicate_json_key")
        result[key] = value
    return result


def _strict_json(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: _fail("permit_nonfinite_json"),
        )
    except AiPanoramaInstallPermitError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    if not isinstance(value, dict):
        _fail(code)
    return value


def _file_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_uid),
        int(details.st_gid),
        int(details.st_nlink),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
    )


def _directory_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_uid),
        int(details.st_gid),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
    )


def _require_secure_open_primitives() -> None:
    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        _fail("ai_panorama_secure_open_unavailable")


def _open_absolute_directory(path: Path, *, code: str) -> int:
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        _fail(code)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            _fail(code)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_relative_directory(
    root_descriptor: int,
    relpath: str,
    *,
    code: str,
    required_uid: int | None,
    forbid_writable: bool,
) -> tuple[int, tuple[int, ...]]:
    normalized = _safe_relpath(relpath, code)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.dup(root_descriptor)
    try:
        for part in PurePosixPath(normalized).parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            details = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(details.st_mode)
                or (required_uid is not None and details.st_uid != required_uid)
                or (forbid_writable and stat.S_IMODE(details.st_mode) & 0o022)
            ):
                _fail(code)
        details = os.fstat(descriptor)
        return descriptor, _directory_identity(details)
    except AiPanoramaInstallPermitError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise AiPanoramaInstallPermitError(code) from exc


def _read_relative_regular(
    root_descriptor: int,
    relpath: str,
    *,
    code: str,
    maximum_bytes: int,
    required_uid: int | None,
    exact_mode: int | None = None,
    forbidden_mode_bits: int = 0o022,
) -> _StableFile:
    normalized = _safe_relpath(relpath, code)
    parts = PurePosixPath(normalized).parts
    parent_descriptor = os.dup(root_descriptor)
    descriptor = -1
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
            directory_details = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(directory_details.st_mode)
                or (
                    required_uid is not None
                    and directory_details.st_uid != required_uid
                )
                or stat.S_IMODE(directory_details.st_mode) & forbidden_mode_bits
            ):
                _fail(code)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(parts[-1], flags, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or (required_uid is not None and before.st_uid != required_uid)
            or (exact_mode is not None and mode != exact_mode)
            or (exact_mode is None and mode & forbidden_mode_bits)
        ):
            _fail(code)
        before_identity = _file_identity(before)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                _fail(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        path_details = os.stat(
            parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(after) != before_identity
            or _file_identity(path_details) != before_identity
        ):
            _fail("ai_panorama_permit_file_changed")
        return _StableFile(data=b"".join(chunks), identity=before_identity)
    except AiPanoramaInstallPermitError:
        raise
    except OSError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _read_absolute_regular(
    path: Path,
    *,
    code: str,
    maximum_bytes: int,
    required_uid: int,
    exact_mode: int,
) -> _StableFile:
    if not path.is_absolute() or Path(os.path.abspath(path)) != path or path.name in {"", ".", ".."}:
        _fail(code)
    parent_descriptor = _open_absolute_directory(path.parent, code=code)
    try:
        return _read_relative_regular(
            parent_descriptor,
            path.name,
            code=code,
            maximum_bytes=maximum_bytes,
            required_uid=required_uid,
            exact_mode=exact_mode,
        )
    finally:
        os.close(parent_descriptor)


def _controller_paths_sha256() -> str:
    _require_secure_open_primitives()
    paths = _CONTROLLER_PATHS
    for path in (
        paths.control_root,
        paths.permit_root,
        paths.ledger_path,
        paths.ledger_lock_path,
        paths.volume_profile_path,
    ):
        if not path.is_absolute() or Path(os.path.abspath(path)) != path:
            _fail("ai_panorama_controller_path_invalid")
    if (
        paths.permit_root.parent != paths.control_root
        or paths.ledger_path.parent != paths.control_root
        or paths.ledger_lock_path.parent != paths.control_root
        or isinstance(paths.required_uid, bool)
        or not isinstance(paths.required_uid, int)
        or paths.required_uid < 0
    ):
        _fail("ai_panorama_controller_path_invalid")
    return _sha256(
        _canonical_bytes(
            {
                "control_root": str(paths.control_root),
                "permit_root": str(paths.permit_root),
                "ledger_path": str(paths.ledger_path),
                "ledger_lock_path": str(paths.ledger_lock_path),
                "volume_profile_path": str(paths.volume_profile_path),
                "required_uid": paths.required_uid,
            }
        )
    )


def _load_volume_profile(environment: str) -> _VolumeProfile:
    paths = _CONTROLLER_PATHS
    stable = _read_absolute_regular(
        paths.volume_profile_path,
        code="ai_panorama_volume_profile_unavailable",
        maximum_bytes=64 * 1024,
        required_uid=paths.required_uid,
        exact_mode=EXTERNAL_PROFILE_MODE,
    )
    payload = _strict_json(stable.data, "ai_panorama_volume_profile_invalid")
    _exact_keys(
        payload,
        {
            "schema",
            "version",
            "authority",
            "status",
            "environment",
            "volume_id",
            "logical_purpose",
            "application_setting",
            "application_setting_value",
            "storage_kind",
            "docker_volume_name",
            "container_mount_source",
            "container_mount_target",
            "runtime_uid",
            "runtime_gid",
            "artifact_root",
            "artifact_root_device",
            "artifact_root_inode",
            "public_tour_root",
            "public_tour_root_device",
            "public_tour_root_inode",
        },
        "ai_panorama_volume_profile_invalid",
    )
    if (
        payload["schema"] != VOLUME_PROFILE_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["authority"] != LEDGER_AUTHORITY
        or payload["status"] != "active"
        or payload["environment"] != environment
        or payload["logical_purpose"] != "public-tours"
        or payload["application_setting"] != CANONICAL_PUBLIC_TOUR_SETTING
        or payload["application_setting_value"]
        != CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        or payload["storage_kind"] != CANONICAL_PUBLIC_TOUR_STORAGE_KIND
        or payload["docker_volume_name"]
        != CANONICAL_PUBLIC_TOUR_VOLUME_NAME
        or payload["container_mount_target"]
        != CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        or payload["runtime_uid"] != CANONICAL_PUBLIC_TOUR_RUNTIME_UID
        or payload["runtime_gid"] != CANONICAL_PUBLIC_TOUR_RUNTIME_GID
    ):
        _fail("ai_panorama_volume_profile_invalid")
    volume_id = _safe_id(payload["volume_id"], "ai_panorama_volume_profile_invalid")
    artifact_root = Path(_string(payload["artifact_root"], "ai_panorama_volume_profile_invalid"))
    public_root = Path(_string(payload["public_tour_root"], "ai_panorama_volume_profile_invalid"))
    mount_source = Path(
        _string(
            payload["container_mount_source"],
            "ai_panorama_volume_profile_invalid",
        )
    )
    if mount_source != public_root:
        _fail("ai_panorama_volume_profile_invalid")
    for value in (
        payload["artifact_root_device"],
        payload["artifact_root_inode"],
        payload["public_tour_root_device"],
        payload["public_tour_root_inode"],
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            _fail("ai_panorama_volume_profile_invalid")

    artifact_descriptor = _open_absolute_directory(
        artifact_root,
        code="ai_panorama_artifact_root_invalid",
    )
    public_descriptor = _open_absolute_directory(
        public_root,
        code="ai_panorama_public_tour_root_invalid",
    )
    try:
        artifact_details = os.fstat(artifact_descriptor)
        public_details = os.fstat(public_descriptor)
        artifact_identity = _directory_identity(artifact_details)
        public_identity = _directory_identity(public_details)
        if (
            artifact_details.st_dev != payload["artifact_root_device"]
            or artifact_details.st_ino != payload["artifact_root_inode"]
            or public_details.st_dev != payload["public_tour_root_device"]
            or public_details.st_ino != payload["public_tour_root_inode"]
            or artifact_details.st_uid != paths.required_uid
            or public_details.st_uid != CANONICAL_PUBLIC_TOUR_RUNTIME_UID
            or public_details.st_gid != CANONICAL_PUBLIC_TOUR_RUNTIME_GID
            or stat.S_IMODE(artifact_details.st_mode) & 0o022
            or stat.S_IMODE(public_details.st_mode) & 0o022
        ):
            _fail("ai_panorama_volume_identity_mismatch")
    finally:
        os.close(public_descriptor)
        os.close(artifact_descriptor)
    return _VolumeProfile(
        environment=environment,
        artifact_root=artifact_root,
        artifact_root_identity=artifact_identity,
        public_tour_root=public_root,
        public_tour_root_identity=public_identity,
        public_tour_volume_name=CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        public_tour_mount_target=CANONICAL_PUBLIC_TOUR_MOUNT_TARGET,
        volume_id=volume_id,
        sha256=_sha256(stable.data),
    )


def _validate_expected(expected: AiPanoramaInstallExpectedBindings) -> None:
    if type(expected) is not AiPanoramaInstallExpectedBindings:
        _fail("ai_panorama_expected_bindings_invalid")
    _string(expected.subject, "ai_panorama_subject_invalid", maximum=512)
    _string(
        expected.authenticated_principal_id,
        "ai_panorama_principal_invalid",
        maximum=512,
    )
    for value, code in (
        (expected.search_run_id, "ai_panorama_run_invalid"),
        (expected.candidate_ref, "ai_panorama_candidate_invalid"),
        (expected.external_id, "ai_panorama_external_id_invalid"),
        (expected.provider_key, "ai_panorama_provider_invalid"),
        (expected.request_id, "ai_panorama_request_id_invalid"),
        (expected.environment, "ai_panorama_environment_invalid"),
        (expected.job, "ai_panorama_job_invalid"),
    ):
        _safe_id(value, code)
    _https_url(expected.listing_url, "ai_panorama_listing_url_invalid")
    _https_url(expected.source_ref, "ai_panorama_source_ref_invalid")
    if _SLUG_RE.fullmatch(expected.expected_slug) is None:
        _fail("ai_panorama_slug_invalid")
    for value, code in (
        (expected.expected_source_tree_sha256, "ai_panorama_source_tree_digest_invalid"),
        (expected.expected_tour_sha256, "ai_panorama_tour_digest_invalid"),
        (expected.expected_core_manifest_sha256, "ai_panorama_core_manifest_digest_invalid"),
        (
            expected.expected_materialization_receipt_sha256,
            "ai_panorama_materialization_receipt_digest_invalid",
        ),
        (expected.expected_candidate_marker_sha256, "ai_panorama_marker_digest_invalid"),
        (
            expected.expected_publication_record_sha256,
            "ai_panorama_publication_record_digest_invalid",
        ),
        (expected.review_receipt_sha256, "ai_panorama_review_receipt_digest_invalid"),
    ):
        _digest(value, code)
    _safe_relpath(expected.artifact_relpath, "ai_panorama_artifact_relpath_invalid")
    _safe_relpath(
        expected.materialization_receipt_relpath,
        "ai_panorama_materialization_receipt_relpath_invalid",
    )
    if (
        _REPOSITORY_RE.fullmatch(expected.repository) is None
        or not expected.git_ref.startswith("refs/")
        or _GIT_SHA_RE.fullmatch(expected.git_head_sha) is None
    ):
        _fail("ai_panorama_release_context_invalid")
    _string(expected.git_ref, "ai_panorama_release_context_invalid", maximum=512)
    _string(expected.workflow_ref, "ai_panorama_release_context_invalid", maximum=512)


def _expected_payload(expected: AiPanoramaInstallExpectedBindings) -> dict[str, str]:
    return {
        "audience": PERMIT_AUDIENCE,
        "issuer": PERMIT_ISSUER,
        "operation": PERMIT_OPERATION,
        "subject": expected.subject,
        "authenticated_principal_id": expected.authenticated_principal_id,
        "search_run_id": expected.search_run_id,
        "candidate_ref": expected.candidate_ref,
        "external_id": expected.external_id,
        "listing_url": expected.listing_url,
        "source_ref": expected.source_ref,
        "provider_key": expected.provider_key,
        "expected_slug": expected.expected_slug,
        "expected_source_tree_sha256": expected.expected_source_tree_sha256,
        "expected_tour_sha256": expected.expected_tour_sha256,
        "expected_core_manifest_sha256": expected.expected_core_manifest_sha256,
        "expected_materialization_receipt_sha256": (
            expected.expected_materialization_receipt_sha256
        ),
        "expected_candidate_marker_sha256": expected.expected_candidate_marker_sha256,
        "expected_publication_record_sha256": (
            expected.expected_publication_record_sha256
        ),
        "artifact_relpath": expected.artifact_relpath,
        "materialization_receipt_relpath": (
            expected.materialization_receipt_relpath
        ),
        "request_id": expected.request_id,
        "repository": expected.repository,
        "git_ref": expected.git_ref,
        "git_head_sha": expected.git_head_sha,
        "workflow_ref": expected.workflow_ref,
        "job": expected.job,
        "environment": expected.environment,
        "review_receipt_sha256": expected.review_receipt_sha256,
    }


def _signature_preimage(envelope: Mapping[str, Any]) -> bytes:
    signature = envelope["signature"]
    body = {
        "domain": SIGNATURE_DOMAIN.decode("ascii").rstrip("\0"),
        "schema": envelope["schema"],
        "version": envelope["version"],
        "permit": envelope["permit"],
        "signature_context": {
            "algorithm": signature["algorithm"],
            "key_id": signature["key_id"],
            "encoding": signature["encoding"],
        },
    }
    canonical = _canonical_bytes(body)
    return SIGNATURE_DOMAIN + len(canonical).to_bytes(8, "big") + canonical


def _decode_signature(value: Any) -> bytes:
    text = _string(value, "ai_panorama_signature_invalid", maximum=128)
    if re.fullmatch(r"[A-Za-z0-9_-]+", text) is None:
        _fail("ai_panorama_signature_invalid")
    try:
        decoded = base64.b64decode(
            text + "=" * ((4 - len(text) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise AiPanoramaInstallPermitError("ai_panorama_signature_invalid") from exc
    if (
        len(decoded) != 64
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != text
    ):
        _fail("ai_panorama_signature_invalid")
    return decoded


def _load_permit(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> tuple[
    dict[str, Any],
    _StableFile,
    str,
    str,
    release_keyring.TrustedDrainKey,
]:
    normalized = _safe_relpath(permit_relpath, "ai_panorama_permit_relpath_invalid")
    permit_root_descriptor = _open_absolute_directory(
        _CONTROLLER_PATHS.permit_root,
        code="ai_panorama_permit_root_invalid",
    )
    try:
        root_details = os.fstat(permit_root_descriptor)
        if (
            root_details.st_uid != _CONTROLLER_PATHS.required_uid
            or stat.S_IMODE(root_details.st_mode) & 0o077
        ):
            _fail("ai_panorama_permit_root_invalid")
        stable = _read_relative_regular(
            permit_root_descriptor,
            normalized,
            code="ai_panorama_permit_file_invalid",
            maximum_bytes=MAX_PERMIT_BYTES,
            required_uid=_CONTROLLER_PATHS.required_uid,
        )
    finally:
        os.close(permit_root_descriptor)
    envelope = _strict_json(stable.data, "ai_panorama_permit_json_invalid")
    if stable.data != _canonical_bytes(envelope) + b"\n":
        _fail("ai_panorama_permit_not_canonical")
    _exact_keys(
        envelope,
        {"schema", "version", "permit", "signature"},
        "ai_panorama_permit_envelope_invalid",
    )
    if (
        envelope["schema"] != PERMIT_SCHEMA
        or type(envelope["version"]) is not int
        or envelope["version"] != PERMIT_VERSION
        or not isinstance(envelope["permit"], dict)
        or not isinstance(envelope["signature"], dict)
    ):
        _fail("ai_panorama_permit_envelope_invalid")
    signature = envelope["signature"]
    _exact_keys(
        signature,
        {"algorithm", "key_id", "encoding", "value"},
        "ai_panorama_signature_invalid",
    )
    if signature["algorithm"] != "Ed25519" or signature["encoding"] != "base64url":
        _fail("ai_panorama_signature_invalid")
    key_id = _safe_id(signature["key_id"], "ai_panorama_signature_key_invalid")

    permit = envelope["permit"]
    expected_keys = set(_expected_payload(expected)) | {
        "issued_at",
        "expires_at",
        "nonce",
    }
    _exact_keys(permit, expected_keys, "ai_panorama_permit_fields_invalid")
    expected_payload = _expected_payload(expected)
    for key, value in expected_payload.items():
        if type(permit.get(key)) is not str or permit[key] != value:
            _fail("ai_panorama_permit_binding_mismatch")
    nonce = _string(permit["nonce"], "ai_panorama_nonce_invalid", maximum=32)
    if _NONCE_RE.fullmatch(nonce) is None:
        _fail("ai_panorama_nonce_invalid")
    issued = _timestamp(permit["issued_at"], "ai_panorama_issued_at_invalid")
    expires = _timestamp(permit["expires_at"], "ai_panorama_expires_at_invalid")
    checked_at = _utc_now().astimezone(timezone.utc)
    if (
        expires <= issued
        or (expires - issued).total_seconds() > MAX_TTL_SECONDS
        or issued > checked_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
        or checked_at >= expires
    ):
        _fail("ai_panorama_permit_not_fresh")

    try:
        trusted = release_keyring.select_trusted_key(key_id, at=checked_at)
        public_key = Ed25519PublicKey.from_public_bytes(trusted.public_key)
    except Exception as exc:
        raise AiPanoramaInstallPermitError(
            "ai_panorama_release_key_untrusted"
        ) from exc
    preimage = _signature_preimage(envelope)
    try:
        public_key.verify(_decode_signature(signature["value"]), preimage)
    except InvalidSignature as exc:
        raise AiPanoramaInstallPermitError(
            "ai_panorama_signature_invalid"
        ) from exc
    return envelope, stable, _sha256(stable.data), _sha256(preimage), trusted


def _bind_artifacts(
    expected: AiPanoramaInstallExpectedBindings,
    profile: _VolumeProfile,
) -> tuple[Path, tuple[int, ...], Path, tuple[int, ...]]:
    artifact_root_descriptor = _open_absolute_directory(
        profile.artifact_root,
        code="ai_panorama_artifact_root_invalid",
    )
    try:
        root_details = os.fstat(artifact_root_descriptor)
        if _directory_identity(root_details) != profile.artifact_root_identity:
            _fail("ai_panorama_volume_identity_mismatch")
        source_descriptor, source_identity = _open_relative_directory(
            artifact_root_descriptor,
            expected.artifact_relpath,
            code="ai_panorama_source_bundle_invalid",
            required_uid=_CONTROLLER_PATHS.required_uid,
            forbid_writable=True,
        )
        os.close(source_descriptor)
        receipt = _read_relative_regular(
            artifact_root_descriptor,
            expected.materialization_receipt_relpath,
            code="ai_panorama_materialization_receipt_invalid",
            maximum_bytes=MAX_RECEIPT_BYTES,
            required_uid=_CONTROLLER_PATHS.required_uid,
        )
    finally:
        os.close(artifact_root_descriptor)
    if _sha256(receipt.data) != expected.expected_materialization_receipt_sha256:
        _fail("ai_panorama_materialization_receipt_digest_mismatch")
    source_bundle = Path(
        os.path.abspath(profile.artifact_root / expected.artifact_relpath)
    )
    receipt_path = Path(
        os.path.abspath(
            profile.artifact_root / expected.materialization_receipt_relpath
        )
    )
    return source_bundle, source_identity, receipt_path, receipt.identity


def _unconsumed_admission(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaInstallAdmission:
    _validate_expected(expected)
    controller_paths_sha256 = _controller_paths_sha256()
    profile = _load_volume_profile(expected.environment)
    envelope, stable, permit_sha256, preimage_sha256, trusted = _load_permit(
        permit_relpath,
        expected,
    )
    source_bundle, source_identity, receipt_path, receipt_identity = _bind_artifacts(
        expected,
        profile,
    )
    permit = envelope["permit"]
    context_sha256 = _sha256(_canonical_bytes(permit))
    return VerifiedAiPanoramaInstallAdmission(
        operation=PERMIT_OPERATION,
        subject=expected.subject,
        authenticated_principal_id=expected.authenticated_principal_id,
        search_run_id=expected.search_run_id,
        candidate_ref=expected.candidate_ref,
        external_id=expected.external_id,
        listing_url=expected.listing_url,
        source_ref=expected.source_ref,
        provider_key=expected.provider_key,
        expected_slug=expected.expected_slug,
        expected_source_tree_sha256=expected.expected_source_tree_sha256,
        expected_tour_sha256=expected.expected_tour_sha256,
        expected_core_manifest_sha256=expected.expected_core_manifest_sha256,
        expected_materialization_receipt_sha256=(
            expected.expected_materialization_receipt_sha256
        ),
        expected_candidate_marker_sha256=expected.expected_candidate_marker_sha256,
        expected_publication_record_sha256=(
            expected.expected_publication_record_sha256
        ),
        source_bundle=source_bundle,
        materialization_receipt_path=receipt_path,
        incoming_root=profile.artifact_root,
        public_tour_dir=profile.public_tour_root,
        public_tour_volume_name=profile.public_tour_volume_name,
        public_tour_mount_target=profile.public_tour_mount_target,
        public_tour_root_device=profile.public_tour_root_identity[0],
        public_tour_root_inode=profile.public_tour_root_identity[1],
        public_control_url=(
            f"{CANONICAL_PUBLIC_ORIGIN}/tours/{expected.expected_slug}/control"
        ),
        artifact_relpath=expected.artifact_relpath,
        materialization_receipt_relpath=(
            expected.materialization_receipt_relpath
        ),
        permit_sha256=permit_sha256,
        request_id=expected.request_id,
        nonce=permit["nonce"],
        repository=expected.repository,
        git_ref=expected.git_ref,
        git_head_sha=expected.git_head_sha,
        workflow_ref=expected.workflow_ref,
        job=expected.job,
        environment=expected.environment,
        review_receipt_sha256=expected.review_receipt_sha256,
        issued_at=permit["issued_at"],
        expires_at=permit["expires_at"],
        key_id=trusted.key_id,
        key_epoch=trusted.epoch,
        key_sha256=trusted.public_key_sha256,
        volume_profile_sha256=profile.sha256,
        permit_verified=True,
        nonce_consumed=False,
        _permit_relpath=_safe_relpath(
            permit_relpath,
            "ai_panorama_permit_relpath_invalid",
        ),
        _permit_file_identity=stable.identity,
        _source_bundle_identity=source_identity,
        _materialization_receipt_identity=receipt_identity,
        _controller_paths_sha256=controller_paths_sha256,
        _signed_preimage_sha256=preimage_sha256,
        _context_sha256=context_sha256,
        _ledger_instance_id="",
        _ledger_sequence=0,
        _ledger_entry_sha256="",
    )


def _expected_from_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
) -> AiPanoramaInstallExpectedBindings:
    return AiPanoramaInstallExpectedBindings(
        subject=admission.subject,
        authenticated_principal_id=admission.authenticated_principal_id,
        search_run_id=admission.search_run_id,
        candidate_ref=admission.candidate_ref,
        external_id=admission.external_id,
        listing_url=admission.listing_url,
        source_ref=admission.source_ref,
        provider_key=admission.provider_key,
        expected_slug=admission.expected_slug,
        expected_source_tree_sha256=admission.expected_source_tree_sha256,
        expected_tour_sha256=admission.expected_tour_sha256,
        expected_core_manifest_sha256=admission.expected_core_manifest_sha256,
        expected_materialization_receipt_sha256=(
            admission.expected_materialization_receipt_sha256
        ),
        expected_candidate_marker_sha256=admission.expected_candidate_marker_sha256,
        expected_publication_record_sha256=(
            admission.expected_publication_record_sha256
        ),
        artifact_relpath=admission.artifact_relpath,
        materialization_receipt_relpath=(
            admission.materialization_receipt_relpath
        ),
        request_id=admission.request_id,
        repository=admission.repository,
        git_ref=admission.git_ref,
        git_head_sha=admission.git_head_sha,
        workflow_ref=admission.workflow_ref,
        job=admission.job,
        environment=admission.environment,
        review_receipt_sha256=admission.review_receipt_sha256,
    )


def _empty_ledger_shape(value: Mapping[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema",
            "authority",
            "instance_id",
            "sequence",
            "tip_sha256",
            "entries",
        },
        "ai_panorama_ledger_invalid",
    )
    if (
        value["schema"] != LEDGER_SCHEMA
        or value["authority"] != LEDGER_AUTHORITY
        or _NONCE_RE.fullmatch(
            _string(
                value["instance_id"],
                "ai_panorama_ledger_invalid",
                maximum=32,
            )
        )
        is None
        or isinstance(value["sequence"], bool)
        or not isinstance(value["sequence"], int)
        or value["sequence"] < 0
        or value["sequence"] > MAX_LEDGER_ENTRIES
        or not isinstance(value["entries"], list)
        or len(value["entries"]) != value["sequence"]
    ):
        _fail("ai_panorama_ledger_invalid")


def _entry_digest(entry_without_digest: Mapping[str, Any]) -> str:
    return _sha256(
        b"propertyquarry.ai-panorama-install-ledger-entry.v1\0"
        + _canonical_bytes(entry_without_digest)
    )


def _validate_ledger(value: Mapping[str, Any]) -> dict[str, Any]:
    _empty_ledger_shape(value)
    previous = _GENESIS_DIGEST
    request_ids: set[str] = set()
    nonce_hashes: set[str] = set()
    permit_hashes: set[str] = set()
    for index, raw_entry in enumerate(value["entries"], start=1):
        if not isinstance(raw_entry, dict):
            _fail("ai_panorama_ledger_invalid")
        _exact_keys(
            raw_entry,
            {
                "sequence",
                "permit_sha256",
                "request_id",
                "nonce_sha256",
                "context_sha256",
                "signed_preimage_sha256",
                "key_id",
                "key_epoch",
                "consumed_at",
                "previous_entry_sha256",
                "entry_sha256",
            },
            "ai_panorama_ledger_invalid",
        )
        if (
            type(raw_entry["sequence"]) is not int
            or raw_entry["sequence"] != index
            or type(raw_entry["key_epoch"]) is not int
            or raw_entry["key_epoch"] < 1
            or raw_entry["previous_entry_sha256"] != previous
        ):
            _fail("ai_panorama_ledger_invalid")
        for key in (
            "permit_sha256",
            "nonce_sha256",
            "context_sha256",
            "signed_preimage_sha256",
            "previous_entry_sha256",
            "entry_sha256",
        ):
            _digest(raw_entry[key], "ai_panorama_ledger_invalid")
        request_id = _safe_id(
            raw_entry["request_id"],
            "ai_panorama_ledger_invalid",
        )
        _safe_id(raw_entry["key_id"], "ai_panorama_ledger_invalid")
        _timestamp(raw_entry["consumed_at"], "ai_panorama_ledger_invalid")
        unsigned = dict(raw_entry)
        claimed = unsigned.pop("entry_sha256")
        if claimed != _entry_digest(unsigned):
            _fail("ai_panorama_ledger_invalid")
        if (
            request_id in request_ids
            or raw_entry["nonce_sha256"] in nonce_hashes
            or raw_entry["permit_sha256"] in permit_hashes
        ):
            _fail("ai_panorama_ledger_invalid")
        request_ids.add(request_id)
        nonce_hashes.add(raw_entry["nonce_sha256"])
        permit_hashes.add(raw_entry["permit_sha256"])
        previous = claimed
    if value["tip_sha256"] != previous:
        _fail("ai_panorama_ledger_invalid")
    return dict(value)


def _open_control_root() -> int:
    descriptor = _open_absolute_directory(
        _CONTROLLER_PATHS.control_root,
        code="ai_panorama_control_root_invalid",
    )
    details = os.fstat(descriptor)
    if (
        details.st_uid != _CONTROLLER_PATHS.required_uid
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        os.close(descriptor)
        _fail("ai_panorama_control_root_invalid")
    return descriptor


def _open_locked_ledger() -> tuple[int, int, dict[str, Any], tuple[int, ...]]:
    control_descriptor = _open_control_root()
    lock_descriptor = -1
    try:
        lock_stable = _read_relative_regular(
            control_descriptor,
            _CONTROLLER_PATHS.ledger_lock_path.name,
            code="ai_panorama_ledger_lock_invalid",
            maximum_bytes=64,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=CONTROLLER_FILE_MODE,
        )
        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        lock_descriptor = os.open(
            _CONTROLLER_PATHS.ledger_lock_path.name,
            flags,
            dir_fd=control_descriptor,
        )
        if _file_identity(os.fstat(lock_descriptor)) != lock_stable.identity:
            _fail("ai_panorama_ledger_lock_invalid")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current_lock = os.stat(
            _CONTROLLER_PATHS.ledger_lock_path.name,
            dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(current_lock) != lock_stable.identity:
            _fail("ai_panorama_ledger_lock_invalid")
        ledger_stable = _read_relative_regular(
            control_descriptor,
            _CONTROLLER_PATHS.ledger_path.name,
            code="ai_panorama_ledger_unavailable",
            maximum_bytes=16 * 1024 * 1024,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=CONTROLLER_FILE_MODE,
        )
        ledger = _validate_ledger(
            _strict_json(ledger_stable.data, "ai_panorama_ledger_invalid")
        )
        return (
            control_descriptor,
            lock_descriptor,
            ledger,
            ledger_stable.identity,
        )
    except Exception:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        os.close(control_descriptor)
        raise


def _atomic_replace_ledger(
    control_descriptor: int,
    ledger: Mapping[str, Any],
    expected_identity: tuple[int, ...],
) -> None:
    ledger_name = _CONTROLLER_PATHS.ledger_path.name
    current = os.stat(
        ledger_name,
        dir_fd=control_descriptor,
        follow_symlinks=False,
    )
    if _file_identity(current) != expected_identity:
        _fail("ai_panorama_ledger_changed")
    temporary_name = ""
    temporary_descriptor = -1
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    encoded = _canonical_bytes(ledger) + b"\n"
    try:
        for _ in range(32):
            candidate = f".{ledger_name}.tmp-{secrets.token_hex(8)}"
            try:
                temporary_descriptor = os.open(
                    candidate,
                    flags,
                    CONTROLLER_FILE_MODE,
                    dir_fd=control_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_descriptor < 0:
            _fail("ai_panorama_ledger_write_failed")
        os.fchmod(temporary_descriptor, CONTROLLER_FILE_MODE)
        offset = 0
        while offset < len(encoded):
            written = os.write(temporary_descriptor, encoded[offset:])
            if written <= 0:
                _fail("ai_panorama_ledger_write_failed")
            offset += written
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        current = os.stat(
            ledger_name,
            dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(current) != expected_identity:
            _fail("ai_panorama_ledger_changed")
        os.replace(
            temporary_name,
            ledger_name,
            src_dir_fd=control_descriptor,
            dst_dir_fd=control_descriptor,
        )
        temporary_name = ""
        os.fsync(control_descriptor)
    except AiPanoramaInstallPermitError:
        raise
    except OSError as exc:
        raise AiPanoramaInstallPermitError(
            "ai_panorama_ledger_write_failed"
        ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=control_descriptor)
            except FileNotFoundError:
                pass


def _consumption_entry(
    admission: VerifiedAiPanoramaInstallAdmission,
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    unsigned = {
        "sequence": int(ledger["sequence"]) + 1,
        "permit_sha256": admission.permit_sha256,
        "request_id": admission.request_id,
        "nonce_sha256": _sha256(admission.nonce.encode("ascii")),
        "context_sha256": admission._context_sha256,
        "signed_preimage_sha256": admission._signed_preimage_sha256,
        "key_id": admission.key_id,
        "key_epoch": admission.key_epoch,
        "consumed_at": _format_timestamp(_utc_now()),
        "previous_entry_sha256": ledger["tip_sha256"],
    }
    return {**unsigned, "entry_sha256": _entry_digest(unsigned)}


def _with_consumption(
    admission: VerifiedAiPanoramaInstallAdmission,
    *,
    ledger_instance_id: str,
    entry: Mapping[str, Any],
) -> VerifiedAiPanoramaInstallAdmission:
    values = {
        field: getattr(admission, field)
        for field in admission.__dataclass_fields__
    }
    values.update(
        {
            "nonce_consumed": True,
            "_ledger_instance_id": ledger_instance_id,
            "_ledger_sequence": entry["sequence"],
            "_ledger_entry_sha256": entry["entry_sha256"],
        }
    )
    return VerifiedAiPanoramaInstallAdmission(**values)


def verify_ai_panorama_install_permit(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaInstallAdmission:
    """Verify a permit without consuming it; suitable only for protected dry-run."""

    return _unconsumed_admission(permit_relpath, expected)


def consume_ai_panorama_install_permit(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaInstallAdmission:
    """Atomically verify and consume one permit, returning a revalidatable admission."""

    control_descriptor, lock_descriptor, ledger, ledger_identity = (
        _open_locked_ledger()
    )
    try:
        admission = _unconsumed_admission(permit_relpath, expected)
        nonce_sha256 = _sha256(admission.nonce.encode("ascii"))
        if any(
            entry["request_id"] == admission.request_id
            or entry["nonce_sha256"] == nonce_sha256
            or entry["permit_sha256"] == admission.permit_sha256
            for entry in ledger["entries"]
        ):
            _fail("ai_panorama_permit_replayed")
        entry = _consumption_entry(admission, ledger)
        updated = dict(ledger)
        updated["entries"] = [*ledger["entries"], entry]
        updated["sequence"] = entry["sequence"]
        updated["tip_sha256"] = entry["entry_sha256"]
        _validate_ledger(updated)
        _atomic_replace_ledger(control_descriptor, updated, ledger_identity)
        return _with_consumption(
            admission,
            ledger_instance_id=ledger["instance_id"],
            entry=entry,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def _matching_consumption_entry(
    admission: VerifiedAiPanoramaInstallAdmission,
    ledger: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        ledger["instance_id"] != admission._ledger_instance_id
        or admission._ledger_sequence < 1
        or admission._ledger_sequence > len(ledger["entries"])
    ):
        _fail("ai_panorama_consumption_record_missing")
    entry = ledger["entries"][admission._ledger_sequence - 1]
    if (
        entry["sequence"] != admission._ledger_sequence
        or entry["entry_sha256"] != admission._ledger_entry_sha256
        or entry["permit_sha256"] != admission.permit_sha256
        or entry["request_id"] != admission.request_id
        or entry["nonce_sha256"] != _sha256(admission.nonce.encode("ascii"))
        or entry["context_sha256"] != admission._context_sha256
        or entry["signed_preimage_sha256"] != admission._signed_preimage_sha256
        or entry["key_id"] != admission.key_id
        or entry["key_epoch"] != admission.key_epoch
    ):
        _fail("ai_panorama_consumption_record_mismatch")
    return entry


def revalidate_ai_panorama_install_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
    *,
    require_consumed: bool,
) -> VerifiedAiPanoramaInstallAdmission:
    """Reverify signature, fixed context, artifacts, and optional ledger evidence."""

    if type(require_consumed) is not bool:
        _fail("ai_panorama_consumption_requirement_invalid")
    if type(admission) is not VerifiedAiPanoramaInstallAdmission:
        _fail("ai_panorama_admission_type_invalid")
    if (
        admission.operation != PERMIT_OPERATION
        or admission.permit_verified is not True
        or admission._controller_paths_sha256 != _controller_paths_sha256()
    ):
        _fail("ai_panorama_admission_invalid")
    fresh = _unconsumed_admission(
        admission._permit_relpath,
        _expected_from_admission(admission),
    )
    for field in admission.__dataclass_fields__:
        if field in {
            "nonce_consumed",
            "_ledger_instance_id",
            "_ledger_sequence",
            "_ledger_entry_sha256",
        }:
            continue
        if getattr(fresh, field) != getattr(admission, field):
            _fail("ai_panorama_admission_context_changed")
    if admission.nonce_consumed is not True:
        if require_consumed:
            _fail("ai_panorama_permit_not_consumed")
        if (
            admission._ledger_instance_id
            or admission._ledger_sequence != 0
            or admission._ledger_entry_sha256
        ):
            _fail("ai_panorama_admission_invalid")
        return fresh

    control_descriptor, lock_descriptor, ledger, _ledger_identity = (
        _open_locked_ledger()
    )
    try:
        entry = _matching_consumption_entry(admission, ledger)
        consumed = _with_consumption(
            fresh,
            ledger_instance_id=ledger["instance_id"],
            entry=entry,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)
    if consumed != admission:
        _fail("ai_panorama_admission_context_changed")
    return consumed


def validate_verified_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
) -> VerifiedAiPanoramaInstallAdmission:
    """Revalidate an apply-capable, consumed admission."""

    return revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=True,
    )


def is_verified_ai_panorama_install_admission(
    value: object,
    require_consumed: bool,
) -> bool:
    """Return whether ``value`` survives current trust and ledger revalidation."""

    try:
        revalidate_ai_panorama_install_admission(
            value,  # type: ignore[arg-type]
            require_consumed=require_consumed,
        )
    except (AiPanoramaInstallPermitError, OSError, ValueError):
        return False
    return True
