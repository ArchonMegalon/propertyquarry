"""Controller-owned admission for one-time AI panorama installation.

This module verifies and consumes authority; it never creates it.  A permit is
accepted only when its Ed25519 signature, short lifetime, release context,
candidate identity, content digests, and fixed-root artifact paths all match.
The signing key is selected from a fixed, purpose-specific external keyring.
Replay state lives in a controller-owned ledger plus root-owned exclusive
tombstones, and apply authority expires at the end of a bounded execution
lease.

The returned frozen object is intentionally not authority by construction.
Callers must pass it through :func:`revalidate_ai_panorama_install_admission`
at the mutation boundary.  Revalidation reads the signed permit and the fixed
ledger and tombstones again, so copying the dataclass, toggling a boolean,
deleting the permit or ledger, or selecting another ledger cannot authorize an
install.  Terminal journal recovery returns a separate evidence type that can
never authorize installation.
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
from app.product.property_tour_governed_reservations import (
    GOVERNED_PUBLIC_TOUR_MOUNT_TARGET as CANONICAL_PUBLIC_TOUR_MOUNT_TARGET,
    GOVERNED_PUBLIC_TOUR_VOLUME_NAME as CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
)

PERMIT_SCHEMA = "propertyquarry.ai-panorama-install-permit.v2"
PERMIT_VERSION = 2
PERMIT_AUDIENCE = "propertyquarry-ai-panorama-install-controller"
PERMIT_ISSUER = "propertyquarry-release-control"
PERMIT_OPERATION = "ai-panorama-install"
PERMIT_KEY_USAGE = "propertyquarry.ai-panorama-install-permit.signing.v1"
SIGNATURE_DOMAIN = b"propertyquarry.ai-panorama-install-permit.signature.v2\0"
PERMIT_RELPATH_PREFIX = "prater-ai-panorama-install-"
PERMIT_RELPATH_SUFFIX = ".v2.json"
LEDGER_SCHEMA = "propertyquarry.ai-panorama-install-consumption-ledger.v2"
LEDGER_AUTHORITY = "propertyquarry-release-control"
TOMBSTONE_SCHEMA = "propertyquarry.ai-panorama-install-tombstone.v1"
TRUST_ASSERTION_SCHEMA = "propertyquarry.ai-panorama-install-trust-assertion.v1"
VOLUME_PROFILE_SCHEMA = "propertyquarry.public-tour-volume-profile.v2"
COMPOSE_PLAN_SCHEMA = "propertyquarry.public-tour-compose-plan.v1"
KEYRING_SCHEMA = "propertyquarry.ai-panorama-install-keyring.v1"
PERMIT_FILE_IDENTITY_SCHEMA = (
    "propertyquarry.ai-panorama-install-permit-file-identity.v1"
)
CANONICAL_PUBLIC_TOUR_SETTING = "EA_GOVERNED_PUBLIC_TOUR_DIR"
CANONICAL_PUBLIC_TOUR_STORAGE_KIND = "docker-named-volume"
CANONICAL_PUBLIC_TOUR_VOLUME_ID = (
    "propertyquarry-governed-public-tours-production"
)
CANONICAL_PUBLIC_TOUR_LOGICAL_PURPOSE = "governed-public-tours"
CANONICAL_PUBLIC_TOUR_RUNTIME_UID = 10001
CANONICAL_PUBLIC_TOUR_RUNTIME_GID = 10001
CANONICAL_PUBLIC_ORIGIN = "https://propertyquarry.com"
CANONICAL_WEB_IMAGE_REPOSITORY = (
    "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime"
)
CANONICAL_CANDIDATE_MARKER_RELPATH = (
    "bundle/.propertyquarry-ai-panorama-candidate.json"
)
CANONICAL_REPOSITORY = "ArchonMegalon/propertyquarry"
CANONICAL_GIT_REF = "refs/heads/main"
CANONICAL_WORKFLOW_REF = (
    "ArchonMegalon/propertyquarry/.github/workflows/"
    "smoke-runtime.yml@refs/heads/main"
)
CANONICAL_JOB = "propertyquarry-release-v2"
CANONICAL_ENVIRONMENT = "propertyquarry-production"
CANONICAL_SUBJECT = (
    "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
    "environment:propertyquarry-production"
)

MAX_PERMIT_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_TTL_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
MAX_CONSUMED_EXECUTION_LEASE_SECONDS = 900
MAX_CONSUMPTION_RECOVERY_SECONDS = 24 * 60 * 60
MAX_LEDGER_ENTRIES = 100_000
MAX_TOMBSTONE_BYTES = 16 * 1024
MAX_KEYRING_BYTES = 256 * 1024
MAX_KEYRING_KEYS = 64

CONTROL_ROOT = Path(
    "/var/lib/propertyquarry/release-control/ai-panorama-install"
)
PERMIT_ROOT = CONTROL_ROOT / "permits"
TOMBSTONE_ROOT = CONTROL_ROOT / "tombstones"
SEALED_ARTIFACT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/"
    "ai-panorama-artifacts/prater-v1"
)
LEDGER_PATH = CONTROL_ROOT / "consumption-ledger.v2.json"
LEDGER_LOCK_PATH = CONTROL_ROOT / "consumption-ledger.v2.lock"
RUNTIME_CONTROL_ROOT = Path(
    "/run/propertyquarry-release-control/ai-panorama-install"
)
VOLUME_PROFILE_PATH = RUNTIME_CONTROL_ROOT / (
    "public-tour-volume-profile.v2.json"
)
COMPOSE_PLAN_PATH = RUNTIME_CONTROL_ROOT / (
    "public-tour-compose-plan.v1.json"
)
TRUST_ASSERTION_PATH = RUNTIME_CONTROL_ROOT / (
    "ai-panorama-install-trust-assertion.v1.json"
)
KEYRING_PATH = Path(
    "/etc/propertyquarry/release-control/"
    "ai-panorama-install-keyring.v1.json"
)
CONTROLLER_REQUIRED_UID = 0
CONTROLLER_FILE_MODE = 0o600
EXTERNAL_KEYRING_MODE = 0o444
RUNTIME_PROFILE_MODE = 0o400

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}\Z")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SOURCE_REF_RE = re.compile(
    r"[a-z0-9][a-z0-9_-]{0,63}:"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z"
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
    tombstone_root: Path
    sealed_artifact_root: Path
    ledger_path: Path
    ledger_lock_path: Path
    volume_profile_path: Path
    compose_plan_path: Path
    trust_assertion_path: Path
    keyring_path: Path
    public_tour_runtime_root: Path
    required_uid: int


_CONTROLLER_PATHS = _ControllerPaths(
    control_root=CONTROL_ROOT,
    permit_root=PERMIT_ROOT,
    tombstone_root=TOMBSTONE_ROOT,
    sealed_artifact_root=SEALED_ARTIFACT_ROOT,
    ledger_path=LEDGER_PATH,
    ledger_lock_path=LEDGER_LOCK_PATH,
    volume_profile_path=VOLUME_PROFILE_PATH,
    compose_plan_path=COMPOSE_PLAN_PATH,
    trust_assertion_path=TRUST_ASSERTION_PATH,
    keyring_path=KEYRING_PATH,
    public_tour_runtime_root=Path(CANONICAL_PUBLIC_TOUR_MOUNT_TARGET),
    required_uid=CONTROLLER_REQUIRED_UID,
)


@dataclass(frozen=True, slots=True, repr=False)
class AiPanoramaInstallExpectedBindings:
    """Authenticated request and release context expected by the consumer."""

    subject: str
    actor_principal_id: str
    owner_principal_id: str
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
    # The authenticated protected-workflow candidate SHA
    # (native Identity.CandidateSHA == Config.WorkflowSHA).
    git_head_sha: str
    workflow_ref: str
    job: str
    environment: str
    # Raw SHA-256 of the signed run-succeeded journal receipt.
    review_receipt_sha256: str
    web_image: str
    web_image_id: str
    key_usage: str
    key_id: str
    key_epoch: int
    key_sha256: str
    keyring_sha256: str
    volume_profile_sha256: str
    compose_plan_sha256: str
    volume_id: str
    artifact_root_device: int
    artifact_root_inode: int
    public_tour_root_device: int
    public_tour_root_inode: int
    execution_lease_seconds: int


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAiPanoramaInstallAdmission:
    """Immutable projection of a currently verified signed permit."""

    operation: str
    subject: str
    actor_principal_id: str
    owner_principal_id: str
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
    candidate_marker_path: Path
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
    web_image: str
    web_image_id: str
    issued_at: str
    expires_at: str
    key_id: str
    key_epoch: int
    key_sha256: str
    key_usage: str
    keyring_sha256: str
    volume_profile_sha256: str
    compose_plan_sha256: str
    volume_id: str
    artifact_root_device: int
    artifact_root_inode: int
    execution_lease_seconds: int
    consumed_at: str
    execution_lease_expires_at: str
    permit_verified: bool
    nonce_consumed: bool
    _permit_relpath: str
    _permit_file_identity: tuple[int, ...]
    _permit_file_mount_id: int
    _source_bundle_identity: tuple[int, ...]
    _candidate_marker_identity: tuple[int, ...]
    _materialization_receipt_identity: tuple[int, ...]
    _controller_paths_sha256: str
    _signed_preimage_sha256: str
    _context_sha256: str
    _trust_assertion_sha256: str
    _ledger_instance_id: str
    _ledger_sequence: int
    _ledger_entry_sha256: str
    _request_tombstone_sha256: str
    _nonce_tombstone_sha256: str
    _permit_tombstone_sha256: str

    @property
    def authenticated_principal_id(self) -> str:
        """Compatibility alias for the property owner, never the controller actor."""

        return self.owner_principal_id


@dataclass(frozen=True, slots=True)
class _StableFile:
    data: bytes
    identity: tuple[int, ...]
    mount_id: int


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
    compose_plan_sha256: str
    artifact_root_mount_id: int
    public_tour_root_mount_id: int


@dataclass(frozen=True, slots=True, repr=False)
class AiPanoramaInstallTrustedContext:
    """Read-only projection of the fixed, root-owned controller assertion."""

    subject: str
    actor_principal_id: str
    repository: str
    git_ref: str
    git_head_sha: str
    workflow_ref: str
    job: str
    environment: str
    review_receipt_sha256: str
    web_image: str
    web_image_id: str
    key_usage: str
    key_id: str
    key_epoch: int
    key_sha256: str
    keyring_sha256: str
    volume_profile_sha256: str
    compose_plan_sha256: str
    volume_id: str
    artifact_root_device: int
    artifact_root_inode: int
    public_tour_root_device: int
    public_tour_root_inode: int
    execution_lease_seconds: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _TrustedAiPanoramaInstallKey:
    key_id: str
    epoch: int
    usage: str
    public_key: bytes
    public_key_sha256: str
    activates_at: datetime
    accept_until: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAiPanoramaInstallRecoveryEvidence:
    """Non-install authority proving a recent consumed operation."""

    permit_sha256: str
    request_id_sha256: str
    nonce_sha256: str
    context_sha256: str
    ledger_instance_id: str
    ledger_sequence: int
    ledger_entry_sha256: str
    consumed_at: str
    recovery_expires_at: str


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAiPanoramaHistoricalConsumptionProof:
    """Non-expiring, non-authorizing proof of one durable consumption."""

    permit_sha256: str
    request_id_sha256: str
    nonce_sha256: str
    context_sha256: str
    signed_preimage_sha256: str
    trust_assertion_sha256: str
    ledger_instance_id: str
    ledger_sequence: int
    ledger_entry_sha256: str
    consumed_at: str
    execution_lease_expires_at: str


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


def _oci_digest(value: Any, code: str) -> str:
    text = _string(value, code, maximum=71)
    if _OCI_DIGEST_RE.fullmatch(text) is None:
        _fail(code)
    return text


def _web_image_ref(value: Any, code: str) -> str:
    text = _string(value, code, maximum=256)
    prefix = f"{CANONICAL_WEB_IMAGE_REPOSITORY}@"
    if not text.startswith(prefix):
        _fail(code)
    _oci_digest(text[len(prefix):], code)
    return text


def _safe_relpath(
    value: Any,
    code: str,
    *,
    allow_hidden_leaf: bool = False,
) -> str:
    text = _string(value, code, maximum=512)
    if "\\" in text or text.startswith("/"):
        _fail(code)
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != text
        or any(
            part in {"", ".", ".."}
            or (
                part.startswith(".")
                and not (
                    allow_hidden_leaf
                    and index == len(candidate.parts) - 1
                )
            )
            for index, part in enumerate(candidate.parts)
        )
    ):
        _fail(code)
    return text


def ai_panorama_install_permit_relpath(request_id: str) -> str:
    """Return the sole permit leaf authorized for one controller attempt."""

    if (
        type(request_id) is not str
        or _REQUEST_ID_RE.fullmatch(request_id) is None
    ):
        _fail("ai_panorama_request_id_invalid")
    return f"{PERMIT_RELPATH_PREFIX}{request_id}{PERMIT_RELPATH_SUFFIX}"


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
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


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


def _descriptor_mount_id(descriptor: int, *, code: str) -> int:
    """Return Linux's mount identity for an open descriptor.

    Device numbers alone do not detect bind mounts or same-filesystem nested
    mounts.  The controller is Linux-only and fails closed when procfs cannot
    provide the descriptor's mount ID.
    """

    fdinfo_descriptor = -1
    try:
        fdinfo_descriptor = os.open(
            f"/proc/self/fdinfo/{int(descriptor)}",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        raw = os.read(fdinfo_descriptor, 16 * 1024)
    except OSError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    finally:
        if fdinfo_descriptor >= 0:
            os.close(fdinfo_descriptor)
    try:
        decoded = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    matches = [
        line.partition(":")[2].strip()
        for line in decoded.splitlines()
        if line.startswith("mnt_id:")
    ]
    if len(matches) != 1 or not matches[0].isdigit():
        _fail(code)
    mount_id = int(matches[0])
    if mount_id < 1:
        _fail(code)
    return mount_id


def _descriptor_mount_is_read_only(descriptor: int, *, code: str) -> bool:
    read_only_flag = getattr(os, "ST_RDONLY", 0)
    if not read_only_flag:
        _fail(code)
    try:
        flags = os.fstatvfs(descriptor).f_flag
    except OSError as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    return bool(flags & read_only_flag)


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


def _validated_permit_file_identity(
    value: object,
    *,
    code: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(code)
    fields = {
        "schema",
        "version",
        "permit_relpath",
        "permit_relpath_sha256",
        "permit_sha256",
        "device",
        "inode",
        "mount_id",
        "mode",
        "uid",
        "gid",
        "nlink",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "identity_sha256",
    }
    if (
        set(value) != fields
        or value.get("schema") != PERMIT_FILE_IDENTITY_SCHEMA
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        _fail(code)
    relpath = _safe_relpath(
        value.get("permit_relpath"),
        code,
    )
    request_id = (
        relpath[
            len(PERMIT_RELPATH_PREFIX) : -len(PERMIT_RELPATH_SUFFIX)
        ]
        if relpath.startswith(PERMIT_RELPATH_PREFIX)
        and relpath.endswith(PERMIT_RELPATH_SUFFIX)
        else ""
    )
    if (
        _REQUEST_ID_RE.fullmatch(request_id) is None
        or relpath
        != f"{PERMIT_RELPATH_PREFIX}{request_id}{PERMIT_RELPATH_SUFFIX}"
        or value.get("permit_relpath_sha256")
        != _sha256(relpath.encode("ascii"))
    ):
        _fail(code)
    for field in (
        "permit_relpath_sha256",
        "permit_sha256",
        "identity_sha256",
    ):
        _digest(value.get(field), code)
    for field in (
        "device",
        "inode",
        "mount_id",
        "size_bytes",
    ):
        _positive_int(value.get(field), code)
    for field in ("uid", "gid", "mtime_ns", "ctime_ns"):
        if (
            type(value.get(field)) is not int
            or int(value[field]) < 0
        ):
            _fail(code)
    if (
        type(value.get("mode")) is not int
        or value["mode"] != CONTROLLER_FILE_MODE
        or type(value.get("nlink")) is not int
        or value["nlink"] != 1
        or value["uid"] != _CONTROLLER_PATHS.required_uid
    ):
        _fail(code)
    unsigned = dict(value)
    claimed = unsigned.pop("identity_sha256")
    expected_digest = _sha256(
        b"propertyquarry.ai-panorama-install-permit-file-identity.v1\0"
        + _canonical_bytes(unsigned)
    )
    if claimed != expected_digest:
        _fail(code)
    return dict(value)


def _permit_file_identity_payload(
    admission: VerifiedAiPanoramaInstallAdmission,
) -> dict[str, Any]:
    identity = admission._permit_file_identity
    if (
        len(identity) != 9
        or admission._permit_file_mount_id < 1
    ):
        _fail("ai_panorama_permit_file_identity_invalid")
    unsigned: dict[str, Any] = {
        "schema": PERMIT_FILE_IDENTITY_SCHEMA,
        "version": 1,
        "permit_relpath": admission._permit_relpath,
        "permit_relpath_sha256": _sha256(
            admission._permit_relpath.encode("ascii")
        ),
        "permit_sha256": admission.permit_sha256,
        "device": identity[0],
        "inode": identity[1],
        "mount_id": admission._permit_file_mount_id,
        "mode": stat.S_IMODE(identity[2]),
        "uid": identity[3],
        "gid": identity[4],
        "nlink": identity[5],
        "size_bytes": identity[6],
        "mtime_ns": identity[7],
        "ctime_ns": identity[8],
    }
    value = {
        **unsigned,
        "identity_sha256": _sha256(
            b"propertyquarry.ai-panorama-install-permit-file-identity.v1\0"
            + _canonical_bytes(unsigned)
        ),
    }
    return _validated_permit_file_identity(
        value,
        code="ai_panorama_permit_file_identity_invalid",
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
    required_device: int | None = None,
    required_mount_id: int | None = None,
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
                or (
                    required_device is not None
                    and int(details.st_dev) != required_device
                )
                or (
                    required_mount_id is not None
                    and _descriptor_mount_id(descriptor, code=code)
                    != required_mount_id
                )
            ):
                _fail(code)
        details = os.fstat(descriptor)
        mount_id = _descriptor_mount_id(descriptor, code=code)
        if (
            required_device is not None
            and int(details.st_dev) != required_device
        ) or (
            required_mount_id is not None and mount_id != required_mount_id
        ):
            _fail(code)
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
    required_device: int | None = None,
    required_mount_id: int | None = None,
    allow_hidden_leaf: bool = False,
) -> _StableFile:
    normalized = _safe_relpath(
        relpath,
        code,
        allow_hidden_leaf=allow_hidden_leaf,
    )
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
                or (
                    required_device is not None
                    and int(directory_details.st_dev) != required_device
                )
                or (
                    required_mount_id is not None
                    and _descriptor_mount_id(parent_descriptor, code=code)
                    != required_mount_id
                )
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
            or (
                required_device is not None
                and int(before.st_dev) != required_device
            )
        ):
            _fail(code)
        mount_id = _descriptor_mount_id(descriptor, code=code)
        if required_mount_id is not None and mount_id != required_mount_id:
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
        return _StableFile(
            data=b"".join(chunks),
            identity=before_identity,
            mount_id=mount_id,
        )
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
        paths.tombstone_root,
        paths.sealed_artifact_root,
        paths.ledger_path,
        paths.ledger_lock_path,
        paths.volume_profile_path,
        paths.compose_plan_path,
        paths.trust_assertion_path,
        paths.keyring_path,
    ):
        if not path.is_absolute() or Path(os.path.abspath(path)) != path:
            _fail("ai_panorama_controller_path_invalid")
    if (
        paths.permit_root.parent != paths.control_root
        or paths.tombstone_root.parent != paths.control_root
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
                "tombstone_root": str(paths.tombstone_root),
                "sealed_artifact_root": str(paths.sealed_artifact_root),
                "ledger_path": str(paths.ledger_path),
                "ledger_lock_path": str(paths.ledger_lock_path),
                "volume_profile_path": str(paths.volume_profile_path),
                "compose_plan_path": str(paths.compose_plan_path),
                "trust_assertion_path": str(paths.trust_assertion_path),
                "keyring_path": str(paths.keyring_path),
                "required_uid": paths.required_uid,
            }
        )
    )


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(code)
    return value


def _canonical_external_json(
    stable: _StableFile,
    *,
    code: str,
) -> dict[str, Any]:
    payload = _strict_json(stable.data, code)
    if stable.data != _canonical_bytes(payload) + b"\n":
        _fail(code)
    return payload


def _decode_public_key(value: Any, code: str) -> bytes:
    text = _string(value, code, maximum=64)
    if re.fullmatch(r"[A-Za-z0-9_-]+", text) is None:
        _fail(code)
    try:
        decoded = base64.b64decode(
            text + "=" * ((4 - len(text) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise AiPanoramaInstallPermitError(code) from exc
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        != text
    ):
        _fail(code)
    return decoded


def _load_panorama_install_keyring() -> tuple[
    tuple[_TrustedAiPanoramaInstallKey, ...],
    int,
    str,
]:
    paths = _CONTROLLER_PATHS
    stable = _read_absolute_regular(
        paths.keyring_path,
        code="ai_panorama_release_keyring_unavailable",
        maximum_bytes=MAX_KEYRING_BYTES,
        required_uid=paths.required_uid,
        exact_mode=EXTERNAL_KEYRING_MODE,
    )
    payload = _canonical_external_json(
        stable,
        code="ai_panorama_release_keyring_invalid",
    )
    _exact_keys(
        payload,
        {
            "schema",
            "version",
            "authority",
            "algorithm",
            "status",
            "usage",
            "rotation_epoch",
            "minimum_accepted_epoch",
            "keys",
        },
        "ai_panorama_release_keyring_invalid",
    )
    rotation_epoch = _positive_int(
        payload["rotation_epoch"],
        "ai_panorama_release_keyring_invalid",
    )
    minimum_epoch = _positive_int(
        payload["minimum_accepted_epoch"],
        "ai_panorama_release_keyring_invalid",
    )
    raw_keys = payload["keys"]
    if (
        payload["schema"] != KEYRING_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["authority"] != LEDGER_AUTHORITY
        or payload["algorithm"] != "Ed25519"
        or payload["status"] != "active"
        or payload["usage"] != PERMIT_KEY_USAGE
        or minimum_epoch > rotation_epoch
        or not isinstance(raw_keys, list)
        or not 1 <= len(raw_keys) <= MAX_KEYRING_KEYS
    ):
        _fail("ai_panorama_release_keyring_invalid")

    keys: list[_TrustedAiPanoramaInstallKey] = []
    key_ids: set[str] = set()
    key_epochs: set[int] = set()
    previous_epoch = 0
    for raw_key in raw_keys:
        if not isinstance(raw_key, dict):
            _fail("ai_panorama_release_keyring_invalid")
        _exact_keys(
            raw_key,
            {
                "key_id",
                "epoch",
                "usage",
                "public_key",
                "public_key_sha256",
                "activates_at",
                "accept_until",
                "revoked_at",
            },
            "ai_panorama_release_keyring_invalid",
        )
        key_id = _safe_id(
            raw_key["key_id"],
            "ai_panorama_release_keyring_invalid",
        )
        epoch = _positive_int(
            raw_key["epoch"],
            "ai_panorama_release_keyring_invalid",
        )
        public_key = _decode_public_key(
            raw_key["public_key"],
            "ai_panorama_release_keyring_invalid",
        )
        public_key_sha256 = _digest(
            raw_key["public_key_sha256"],
            "ai_panorama_release_keyring_invalid",
        )
        activates_at = _timestamp(
            raw_key["activates_at"],
            "ai_panorama_release_keyring_invalid",
        )
        accept_until = (
            None
            if raw_key["accept_until"] is None
            else _timestamp(
                raw_key["accept_until"],
                "ai_panorama_release_keyring_invalid",
            )
        )
        revoked_at = (
            None
            if raw_key["revoked_at"] is None
            else _timestamp(
                raw_key["revoked_at"],
                "ai_panorama_release_keyring_invalid",
            )
        )
        if (
            raw_key["usage"] != PERMIT_KEY_USAGE
            or epoch > rotation_epoch
            or key_id in key_ids
            or epoch in key_epochs
            or epoch <= previous_epoch
            or _sha256(public_key) != public_key_sha256
            or (
                accept_until is not None
                and accept_until <= activates_at
            )
            or (
                revoked_at is not None
                and revoked_at < activates_at
            )
        ):
            _fail("ai_panorama_release_keyring_invalid")
        key_ids.add(key_id)
        key_epochs.add(epoch)
        previous_epoch = epoch
        keys.append(
            _TrustedAiPanoramaInstallKey(
                key_id=key_id,
                epoch=epoch,
                usage=raw_key["usage"],
                public_key=public_key,
                public_key_sha256=public_key_sha256,
                activates_at=activates_at,
                accept_until=accept_until,
                revoked_at=revoked_at,
            )
        )
    if (
        keys[-1].epoch != rotation_epoch
        or not any(key.epoch >= minimum_epoch for key in keys)
    ):
        _fail("ai_panorama_release_keyring_invalid")
    return tuple(keys), minimum_epoch, _sha256(stable.data)


def _select_panorama_install_key(
    keys: tuple[_TrustedAiPanoramaInstallKey, ...],
    *,
    minimum_epoch: int,
    key_id: str,
    at: datetime,
) -> _TrustedAiPanoramaInstallKey:
    matches = [key for key in keys if key.key_id == key_id]
    if len(matches) != 1:
        _fail("ai_panorama_release_key_untrusted")
    key = matches[0]
    if (
        key.usage != PERMIT_KEY_USAGE
        or key.epoch < minimum_epoch
        or at < key.activates_at
        or (key.accept_until is not None and at >= key.accept_until)
        or (key.revoked_at is not None and at >= key.revoked_at)
    ):
        _fail("ai_panorama_release_key_untrusted")
    return key


def _load_trust_assertion() -> AiPanoramaInstallTrustedContext:
    paths = _CONTROLLER_PATHS
    stable = _read_absolute_regular(
        paths.trust_assertion_path,
        code="ai_panorama_trust_assertion_unavailable",
        maximum_bytes=64 * 1024,
        required_uid=paths.required_uid,
        exact_mode=RUNTIME_PROFILE_MODE,
    )
    payload = _canonical_external_json(
        stable,
        code="ai_panorama_trust_assertion_invalid",
    )
    expected_keys = {
        "schema",
        "version",
        "authority",
        "status",
        "subject",
        "actor_principal_id",
        "repository",
        "git_ref",
        "git_head_sha",
        "workflow_ref",
        "job",
        "environment",
        "review_receipt_sha256",
        "web_image",
        "web_image_id",
        "key_usage",
        "key_id",
        "key_epoch",
        "key_sha256",
        "keyring_sha256",
        "volume_profile_sha256",
        "compose_plan_sha256",
        "volume_id",
        "artifact_root_device",
        "artifact_root_inode",
        "public_tour_root_device",
        "public_tour_root_inode",
        "execution_lease_seconds",
    }
    _exact_keys(payload, expected_keys, "ai_panorama_trust_assertion_invalid")
    if (
        payload["schema"] != TRUST_ASSERTION_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["authority"] != LEDGER_AUTHORITY
        or payload["status"] != "active"
        or payload["subject"] != CANONICAL_SUBJECT
        or payload["repository"] != CANONICAL_REPOSITORY
        or payload["git_ref"] != CANONICAL_GIT_REF
        or payload["workflow_ref"] != CANONICAL_WORKFLOW_REF
        or payload["job"] != CANONICAL_JOB
        or payload["environment"] != CANONICAL_ENVIRONMENT
        or payload["key_usage"] != PERMIT_KEY_USAGE
        or payload["volume_id"] != CANONICAL_PUBLIC_TOUR_VOLUME_ID
    ):
        _fail("ai_panorama_trust_assertion_invalid")
    actor_principal_id = _safe_id(
        payload["actor_principal_id"],
        "ai_panorama_trust_assertion_invalid",
    )
    key_id = _safe_id(
        payload["key_id"],
        "ai_panorama_trust_assertion_invalid",
    )
    key_epoch = _positive_int(
        payload["key_epoch"],
        "ai_panorama_trust_assertion_invalid",
    )
    git_head_sha = _string(
        payload["git_head_sha"],
        "ai_panorama_trust_assertion_invalid",
        maximum=40,
    )
    if _GIT_SHA_RE.fullmatch(git_head_sha) is None:
        _fail("ai_panorama_trust_assertion_invalid")
    web_image = _web_image_ref(
        payload["web_image"],
        "ai_panorama_trust_assertion_invalid",
    )
    web_image_id = _oci_digest(
        payload["web_image_id"],
        "ai_panorama_trust_assertion_invalid",
    )
    execution_lease_seconds = _positive_int(
        payload["execution_lease_seconds"],
        "ai_panorama_trust_assertion_invalid",
    )
    if execution_lease_seconds > MAX_CONSUMED_EXECUTION_LEASE_SECONDS:
        _fail("ai_panorama_trust_assertion_invalid")
    for key in (
        "review_receipt_sha256",
        "key_sha256",
        "keyring_sha256",
        "volume_profile_sha256",
        "compose_plan_sha256",
    ):
        _digest(payload[key], "ai_panorama_trust_assertion_invalid")
    volume_id = _safe_id(
        payload["volume_id"],
        "ai_panorama_trust_assertion_invalid",
    )
    for key in (
        "artifact_root_device",
        "artifact_root_inode",
        "public_tour_root_device",
        "public_tour_root_inode",
    ):
        _positive_int(payload[key], "ai_panorama_trust_assertion_invalid")
    return AiPanoramaInstallTrustedContext(
        subject=payload["subject"],
        actor_principal_id=actor_principal_id,
        repository=payload["repository"],
        git_ref=payload["git_ref"],
        git_head_sha=git_head_sha,
        workflow_ref=payload["workflow_ref"],
        job=payload["job"],
        environment=payload["environment"],
        review_receipt_sha256=payload["review_receipt_sha256"],
        web_image=web_image,
        web_image_id=web_image_id,
        key_usage=payload["key_usage"],
        key_id=key_id,
        key_epoch=key_epoch,
        key_sha256=payload["key_sha256"],
        keyring_sha256=payload["keyring_sha256"],
        volume_profile_sha256=payload["volume_profile_sha256"],
        compose_plan_sha256=payload["compose_plan_sha256"],
        volume_id=volume_id,
        artifact_root_device=payload["artifact_root_device"],
        artifact_root_inode=payload["artifact_root_inode"],
        public_tour_root_device=payload["public_tour_root_device"],
        public_tour_root_inode=payload["public_tour_root_inode"],
        execution_lease_seconds=execution_lease_seconds,
        sha256=_sha256(stable.data),
    )


def _load_compose_plan(
    expected: AiPanoramaInstallExpectedBindings,
) -> str:
    paths = _CONTROLLER_PATHS
    stable = _read_absolute_regular(
        paths.compose_plan_path,
        code="ai_panorama_compose_plan_unavailable",
        maximum_bytes=1024 * 1024,
        required_uid=paths.required_uid,
        exact_mode=RUNTIME_PROFILE_MODE,
    )
    payload = _canonical_external_json(
        stable,
        code="ai_panorama_compose_plan_invalid",
    )
    _exact_keys(
        payload,
        {
            "schema",
            "version",
            "authority",
            "status",
            "environment",
            "web_image",
            "web_image_id",
            "volume_id",
            "storage_kind",
            "docker_volume_name",
            "container_mount_target",
            "artifact_mount_read_only",
            "web_mount_read_only",
            "publisher_mount_read_write",
            "artifact_root_device",
            "artifact_root_inode",
            "public_tour_root_device",
            "public_tour_root_inode",
        },
        "ai_panorama_compose_plan_invalid",
    )
    if (
        payload["schema"] != COMPOSE_PLAN_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["authority"] != LEDGER_AUTHORITY
        or payload["status"] != "active"
        or payload["environment"] != expected.environment
        or payload["web_image"] != expected.web_image
        or payload["web_image_id"] != expected.web_image_id
        or payload["volume_id"] != expected.volume_id
        or payload["storage_kind"] != CANONICAL_PUBLIC_TOUR_STORAGE_KIND
        or payload["docker_volume_name"]
        != CANONICAL_PUBLIC_TOUR_VOLUME_NAME
        or payload["container_mount_target"]
        != CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        or payload["artifact_mount_read_only"] is not True
        or payload["web_mount_read_only"] is not True
        or payload["publisher_mount_read_write"] is not True
    ):
        _fail("ai_panorama_compose_plan_invalid")
    _web_image_ref(payload["web_image"], "ai_panorama_compose_plan_invalid")
    _oci_digest(payload["web_image_id"], "ai_panorama_compose_plan_invalid")
    for key in (
        "artifact_root_device",
        "artifact_root_inode",
        "public_tour_root_device",
        "public_tour_root_inode",
    ):
        _positive_int(payload[key], "ai_panorama_compose_plan_invalid")
        if payload[key] != getattr(expected, key):
            _fail("ai_panorama_compose_plan_identity_mismatch")
    digest = _sha256(stable.data)
    if digest != expected.compose_plan_sha256:
        _fail("ai_panorama_compose_plan_digest_mismatch")
    return digest


def _load_volume_profile(
    expected: AiPanoramaInstallExpectedBindings,
) -> _VolumeProfile:
    paths = _CONTROLLER_PATHS
    stable = _read_absolute_regular(
        paths.volume_profile_path,
        code="ai_panorama_volume_profile_unavailable",
        maximum_bytes=64 * 1024,
        required_uid=paths.required_uid,
        exact_mode=RUNTIME_PROFILE_MODE,
    )
    payload = _canonical_external_json(
        stable,
        code="ai_panorama_volume_profile_invalid",
    )
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
            "artifact_mount_read_only",
            "public_tour_root",
            "public_tour_root_device",
            "public_tour_root_inode",
            "compose_plan_sha256",
        },
        "ai_panorama_volume_profile_invalid",
    )
    if (
        payload["schema"] != VOLUME_PROFILE_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != 2
        or payload["authority"] != LEDGER_AUTHORITY
        or payload["status"] != "active"
        or payload["environment"] != expected.environment
        or payload["logical_purpose"]
        != CANONICAL_PUBLIC_TOUR_LOGICAL_PURPOSE
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
        or payload["artifact_mount_read_only"] is not True
    ):
        _fail("ai_panorama_volume_profile_invalid")
    volume_id = _safe_id(payload["volume_id"], "ai_panorama_volume_profile_invalid")
    if volume_id != expected.volume_id:
        _fail("ai_panorama_volume_profile_identity_mismatch")
    artifact_root = Path(_string(payload["artifact_root"], "ai_panorama_volume_profile_invalid"))
    public_root = Path(_string(payload["public_tour_root"], "ai_panorama_volume_profile_invalid"))
    mount_source_text = _string(
        payload["container_mount_source"],
        "ai_panorama_volume_profile_invalid",
    )
    mount_source = Path(mount_source_text)
    mount_source_suffix = (
        "volumes",
        CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        "_data",
    )
    if (
        public_root != paths.public_tour_runtime_root
        or public_root == mount_source
        or not mount_source.is_absolute()
        or mount_source_text != os.path.normpath(mount_source_text)
        or mount_source.parts[-3:] != mount_source_suffix
        or any(part in {"", ".", ".."} for part in mount_source.parts[1:])
    ):
        _fail("ai_panorama_volume_profile_invalid")
    if artifact_root != paths.sealed_artifact_root:
        _fail("ai_panorama_volume_profile_invalid")
    for value in (
        payload["artifact_root_device"],
        payload["artifact_root_inode"],
        payload["public_tour_root_device"],
        payload["public_tour_root_inode"],
    ):
        _positive_int(value, "ai_panorama_volume_profile_invalid")
    compose_plan_sha256 = _digest(
        payload["compose_plan_sha256"],
        "ai_panorama_volume_profile_invalid",
    )
    if compose_plan_sha256 != expected.compose_plan_sha256:
        _fail("ai_panorama_compose_plan_digest_mismatch")

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
        artifact_mount_id = _descriptor_mount_id(
            artifact_descriptor,
            code="ai_panorama_volume_identity_mismatch",
        )
        public_mount_id = _descriptor_mount_id(
            public_descriptor,
            code="ai_panorama_volume_identity_mismatch",
        )
        artifact_identity = _directory_identity(artifact_details)
        public_identity = _directory_identity(public_details)
        if not _descriptor_mount_is_read_only(
            artifact_descriptor,
            code="ai_panorama_artifact_root_not_read_only",
        ):
            _fail("ai_panorama_artifact_root_not_read_only")
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
    profile_sha256 = _sha256(stable.data)
    if (
        profile_sha256 != expected.volume_profile_sha256
        or artifact_details.st_dev != expected.artifact_root_device
        or artifact_details.st_ino != expected.artifact_root_inode
        or public_details.st_dev != expected.public_tour_root_device
        or public_details.st_ino != expected.public_tour_root_inode
    ):
        _fail("ai_panorama_volume_profile_identity_mismatch")
    _load_compose_plan(expected)
    return _VolumeProfile(
        environment=expected.environment,
        artifact_root=artifact_root,
        artifact_root_identity=artifact_identity,
        public_tour_root=public_root,
        public_tour_root_identity=public_identity,
        public_tour_volume_name=CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        public_tour_mount_target=CANONICAL_PUBLIC_TOUR_MOUNT_TARGET,
        volume_id=volume_id,
        sha256=profile_sha256,
        compose_plan_sha256=compose_plan_sha256,
        artifact_root_mount_id=artifact_mount_id,
        public_tour_root_mount_id=public_mount_id,
    )


def _validate_expected(expected: AiPanoramaInstallExpectedBindings) -> None:
    if type(expected) is not AiPanoramaInstallExpectedBindings:
        _fail("ai_panorama_expected_bindings_invalid")
    _string(expected.subject, "ai_panorama_subject_invalid", maximum=512)
    _safe_id(expected.actor_principal_id, "ai_panorama_actor_principal_invalid")
    owner_principal_id = _string(
        expected.owner_principal_id,
        "ai_panorama_owner_principal_invalid",
        maximum=512,
    )
    if owner_principal_id == expected.actor_principal_id:
        _fail("ai_panorama_principal_roles_not_separated")
    for value, code in (
        (expected.search_run_id, "ai_panorama_run_invalid"),
        (expected.candidate_ref, "ai_panorama_candidate_invalid"),
        (expected.external_id, "ai_panorama_external_id_invalid"),
        (expected.provider_key, "ai_panorama_provider_invalid"),
        (expected.environment, "ai_panorama_environment_invalid"),
        (expected.job, "ai_panorama_job_invalid"),
        (expected.key_id, "ai_panorama_key_id_invalid"),
        (expected.volume_id, "ai_panorama_volume_id_invalid"),
    ):
        _safe_id(value, code)
    if _REQUEST_ID_RE.fullmatch(expected.request_id) is None:
        _fail("ai_panorama_request_id_invalid")
    _https_url(expected.listing_url, "ai_panorama_listing_url_invalid")
    if _SOURCE_REF_RE.fullmatch(
        _string(
            expected.source_ref,
            "ai_panorama_source_ref_invalid",
            maximum=320,
        )
    ) is None:
        _fail("ai_panorama_source_ref_invalid")
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
        (expected.key_sha256, "ai_panorama_key_digest_invalid"),
        (expected.keyring_sha256, "ai_panorama_keyring_digest_invalid"),
        (expected.volume_profile_sha256, "ai_panorama_volume_profile_digest_invalid"),
        (expected.compose_plan_sha256, "ai_panorama_compose_plan_digest_invalid"),
    ):
        _digest(value, code)
    artifact_relpath = _safe_relpath(
        expected.artifact_relpath,
        "ai_panorama_artifact_relpath_invalid",
    )
    receipt_relpath = _safe_relpath(
        expected.materialization_receipt_relpath,
        "ai_panorama_materialization_receipt_relpath_invalid",
    )
    if (
        artifact_relpath != f"bundle/{expected.expected_slug}"
        or receipt_relpath != "materialization.receipt.json"
    ):
        _fail("ai_panorama_artifact_layout_invalid")
    if _GIT_SHA_RE.fullmatch(expected.git_head_sha) is None:
        _fail("ai_panorama_release_context_invalid")
    _web_image_ref(expected.web_image, "ai_panorama_release_image_invalid")
    _oci_digest(expected.web_image_id, "ai_panorama_release_image_invalid")
    if (
        expected.subject != CANONICAL_SUBJECT
        or expected.repository != CANONICAL_REPOSITORY
        or expected.git_ref != CANONICAL_GIT_REF
        or expected.workflow_ref != CANONICAL_WORKFLOW_REF
        or expected.job != CANONICAL_JOB
        or expected.environment != CANONICAL_ENVIRONMENT
        or expected.key_usage != PERMIT_KEY_USAGE
        or expected.volume_id != CANONICAL_PUBLIC_TOUR_VOLUME_ID
    ):
        _fail("ai_panorama_release_context_invalid")
    _positive_int(expected.key_epoch, "ai_panorama_key_epoch_invalid")
    for value in (
        expected.artifact_root_device,
        expected.artifact_root_inode,
        expected.public_tour_root_device,
        expected.public_tour_root_inode,
    ):
        _positive_int(value, "ai_panorama_volume_identity_invalid")
    lease_seconds = _positive_int(
        expected.execution_lease_seconds,
        "ai_panorama_execution_lease_invalid",
    )
    if lease_seconds > MAX_CONSUMED_EXECUTION_LEASE_SECONDS:
        _fail("ai_panorama_execution_lease_invalid")


def _trusted_context_matches(
    expected: AiPanoramaInstallExpectedBindings,
    trust: AiPanoramaInstallTrustedContext,
) -> None:
    for field in (
        "subject",
        "actor_principal_id",
        "repository",
        "git_ref",
        "git_head_sha",
        "workflow_ref",
        "job",
        "environment",
        "review_receipt_sha256",
        "web_image",
        "web_image_id",
        "key_usage",
        "key_id",
        "key_epoch",
        "key_sha256",
        "keyring_sha256",
        "volume_profile_sha256",
        "compose_plan_sha256",
        "volume_id",
        "artifact_root_device",
        "artifact_root_inode",
        "public_tour_root_device",
        "public_tour_root_inode",
        "execution_lease_seconds",
    ):
        if getattr(expected, field) != getattr(trust, field):
            _fail("ai_panorama_trusted_context_mismatch")


def _expected_payload(expected: AiPanoramaInstallExpectedBindings) -> dict[str, Any]:
    return {
        "audience": PERMIT_AUDIENCE,
        "issuer": PERMIT_ISSUER,
        "operation": PERMIT_OPERATION,
        "subject": expected.subject,
        "actor_principal_id": expected.actor_principal_id,
        "owner_principal_id": expected.owner_principal_id,
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
        "web_image": expected.web_image,
        "web_image_id": expected.web_image_id,
        "key_usage": expected.key_usage,
        "key_epoch": expected.key_epoch,
        "key_sha256": expected.key_sha256,
        "keyring_sha256": expected.keyring_sha256,
        "volume_profile_sha256": expected.volume_profile_sha256,
        "compose_plan_sha256": expected.compose_plan_sha256,
        "volume_id": expected.volume_id,
        "artifact_root_device": expected.artifact_root_device,
        "artifact_root_inode": expected.artifact_root_inode,
        "public_tour_root_device": expected.public_tour_root_device,
        "public_tour_root_inode": expected.public_tour_root_inode,
        "execution_lease_seconds": expected.execution_lease_seconds,
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
    trust: AiPanoramaInstallTrustedContext,
    *,
    allow_expired: bool,
    historical_key_at_issuance_only: bool = False,
) -> tuple[
    dict[str, Any],
    _StableFile,
    str,
    str,
    _TrustedAiPanoramaInstallKey,
]:
    expected_relpath = ai_panorama_install_permit_relpath(
        expected.request_id
    )
    if type(permit_relpath) is not str or permit_relpath != expected_relpath:
        _fail("ai_panorama_permit_relpath_invalid")
    normalized = _safe_relpath(
        permit_relpath,
        "ai_panorama_permit_relpath_invalid",
    )
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
        root_mount_id = _descriptor_mount_id(
            permit_root_descriptor,
            code="ai_panorama_permit_root_invalid",
        )
        stable = _read_relative_regular(
            permit_root_descriptor,
            normalized,
            code="ai_panorama_permit_file_invalid",
            maximum_bytes=MAX_PERMIT_BYTES,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=CONTROLLER_FILE_MODE,
            required_device=int(root_details.st_dev),
            required_mount_id=root_mount_id,
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
        if type(permit.get(key)) is not type(value) or permit[key] != value:
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
        or (not allow_expired and checked_at >= expires)
    ):
        _fail("ai_panorama_permit_not_fresh")

    keys, minimum_epoch, keyring_sha256 = _load_panorama_install_keyring()
    if (
        keyring_sha256 != expected.keyring_sha256
        or keyring_sha256 != trust.keyring_sha256
    ):
        _fail("ai_panorama_release_key_context_mismatch")
    try:
        trusted_at_issuance = _select_panorama_install_key(
            keys,
            minimum_epoch=minimum_epoch,
            key_id=key_id,
            at=issued,
        )
        trusted = trusted_at_issuance
        if not historical_key_at_issuance_only:
            trusted_now = _select_panorama_install_key(
                keys,
                minimum_epoch=minimum_epoch,
                key_id=key_id,
                at=checked_at,
            )
            if (
                trusted_at_issuance.key_id,
                trusted_at_issuance.epoch,
                trusted_at_issuance.usage,
                trusted_at_issuance.public_key_sha256,
                trusted_at_issuance.public_key,
            ) != (
                trusted_now.key_id,
                trusted_now.epoch,
                trusted_now.usage,
                trusted_now.public_key_sha256,
                trusted_now.public_key,
            ):
                _fail("ai_panorama_release_key_context_mismatch")
            trusted = trusted_now
        if (
            key_id != expected.key_id
            or key_id != trust.key_id
            or trusted.usage != expected.key_usage
            or trusted.usage != trust.key_usage
            or trusted.epoch != expected.key_epoch
            or trusted.epoch != trust.key_epoch
            or trusted.public_key_sha256 != expected.key_sha256
            or trusted.public_key_sha256 != trust.key_sha256
        ):
            _fail("ai_panorama_release_key_context_mismatch")
        public_key = Ed25519PublicKey.from_public_bytes(trusted.public_key)
    except AiPanoramaInstallPermitError:
        raise
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
) -> tuple[
    Path,
    tuple[int, ...],
    Path,
    tuple[int, ...],
    Path,
    tuple[int, ...],
]:
    artifact_root_descriptor = _open_absolute_directory(
        profile.artifact_root,
        code="ai_panorama_artifact_root_invalid",
    )
    try:
        root_details = os.fstat(artifact_root_descriptor)
        if _directory_identity(root_details) != profile.artifact_root_identity:
            _fail("ai_panorama_volume_identity_mismatch")
        root_mount_id = _descriptor_mount_id(
            artifact_root_descriptor,
            code="ai_panorama_volume_identity_mismatch",
        )
        if root_mount_id != profile.artifact_root_mount_id:
            _fail("ai_panorama_volume_identity_mismatch")
        source_descriptor, source_identity = _open_relative_directory(
            artifact_root_descriptor,
            expected.artifact_relpath,
            code="ai_panorama_source_bundle_invalid",
            required_uid=_CONTROLLER_PATHS.required_uid,
            forbid_writable=True,
            required_device=int(root_details.st_dev),
            required_mount_id=root_mount_id,
        )
        os.close(source_descriptor)
        marker = _read_relative_regular(
            artifact_root_descriptor,
            CANONICAL_CANDIDATE_MARKER_RELPATH,
            code="ai_panorama_candidate_marker_invalid",
            maximum_bytes=MAX_RECEIPT_BYTES,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=0o400,
            required_device=int(root_details.st_dev),
            required_mount_id=root_mount_id,
            allow_hidden_leaf=True,
        )
        receipt = _read_relative_regular(
            artifact_root_descriptor,
            expected.materialization_receipt_relpath,
            code="ai_panorama_materialization_receipt_invalid",
            maximum_bytes=MAX_RECEIPT_BYTES,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=0o400,
            required_device=int(root_details.st_dev),
            required_mount_id=root_mount_id,
        )
    finally:
        os.close(artifact_root_descriptor)
    if _sha256(receipt.data) != expected.expected_materialization_receipt_sha256:
        _fail("ai_panorama_materialization_receipt_digest_mismatch")
    if _sha256(marker.data) != expected.expected_candidate_marker_sha256:
        _fail("ai_panorama_candidate_marker_digest_mismatch")
    source_bundle = Path(
        os.path.abspath(profile.artifact_root / expected.artifact_relpath)
    )
    marker_path = Path(
        os.path.abspath(
            profile.artifact_root / CANONICAL_CANDIDATE_MARKER_RELPATH
        )
    )
    receipt_path = Path(
        os.path.abspath(
            profile.artifact_root / expected.materialization_receipt_relpath
        )
    )
    return (
        source_bundle,
        source_identity,
        marker_path,
        marker.identity,
        receipt_path,
        receipt.identity,
    )


def _unconsumed_admission(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
    *,
    allow_expired: bool = False,
    historical_key_at_issuance_only: bool = False,
) -> VerifiedAiPanoramaInstallAdmission:
    _validate_expected(expected)
    controller_paths_sha256 = _controller_paths_sha256()
    trust = _load_trust_assertion()
    _trusted_context_matches(expected, trust)
    profile = _load_volume_profile(expected)
    envelope, stable, permit_sha256, preimage_sha256, trusted = _load_permit(
        permit_relpath,
        expected,
        trust,
        allow_expired=allow_expired,
        historical_key_at_issuance_only=(
            historical_key_at_issuance_only
        ),
    )
    (
        source_bundle,
        source_identity,
        marker_path,
        marker_identity,
        receipt_path,
        receipt_identity,
    ) = _bind_artifacts(expected, profile)
    permit = envelope["permit"]
    context_sha256 = _sha256(_canonical_bytes(permit))
    return VerifiedAiPanoramaInstallAdmission(
        operation=PERMIT_OPERATION,
        subject=expected.subject,
        actor_principal_id=expected.actor_principal_id,
        owner_principal_id=expected.owner_principal_id,
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
        candidate_marker_path=marker_path,
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
        web_image=expected.web_image,
        web_image_id=expected.web_image_id,
        issued_at=permit["issued_at"],
        expires_at=permit["expires_at"],
        key_id=trusted.key_id,
        key_epoch=trusted.epoch,
        key_sha256=trusted.public_key_sha256,
        key_usage=expected.key_usage,
        keyring_sha256=expected.keyring_sha256,
        volume_profile_sha256=profile.sha256,
        compose_plan_sha256=profile.compose_plan_sha256,
        volume_id=profile.volume_id,
        artifact_root_device=profile.artifact_root_identity[0],
        artifact_root_inode=profile.artifact_root_identity[1],
        execution_lease_seconds=expected.execution_lease_seconds,
        consumed_at="",
        execution_lease_expires_at="",
        permit_verified=True,
        nonce_consumed=False,
        _permit_relpath=_safe_relpath(
            permit_relpath,
            "ai_panorama_permit_relpath_invalid",
        ),
        _permit_file_identity=stable.identity,
        _permit_file_mount_id=stable.mount_id,
        _source_bundle_identity=source_identity,
        _candidate_marker_identity=marker_identity,
        _materialization_receipt_identity=receipt_identity,
        _controller_paths_sha256=controller_paths_sha256,
        _signed_preimage_sha256=preimage_sha256,
        _context_sha256=context_sha256,
        _trust_assertion_sha256=trust.sha256,
        _ledger_instance_id="",
        _ledger_sequence=0,
        _ledger_entry_sha256="",
        _request_tombstone_sha256="",
        _nonce_tombstone_sha256="",
        _permit_tombstone_sha256="",
    )


def _expected_from_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
) -> AiPanoramaInstallExpectedBindings:
    return AiPanoramaInstallExpectedBindings(
        subject=admission.subject,
        actor_principal_id=admission.actor_principal_id,
        owner_principal_id=admission.owner_principal_id,
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
        web_image=admission.web_image,
        web_image_id=admission.web_image_id,
        key_usage=admission.key_usage,
        key_id=admission.key_id,
        key_epoch=admission.key_epoch,
        key_sha256=admission.key_sha256,
        keyring_sha256=admission.keyring_sha256,
        volume_profile_sha256=admission.volume_profile_sha256,
        compose_plan_sha256=admission.compose_plan_sha256,
        volume_id=admission.volume_id,
        artifact_root_device=admission.artifact_root_device,
        artifact_root_inode=admission.artifact_root_inode,
        public_tour_root_device=admission.public_tour_root_device,
        public_tour_root_inode=admission.public_tour_root_inode,
        execution_lease_seconds=admission.execution_lease_seconds,
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
        b"propertyquarry.ai-panorama-install-ledger-entry.v2\0"
        + _canonical_bytes(entry_without_digest)
    )


def _validate_ledger(value: Mapping[str, Any]) -> dict[str, Any]:
    _empty_ledger_shape(value)
    previous = _GENESIS_DIGEST
    request_hashes: set[str] = set()
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
                "request_id_sha256",
                "nonce_sha256",
                "context_sha256",
                "signed_preimage_sha256",
                "trust_assertion_sha256",
                "key_id",
                "key_epoch",
                "key_sha256",
                "keyring_sha256",
                "key_usage",
                "consumed_at",
                "execution_lease_seconds",
                "execution_lease_expires_at",
                "request_tombstone_sha256",
                "nonce_tombstone_sha256",
                "permit_tombstone_sha256",
                "permit_file_identity",
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
            or type(raw_entry["execution_lease_seconds"]) is not int
            or raw_entry["execution_lease_seconds"] < 1
            or raw_entry["execution_lease_seconds"]
            > MAX_CONSUMED_EXECUTION_LEASE_SECONDS
            or raw_entry["key_usage"] != PERMIT_KEY_USAGE
            or raw_entry["previous_entry_sha256"] != previous
        ):
            _fail("ai_panorama_ledger_invalid")
        for key in (
            "permit_sha256",
            "request_id_sha256",
            "nonce_sha256",
            "context_sha256",
            "signed_preimage_sha256",
            "trust_assertion_sha256",
            "key_sha256",
            "keyring_sha256",
            "request_tombstone_sha256",
            "nonce_tombstone_sha256",
            "permit_tombstone_sha256",
            "previous_entry_sha256",
            "entry_sha256",
        ):
            _digest(raw_entry[key], "ai_panorama_ledger_invalid")
        permit_file_identity = _validated_permit_file_identity(
            raw_entry["permit_file_identity"],
            code="ai_panorama_ledger_invalid",
        )
        if (
            permit_file_identity["permit_sha256"]
            != raw_entry["permit_sha256"]
        ):
            _fail("ai_panorama_ledger_invalid")
        _safe_id(raw_entry["key_id"], "ai_panorama_ledger_invalid")
        consumed_at = _timestamp(
            raw_entry["consumed_at"],
            "ai_panorama_ledger_invalid",
        )
        lease_expires_at = _timestamp(
            raw_entry["execution_lease_expires_at"],
            "ai_panorama_ledger_invalid",
        )
        if (
            lease_expires_at <= consumed_at
            or (
                lease_expires_at - consumed_at
            ).total_seconds()
            != raw_entry["execution_lease_seconds"]
        ):
            _fail("ai_panorama_ledger_invalid")
        unsigned = dict(raw_entry)
        claimed = unsigned.pop("entry_sha256")
        if claimed != _entry_digest(unsigned):
            _fail("ai_panorama_ledger_invalid")
        if (
            raw_entry["request_id_sha256"] in request_hashes
            or raw_entry["nonce_sha256"] in nonce_hashes
            or raw_entry["permit_sha256"] in permit_hashes
        ):
            _fail("ai_panorama_ledger_invalid")
        request_hashes.add(raw_entry["request_id_sha256"])
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
        control_details = os.fstat(control_descriptor)
        control_mount_id = _descriptor_mount_id(
            control_descriptor,
            code="ai_panorama_control_root_invalid",
        )
        lock_stable = _read_relative_regular(
            control_descriptor,
            _CONTROLLER_PATHS.ledger_lock_path.name,
            code="ai_panorama_ledger_lock_invalid",
            maximum_bytes=64,
            required_uid=_CONTROLLER_PATHS.required_uid,
            exact_mode=CONTROLLER_FILE_MODE,
            required_device=int(control_details.st_dev),
            required_mount_id=control_mount_id,
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
        if (
            _descriptor_mount_id(
                lock_descriptor,
                code="ai_panorama_ledger_lock_invalid",
            )
            != control_mount_id
        ):
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
            required_device=int(control_details.st_dev),
            required_mount_id=control_mount_id,
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


def _open_tombstone_root(control_descriptor: int) -> int:
    control_details = os.fstat(control_descriptor)
    control_mount_id = _descriptor_mount_id(
        control_descriptor,
        code="ai_panorama_tombstone_root_invalid",
    )
    descriptor, _identity = _open_relative_directory(
        control_descriptor,
        _CONTROLLER_PATHS.tombstone_root.name,
        code="ai_panorama_tombstone_root_invalid",
        required_uid=_CONTROLLER_PATHS.required_uid,
        forbid_writable=True,
        required_device=int(control_details.st_dev),
        required_mount_id=control_mount_id,
    )
    return descriptor


def _tombstone_payload(
    admission: VerifiedAiPanoramaInstallAdmission,
    *,
    ledger_instance_id: str,
    ledger_sequence: int,
    kind: str,
    value_sha256: str,
    consumed_at: str,
    lease_expires_at: str,
) -> dict[str, Any]:
    return {
        "schema": TOMBSTONE_SCHEMA,
        "version": 1,
        "authority": LEDGER_AUTHORITY,
        "status": "consumed",
        "kind": kind,
        "value_sha256": value_sha256,
        "permit_sha256": admission.permit_sha256,
        "request_id_sha256": _sha256(admission.request_id.encode("ascii")),
        "nonce_sha256": _sha256(admission.nonce.encode("ascii")),
        "context_sha256": admission._context_sha256,
        "signed_preimage_sha256": admission._signed_preimage_sha256,
        "trust_assertion_sha256": admission._trust_assertion_sha256,
        "key_id": admission.key_id,
        "key_epoch": admission.key_epoch,
        "key_sha256": admission.key_sha256,
        "keyring_sha256": admission.keyring_sha256,
        "key_usage": admission.key_usage,
        "ledger_instance_id": ledger_instance_id,
        "ledger_sequence": ledger_sequence,
        "consumed_at": consumed_at,
        "execution_lease_seconds": admission.execution_lease_seconds,
        "execution_lease_expires_at": lease_expires_at,
        "permit_file_identity": _permit_file_identity_payload(admission),
    }


def _tombstone_name(kind: str, value_sha256: str) -> str:
    if kind not in {"request", "nonce", "permit"}:
        _fail("ai_panorama_tombstone_invalid")
    _digest(value_sha256, "ai_panorama_tombstone_invalid")
    return f"{kind}-{value_sha256}.json"


def _write_exclusive_tombstone(
    tombstone_descriptor: int,
    *,
    name: str,
    payload: Mapping[str, Any],
) -> str:
    encoded = _canonical_bytes(payload) + b"\n"
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            CONTROLLER_FILE_MODE,
            dir_fd=tombstone_descriptor,
        )
        os.fchmod(descriptor, CONTROLLER_FILE_MODE)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != _CONTROLLER_PATHS.required_uid
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != CONTROLLER_FILE_MODE
        ):
            _fail("ai_panorama_tombstone_write_failed")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                _fail("ai_panorama_tombstone_write_failed")
            offset += written
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise AiPanoramaInstallPermitError(
            "ai_panorama_permit_replayed"
        ) from exc
    except AiPanoramaInstallPermitError:
        raise
    except OSError as exc:
        raise AiPanoramaInstallPermitError(
            "ai_panorama_tombstone_write_failed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _sha256(encoded)


def _create_consumption_tombstones(
    control_descriptor: int,
    admission: VerifiedAiPanoramaInstallAdmission,
    ledger: Mapping[str, Any],
    *,
    consumed_at: str,
    lease_expires_at: str,
) -> dict[str, str]:
    request_sha256 = _sha256(admission.request_id.encode("ascii"))
    nonce_sha256 = _sha256(admission.nonce.encode("ascii"))
    values = {
        "request": request_sha256,
        "nonce": nonce_sha256,
        "permit": admission.permit_sha256,
    }
    tombstone_descriptor = _open_tombstone_root(control_descriptor)
    written: dict[str, str] = {}
    try:
        for kind, value_sha256 in values.items():
            payload = _tombstone_payload(
                admission,
                ledger_instance_id=ledger["instance_id"],
                ledger_sequence=int(ledger["sequence"]) + 1,
                kind=kind,
                value_sha256=value_sha256,
                consumed_at=consumed_at,
                lease_expires_at=lease_expires_at,
            )
            written[kind] = _write_exclusive_tombstone(
                tombstone_descriptor,
                name=_tombstone_name(kind, value_sha256),
                payload=payload,
            )
        os.fsync(tombstone_descriptor)
    finally:
        os.close(tombstone_descriptor)
    return written


def _validate_consumption_tombstones(
    control_descriptor: int,
    admission: VerifiedAiPanoramaInstallAdmission,
    ledger: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> None:
    values = {
        "request": entry["request_id_sha256"],
        "nonce": entry["nonce_sha256"],
        "permit": entry["permit_sha256"],
    }
    expected_digests = {
        "request": entry["request_tombstone_sha256"],
        "nonce": entry["nonce_tombstone_sha256"],
        "permit": entry["permit_tombstone_sha256"],
    }
    tombstone_descriptor = _open_tombstone_root(control_descriptor)
    try:
        root_details = os.fstat(tombstone_descriptor)
        root_mount_id = _descriptor_mount_id(
            tombstone_descriptor,
            code="ai_panorama_tombstone_invalid",
        )
        for kind, value_sha256 in values.items():
            stable = _read_relative_regular(
                tombstone_descriptor,
                _tombstone_name(kind, value_sha256),
                code="ai_panorama_tombstone_invalid",
                maximum_bytes=MAX_TOMBSTONE_BYTES,
                required_uid=_CONTROLLER_PATHS.required_uid,
                exact_mode=CONTROLLER_FILE_MODE,
                required_device=int(root_details.st_dev),
                required_mount_id=root_mount_id,
            )
            payload = _canonical_external_json(
                stable,
                code="ai_panorama_tombstone_invalid",
            )
            expected_payload = _tombstone_payload(
                admission,
                ledger_instance_id=ledger["instance_id"],
                ledger_sequence=entry["sequence"],
                kind=kind,
                value_sha256=value_sha256,
                consumed_at=entry["consumed_at"],
                lease_expires_at=entry["execution_lease_expires_at"],
            )
            if (
                payload != expected_payload
                or _sha256(stable.data) != expected_digests[kind]
            ):
                _fail("ai_panorama_tombstone_invalid")
    finally:
        os.close(tombstone_descriptor)


def _consumption_entry(
    admission: VerifiedAiPanoramaInstallAdmission,
    ledger: Mapping[str, Any],
    *,
    consumed_at: str,
    lease_expires_at: str,
    tombstones: Mapping[str, str],
) -> dict[str, Any]:
    unsigned = {
        "sequence": int(ledger["sequence"]) + 1,
        "permit_sha256": admission.permit_sha256,
        "request_id_sha256": _sha256(admission.request_id.encode("ascii")),
        "nonce_sha256": _sha256(admission.nonce.encode("ascii")),
        "context_sha256": admission._context_sha256,
        "signed_preimage_sha256": admission._signed_preimage_sha256,
        "trust_assertion_sha256": admission._trust_assertion_sha256,
        "key_id": admission.key_id,
        "key_epoch": admission.key_epoch,
        "key_sha256": admission.key_sha256,
        "keyring_sha256": admission.keyring_sha256,
        "key_usage": admission.key_usage,
        "consumed_at": consumed_at,
        "execution_lease_seconds": admission.execution_lease_seconds,
        "execution_lease_expires_at": lease_expires_at,
        "request_tombstone_sha256": tombstones["request"],
        "nonce_tombstone_sha256": tombstones["nonce"],
        "permit_tombstone_sha256": tombstones["permit"],
        "permit_file_identity": _permit_file_identity_payload(admission),
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
            "consumed_at": entry["consumed_at"],
            "execution_lease_expires_at": entry[
                "execution_lease_expires_at"
            ],
            "_ledger_instance_id": ledger_instance_id,
            "_ledger_sequence": entry["sequence"],
            "_ledger_entry_sha256": entry["entry_sha256"],
            "_request_tombstone_sha256": entry[
                "request_tombstone_sha256"
            ],
            "_nonce_tombstone_sha256": entry["nonce_tombstone_sha256"],
            "_permit_tombstone_sha256": entry[
                "permit_tombstone_sha256"
            ],
        }
    )
    return VerifiedAiPanoramaInstallAdmission(**values)


def load_ai_panorama_install_trusted_context() -> AiPanoramaInstallTrustedContext:
    """Load the current fixed-file context without granting install authority."""

    _controller_paths_sha256()
    return _load_trust_assertion()


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
        request_id_sha256 = _sha256(admission.request_id.encode("ascii"))
        nonce_sha256 = _sha256(admission.nonce.encode("ascii"))
        if any(
            entry["request_id_sha256"] == request_id_sha256
            or entry["nonce_sha256"] == nonce_sha256
            or entry["permit_sha256"] == admission.permit_sha256
            for entry in ledger["entries"]
        ):
            _fail("ai_panorama_permit_replayed")
        consumed_at_value = _utc_now().astimezone(timezone.utc)
        issued_at_value = _timestamp(
            admission.issued_at,
            "ai_panorama_issued_at_invalid",
        )
        expires_at_value = _timestamp(
            admission.expires_at,
            "ai_panorama_expires_at_invalid",
        )
        if not issued_at_value <= consumed_at_value < expires_at_value:
            _fail("ai_panorama_permit_not_fresh")
        lease_expires_value = consumed_at_value + timedelta(
            seconds=admission.execution_lease_seconds
        )
        consumed_at = _format_timestamp(consumed_at_value)
        lease_expires_at = _format_timestamp(lease_expires_value)
        tombstones = _create_consumption_tombstones(
            control_descriptor,
            admission,
            ledger,
            consumed_at=consumed_at,
            lease_expires_at=lease_expires_at,
        )
        entry = _consumption_entry(
            admission,
            ledger,
            consumed_at=consumed_at,
            lease_expires_at=lease_expires_at,
            tombstones=tombstones,
        )
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
        or entry["request_id_sha256"]
        != _sha256(admission.request_id.encode("ascii"))
        or entry["nonce_sha256"] != _sha256(admission.nonce.encode("ascii"))
        or entry["context_sha256"] != admission._context_sha256
        or entry["signed_preimage_sha256"] != admission._signed_preimage_sha256
        or entry["trust_assertion_sha256"]
        != admission._trust_assertion_sha256
        or entry["key_id"] != admission.key_id
        or entry["key_epoch"] != admission.key_epoch
        or entry["key_sha256"] != admission.key_sha256
        or entry["keyring_sha256"] != admission.keyring_sha256
        or entry["key_usage"] != admission.key_usage
        or entry["execution_lease_seconds"]
        != admission.execution_lease_seconds
        or entry["request_tombstone_sha256"]
        != admission._request_tombstone_sha256
        or entry["nonce_tombstone_sha256"]
        != admission._nonce_tombstone_sha256
        or entry["permit_tombstone_sha256"]
        != admission._permit_tombstone_sha256
        or entry["permit_file_identity"]
        != _permit_file_identity_payload(admission)
        or entry["consumed_at"] != admission.consumed_at
        or entry["execution_lease_expires_at"]
        != admission.execution_lease_expires_at
    ):
        _fail("ai_panorama_consumption_record_mismatch")
    return entry


def _validate_consumption_window(
    admission: VerifiedAiPanoramaInstallAdmission,
    entry: Mapping[str, Any],
    *,
    allow_execution_lease_expired: bool,
) -> None:
    consumed_at = _timestamp(
        entry["consumed_at"],
        "ai_panorama_consumption_record_mismatch",
    )
    lease_expires_at = _timestamp(
        entry["execution_lease_expires_at"],
        "ai_panorama_consumption_record_mismatch",
    )
    issued_at = _timestamp(
        admission.issued_at,
        "ai_panorama_consumption_record_mismatch",
    )
    permit_expires_at = _timestamp(
        admission.expires_at,
        "ai_panorama_consumption_record_mismatch",
    )
    checked_at = _utc_now().astimezone(timezone.utc)
    recovery_expires_at = consumed_at + timedelta(
        seconds=MAX_CONSUMPTION_RECOVERY_SECONDS
    )
    if (
        not issued_at <= consumed_at < permit_expires_at
        or lease_expires_at
        != consumed_at
        + timedelta(seconds=admission.execution_lease_seconds)
    ):
        _fail("ai_panorama_consumed_execution_lease_expired")
    if allow_execution_lease_expired:
        if checked_at >= recovery_expires_at:
            _fail("ai_panorama_consumption_recovery_expired")
    elif checked_at >= lease_expires_at:
        _fail("ai_panorama_consumed_execution_lease_expired")


def _recovery_evidence(
    consumed: VerifiedAiPanoramaInstallAdmission,
) -> VerifiedAiPanoramaInstallRecoveryEvidence:
    consumed_at = _timestamp(
        consumed.consumed_at,
        "ai_panorama_consumption_record_mismatch",
    )
    return VerifiedAiPanoramaInstallRecoveryEvidence(
        permit_sha256=consumed.permit_sha256,
        request_id_sha256=_sha256(consumed.request_id.encode("ascii")),
        nonce_sha256=_sha256(consumed.nonce.encode("ascii")),
        context_sha256=consumed._context_sha256,
        ledger_instance_id=consumed._ledger_instance_id,
        ledger_sequence=consumed._ledger_sequence,
        ledger_entry_sha256=consumed._ledger_entry_sha256,
        consumed_at=consumed.consumed_at,
        recovery_expires_at=_format_timestamp(
            consumed_at
            + timedelta(seconds=MAX_CONSUMPTION_RECOVERY_SECONDS)
        ),
    )


def _revalidate_ai_panorama_install_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
    *,
    require_consumed: bool,
    allow_execution_lease_expired: bool,
) -> VerifiedAiPanoramaInstallAdmission:
    if type(require_consumed) is not bool:
        _fail("ai_panorama_consumption_requirement_invalid")
    if type(allow_execution_lease_expired) is not bool:
        _fail("ai_panorama_recovery_requirement_invalid")
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
        allow_expired=admission.nonce_consumed is True,
    )
    for field in admission.__dataclass_fields__:
        if field in {
            "nonce_consumed",
            "consumed_at",
            "execution_lease_expires_at",
            "_ledger_instance_id",
            "_ledger_sequence",
            "_ledger_entry_sha256",
            "_request_tombstone_sha256",
            "_nonce_tombstone_sha256",
            "_permit_tombstone_sha256",
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
            or admission.consumed_at
            or admission.execution_lease_expires_at
            or admission._request_tombstone_sha256
            or admission._nonce_tombstone_sha256
            or admission._permit_tombstone_sha256
        ):
            _fail("ai_panorama_admission_invalid")
        return fresh

    control_descriptor, lock_descriptor, ledger, _ledger_identity = (
        _open_locked_ledger()
    )
    try:
        entry = _matching_consumption_entry(admission, ledger)
        _validate_consumption_window(
            admission,
            entry,
            allow_execution_lease_expired=(
                allow_execution_lease_expired
            ),
        )
        _validate_consumption_tombstones(
            control_descriptor,
            admission,
            ledger,
            entry,
        )
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


def revalidate_ai_panorama_install_admission(
    admission: VerifiedAiPanoramaInstallAdmission,
    *,
    require_consumed: bool,
) -> VerifiedAiPanoramaInstallAdmission:
    """Reverify current authority within the bounded execution lease."""

    return _revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=require_consumed,
        allow_execution_lease_expired=False,
    )


def revalidate_ai_panorama_install_recovery(
    admission: VerifiedAiPanoramaInstallAdmission,
) -> VerifiedAiPanoramaInstallRecoveryEvidence:
    """Prove a recent consumption for terminal recovery, never installation."""

    consumed = _revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=True,
        allow_execution_lease_expired=True,
    )
    return _recovery_evidence(consumed)


def _recover_ai_panorama_install_consumed_admission(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaInstallAdmission:
    """Reconstruct a consumed admission only for internal classification."""

    fresh = _unconsumed_admission(
        permit_relpath,
        expected,
        allow_expired=True,
    )
    request_id_sha256 = _sha256(fresh.request_id.encode("ascii"))
    nonce_sha256 = _sha256(fresh.nonce.encode("ascii"))
    control_descriptor, lock_descriptor, ledger, _ledger_identity = (
        _open_locked_ledger()
    )
    try:
        matches = [
            entry
            for entry in ledger["entries"]
            if entry["permit_sha256"] == fresh.permit_sha256
            and entry["request_id_sha256"] == request_id_sha256
            and entry["nonce_sha256"] == nonce_sha256
            and entry["context_sha256"] == fresh._context_sha256
            and entry["signed_preimage_sha256"]
            == fresh._signed_preimage_sha256
        ]
        if len(matches) != 1:
            _fail("ai_panorama_consumption_record_missing")
        entry = matches[0]
        consumed = _with_consumption(
            fresh,
            ledger_instance_id=ledger["instance_id"],
            entry=entry,
        )
        _matching_consumption_entry(consumed, ledger)
        _validate_consumption_window(
            consumed,
            entry,
            allow_execution_lease_expired=True,
        )
        _validate_consumption_tombstones(
            control_descriptor,
            consumed,
            ledger,
            entry,
        )
        return consumed
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def recover_ai_panorama_install_consumption(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaInstallRecoveryEvidence:
    """Reconstruct recovery-only evidence after controller process loss."""

    return _recovery_evidence(
        _recover_ai_panorama_install_consumed_admission(
            permit_relpath,
            expected,
        )
    )


def load_ai_panorama_install_historical_consumption(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> VerifiedAiPanoramaHistoricalConsumptionProof:
    """Prove historical consumption from the exact signed retained context.

    This proof has no wall-clock cutoff and is intentionally not accepted by
    any install, binding, deletion, or bounded recovery API.
    """

    fresh = _unconsumed_admission(
        permit_relpath,
        expected,
        allow_expired=True,
        historical_key_at_issuance_only=True,
    )
    request_id_sha256 = _sha256(fresh.request_id.encode("ascii"))
    nonce_sha256 = _sha256(fresh.nonce.encode("ascii"))
    control_descriptor, lock_descriptor, ledger, _ledger_identity = (
        _open_locked_ledger()
    )
    try:
        matches = [
            entry
            for entry in ledger["entries"]
            if entry["permit_sha256"] == fresh.permit_sha256
            and entry["request_id_sha256"] == request_id_sha256
            and entry["nonce_sha256"] == nonce_sha256
            and entry["context_sha256"] == fresh._context_sha256
            and entry["signed_preimage_sha256"]
            == fresh._signed_preimage_sha256
            and entry["trust_assertion_sha256"]
            == fresh._trust_assertion_sha256
        ]
        if len(matches) != 1:
            _fail("ai_panorama_consumption_record_missing")
        entry = matches[0]
        consumed = _with_consumption(
            fresh,
            ledger_instance_id=ledger["instance_id"],
            entry=entry,
        )
        _matching_consumption_entry(consumed, ledger)
        consumed_at = _timestamp(
            entry["consumed_at"],
            "ai_panorama_consumption_record_mismatch",
        )
        lease_expires_at = _timestamp(
            entry["execution_lease_expires_at"],
            "ai_panorama_consumption_record_mismatch",
        )
        issued_at = _timestamp(
            consumed.issued_at,
            "ai_panorama_consumption_record_mismatch",
        )
        permit_expires_at = _timestamp(
            consumed.expires_at,
            "ai_panorama_consumption_record_mismatch",
        )
        if (
            not issued_at <= consumed_at < permit_expires_at
            or lease_expires_at
            != consumed_at
            + timedelta(seconds=consumed.execution_lease_seconds)
        ):
            _fail("ai_panorama_consumption_record_mismatch")
        _validate_consumption_tombstones(
            control_descriptor,
            consumed,
            ledger,
            entry,
        )
        return VerifiedAiPanoramaHistoricalConsumptionProof(
            permit_sha256=consumed.permit_sha256,
            request_id_sha256=request_id_sha256,
            nonce_sha256=nonce_sha256,
            context_sha256=consumed._context_sha256,
            signed_preimage_sha256=consumed._signed_preimage_sha256,
            trust_assertion_sha256=str(
                consumed._trust_assertion_sha256
            ),
            ledger_instance_id=str(ledger["instance_id"]),
            ledger_sequence=int(entry["sequence"]),
            ledger_entry_sha256=str(entry["entry_sha256"]),
            consumed_at=str(entry["consumed_at"]),
            execution_lease_expires_at=str(
                entry["execution_lease_expires_at"]
            ),
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


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
