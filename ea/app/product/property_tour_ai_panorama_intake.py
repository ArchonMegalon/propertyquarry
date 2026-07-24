from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping
from uuid import uuid4

from app.product.property_search_storage import (
    load_property_search_run_record_for_publication,
    property_account_publication_authority,
    update_property_search_run_record_for_publication,
)
from app.product.property_search_tour_binding import (
    PropertySearchTourBindingError,
    _normalized_provider_key,
    _property_url_contains_listing_id,
    _provider_key_from_url,
    _source_ref_identity,
    authorize_property_search_candidate_tour_install,
    canonical_property_source_url,
    plan_governed_prater_candidate_tour_binding,
    plan_property_search_candidate_tour_binding,
    property_search_run_record_sha256,
    property_search_source_url_sha256,
)
from app.product.property_tour_governed_reservations import (
    GOVERNED_PRATER_CANDIDATE_MARKER_SHA256 as _GOVERNED_RELOCATION_MARKER_SHA256,
    GOVERNED_PRATER_CORE_MANIFEST_SHA256 as _GOVERNED_RELOCATION_CORE_MANIFEST_SHA256,
    GOVERNED_PRATER_MATERIALIZATION_RECEIPT_SHA256 as _GOVERNED_RELOCATION_RECEIPT_SHA256,
    GOVERNED_PRATER_SLUG as _GOVERNED_RELOCATION_SLUG,
    GOVERNED_PRATER_SOURCE_TREE_SHA256 as _GOVERNED_RELOCATION_SOURCE_TREE_SHA256,
    GOVERNED_PRATER_TOUR_SHA256 as _GOVERNED_RELOCATION_TOUR_SHA256,
    GOVERNED_PUBLIC_TOUR_MOUNT_TARGET,
    GOVERNED_PUBLIC_TOUR_VOLUME_NAME,
    governed_prater_control_url_reserved,
)
from app.product.property_tour_hosting import (
    _hosted_property_tour_ai_panorama_contract,
    _hosted_property_tour_public_asset_relpath,
    _hosted_property_tour_publication_lock,
    _load_hosted_property_tour_private_receipt,
    _public_tour_dir,
    _public_tour_private_receipt,
    _write_hosted_property_tour_manifests_atomic,
)


AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1 = (
    "propertyquarry.ai_panorama_sealed_install_request.v1"
)
AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2 = (
    "propertyquarry.ai_panorama_sealed_install_request.v2"
)
# Backward-compatible name for existing private v1 requests. New governed
# release handoffs must use AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2.
AI_PANORAMA_INSTALL_REQUEST_CONTRACT = AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1
AI_PANORAMA_INSTALL_RECEIPT_CONTRACT = (
    "propertyquarry.ai_panorama_sealed_install_receipt.v1"
)
AI_PANORAMA_INSTALL_SOURCE_IDENTITY_CONTRACT = (
    "propertyquarry.ai_panorama_installer_source_identity.v1"
)
AI_PANORAMA_INSTALL_SOURCE_TREE_ALGORITHM = (
    "sha256-canonical-json-sorted-file-records.v1"
)
AI_PANORAMA_INSTALL_SOURCE_RELATIVE_ROOT = "."
AI_PANORAMA_INSTALL_SOURCE_RELATIVE_PATH_SEMANTICS = (
    "sealed-bundle-root-relative-posix-paths"
)
AI_PANORAMA_MATERIALIZATION_RECEIPT_CONTRACT = (
    "propertyquarry.ai_panorama_materialization_receipt.v1"
)
AI_PANORAMA_CANDIDATE_MARKER_CONTRACT = (
    "propertyquarry.ai_panorama_candidate_copy.v1"
)
AI_PANORAMA_CANDIDATE_MARKER_RELPATH = (
    ".propertyquarry-ai-panorama-candidate.json"
)
_PRIVATE_REQUEST_MAX_BYTES = 64 * 1024
_SOURCE_MANIFEST_MAX_BYTES = 1024 * 1024
_SOURCE_MAX_FILES = 256
_SOURCE_MAX_BYTES = 64 * 1024 * 1024
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}")
_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}")
_MATERIALIZATION_LINEAGE_REQUEST_KEYS = frozenset(
    {
        "materialization_receipt_path",
        "expected_materialization_receipt_sha256",
        "expected_candidate_marker_sha256",
    }
)
_GOVERNED_RELOCATION_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/"
    "ai-panorama-artifacts/prater-v1"
)
_GOVERNED_RELOCATION_HISTORICAL_CANDIDATE_ROOT = Path(
    "/docker/property/state/incoming_property_tours/"
    "prater-053ad185e1c44b2e/ai-panorama-v2-yaw65-final"
)
_PRIVATE_MANIFEST_KEYS = frozenset(
    {
        "principal_id",
        "search_run_id",
        "candidate_ref",
        "research_candidate_ref",
        "listing_url",
        "property_url",
        "source_ref",
        "external_id",
        "recipient_email",
    }
)


class AiPanoramaIntakeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "ai_panorama_intake_failed").strip()
        self.rollback_performed = False
        self.commit_outcome_ambiguous = False
        self.publication_outcome = "unknown"
        super().__init__(self.code)


@dataclass(frozen=True)
class _SourceFile:
    relpath: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    modified_ns: int


@dataclass(frozen=True)
class _SourceSnapshot:
    files: tuple[_SourceFile, ...]
    tree_sha256: str
    tour_sha256: str
    total_bytes: int


@dataclass(frozen=True)
class AiPanoramaInstallerSourceIdentity:
    """Exact file identity consumed by the sealed-bundle installer.

    Paths are canonical POSIX paths relative to the sealed bundle directory.
    Directory entries are intentionally excluded; the materializer retains its
    separate directory-aware audit identity in addition to this handoff identity.
    """

    contract_name: str
    tree_algorithm: str
    relative_root: str
    relative_path_semantics: str
    tree_sha256: str
    tour_sha256: str
    file_count: int
    total_bytes: int


def _fail(code: str) -> None:
    raise AiPanoramaIntakeError(code)


def _revalidate_ai_panorama_install_admission(
    admission: object,
    *,
    require_consumed: bool,
) -> object:
    """Re-enter the controller verifier; an in-memory object is not authority."""

    try:
        from app.product.property_tour_ai_panorama_admission import (
            revalidate_ai_panorama_install_admission,
        )
    except (ImportError, AttributeError) as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_controller_admission_unavailable"
        ) from exc
    try:
        return revalidate_ai_panorama_install_admission(
            admission,
            require_consumed=require_consumed,
        )
    except Exception as exc:
        # The permit can contain private owner and source identity. Collapse all
        # verifier failures at this boundary rather than serializing them.
        raise AiPanoramaIntakeError(
            "ai_panorama_controller_admission_invalid"
        ) from exc


def _controller_publication_admission(
    request: Mapping[str, object],
    admission: object,
    *,
    require_consumed: bool,
) -> tuple[dict[str, object], dict[str, str], Path, Path]:
    """Return request/path values only after external authority revalidation.

    The verifier rechecks both the raw Ed25519 permit and the durable,
    controller-owned nonce ledger.  Paths below come only from that verified
    admission, never from environment variables or the operator request.
    """

    if admission is None:
        _fail("ai_panorama_apply_authority_required")
    verified = _revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=require_consumed,
    )
    request_bindings = (
        ("principal_id", "authenticated_principal_id"),
        ("search_run_id", "search_run_id"),
        ("candidate_ref", "candidate_ref"),
        ("external_id", "external_id"),
        ("listing_url", "listing_url"),
        ("source_ref", "source_ref"),
        ("provider_key", "provider_key"),
        ("expected_slug", "expected_slug"),
        ("public_control_url", "public_control_url"),
        ("expected_source_tree_sha256", "expected_source_tree_sha256"),
        ("expected_tour_sha256", "expected_tour_sha256"),
        ("expected_core_manifest_sha256", "expected_core_manifest_sha256"),
        (
            "expected_materialization_receipt_sha256",
            "expected_materialization_receipt_sha256",
        ),
        (
            "expected_candidate_marker_sha256",
            "expected_candidate_marker_sha256",
        ),
        (
            "expected_publication_record_sha256",
            "expected_publication_record_sha256",
        ),
    )
    effective = dict(request)
    for request_key, admission_key in request_bindings:
        expected = str(getattr(verified, admission_key, "") or "").strip()
        actual = str(request.get(request_key) or "").strip()
        if request_key.startswith("expected_") and request_key.endswith("_sha256"):
            expected = expected.lower()
            actual = actual.lower()
        if not expected or not actual or not hmac.compare_digest(actual, expected):
            _fail("ai_panorama_apply_authority_binding_mismatch")

    source_bundle = _request_path(
        getattr(verified, "source_bundle", None),
        code="ai_panorama_controller_source_path_invalid",
    )
    receipt_path = _request_path(
        getattr(verified, "materialization_receipt_path", None),
        code="ai_panorama_controller_receipt_path_invalid",
    )
    incoming_root = _request_path(
        getattr(verified, "incoming_root", None),
        code="ai_panorama_controller_incoming_root_invalid",
    )
    public_tour_dir = _request_path(
        getattr(verified, "public_tour_dir", None),
        code="ai_panorama_controller_public_root_invalid",
    )
    public_volume_name = str(
        getattr(verified, "public_tour_volume_name", "") or ""
    ).strip()
    public_mount_target = str(
        getattr(verified, "public_tour_mount_target", "") or ""
    ).strip()
    public_root_device = getattr(verified, "public_tour_root_device", None)
    public_root_inode = getattr(verified, "public_tour_root_inode", None)
    if (
        public_volume_name != GOVERNED_PUBLIC_TOUR_VOLUME_NAME
        or public_mount_target != GOVERNED_PUBLIC_TOUR_MOUNT_TARGET
        or type(public_root_device) is not int
        or type(public_root_inode) is not int
        or public_root_device < 1
        or public_root_inode < 1
    ):
        _fail("ai_panorama_controller_public_volume_profile_invalid")
    for request_key, expected_path in (
        ("source_bundle", source_bundle),
        ("materialization_receipt_path", receipt_path),
        ("public_tour_dir", public_tour_dir),
    ):
        supplied = str(request.get(request_key) or "").strip()
        if supplied and not hmac.compare_digest(supplied, str(expected_path)):
            _fail("ai_panorama_apply_authority_path_mismatch")
        effective[request_key] = str(expected_path)

    permit_sha256 = _require_digest(
        getattr(verified, "permit_sha256", None),
        code="ai_panorama_controller_permit_sha256_invalid",
        required=True,
    )
    identity = {
        "controller_permit_verified": "true",
        "controller_permit_sha256": permit_sha256,
        "authenticated_principal_verified": "true",
        "controller_nonce_consumed": (
            "true"
            if bool(getattr(verified, "nonce_consumed", False))
            else "false"
        ),
        "expected_publication_record_sha256": str(
            getattr(verified, "expected_publication_record_sha256", "") or ""
        ).strip().lower(),
        "public_tour_root_device": str(public_root_device),
        "public_tour_root_inode": str(public_root_inode),
        "public_tour_volume_profile_verified": "true",
    }
    if require_consumed and identity["controller_nonce_consumed"] != "true":
        _fail("ai_panorama_controller_nonce_not_consumed")
    return effective, identity, incoming_root, public_tour_dir


def prepare_ai_panorama_publication_binding(
    request: Mapping[str, object],
    *,
    publication_admission: object,
) -> dict[str, object]:
    """Durably precomputable, read-only binding plan for journal preparation."""

    if request.get("contract") != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2:
        _fail("ai_panorama_controller_admission_contract_invalid")
    (
        effective,
        _controller_identity,
        _incoming_root,
        _public_root,
    ) = _controller_publication_admission(
        request,
        publication_admission,
        require_consumed=True,
    )
    identity = {
        "principal_id": str(effective["principal_id"]),
        "search_run_id": str(effective["search_run_id"]),
        "candidate_ref": str(effective["candidate_ref"]),
        "external_id": str(effective["external_id"]),
        "listing_url": str(effective["listing_url"]),
        "source_ref": str(effective["source_ref"]),
        "provider_key": str(effective["provider_key"]),
        "property_url_sha256": property_search_source_url_sha256(
            effective["listing_url"]
        ),
        "expected_publication_record_sha256": str(
            effective["expected_publication_record_sha256"]
        ),
    }
    bundle_identity = {
        "owner_verified": True,
        "search_run_id": identity["search_run_id"],
        "candidate_ref": identity["candidate_ref"],
        "listing_url": identity["listing_url"],
        "property_url": identity["listing_url"],
        "property_url_sha256": identity["property_url_sha256"],
        "provider_key": identity["provider_key"],
        "source_ref": identity["source_ref"],
        "external_id": identity["external_id"],
    }
    bound_at = datetime.now(timezone.utc).isoformat()
    with property_account_publication_authority(
        identity["principal_id"],
        run_id=identity["search_run_id"],
    ) as connection:
        identity.update(
            _validate_v2_publication_authority(
                request=effective,
                identity=identity,
                connection=connection,
                for_update=True,
                expected_record_sha256=identity[
                    "expected_publication_record_sha256"
                ],
            )
        )
        record = load_property_search_run_record_for_publication(
            run_id=identity["search_run_id"],
            principal_id=identity["principal_id"],
            connection=connection,
            for_update=True,
        )
        if not isinstance(record, Mapping):
            _fail("ai_panorama_publication_run_not_found")
        try:
            _updated, receipt = plan_governed_prater_candidate_tour_binding(
                record,
                principal_id=identity["principal_id"],
                run_id=identity["search_run_id"],
                candidate_ref=identity["candidate_ref"],
                expected_listing_id=identity["external_id"],
                generated_reconstruction_url=str(
                    effective["public_control_url"]
                ),
                bundle_identity=bundle_identity,
                publication_admission=publication_admission,
                reconstruction_kind="ai_panorama_360",
                bound_at=bound_at,
            )
        except PropertySearchTourBindingError as exc:
            raise AiPanoramaIntakeError(
                f"ai_panorama_publication_binding:{exc.code}"
            ) from exc
    before_sha256 = _require_digest(
        receipt.get("before_sha256"),
        code="ai_panorama_publication_binding_before_invalid",
        required=True,
    )
    after_sha256 = _require_digest(
        receipt.get("after_sha256"),
        code="ai_panorama_publication_binding_after_invalid",
        required=True,
    )
    changed = receipt.get("changed")
    digests_differ = not hmac.compare_digest(
        before_sha256,
        after_sha256,
    )
    if type(changed) is not bool or changed != digests_differ:
        _fail("ai_panorama_publication_binding_plan_invalid")
    return {
        "status": "change-required" if changed else "already-bound",
        "publication_binding_expected_before_sha256": before_sha256,
        "publication_binding_expected_after_sha256": after_sha256,
        "publication_binding_bound_at": bound_at,
        "database_mutation_performed": False,
        "private_values_redacted": True,
    }


def _require_digest(value: object, *, code: str, required: bool) -> str:
    if value is None:
        if not required:
            return ""
        _fail(code)
    if type(value) is not str:
        _fail(code)
    digest = value.strip().lower()
    if not digest:
        if not required:
            return ""
        _fail(code)
    if not _DIGEST_PATTERN.fullmatch(digest):
        _fail(code)
    return digest


def _require_safe_id(value: object, *, code: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID_PATTERN.fullmatch(normalized):
        _fail(code)
    return normalized


def _require_principal(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        _fail("ai_panorama_principal_required")
    return normalized


def _request_path(value: object, *, code: str) -> Path:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        _fail(code)
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        _fail(code)
    return path


def _require_no_symlink_components(path: Path, *, code: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            details = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise AiPanoramaIntakeError(code) from exc
        if stat.S_ISLNK(details.st_mode):
            _fail(code)


def _open_regular_nofollow(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
    required_uid: int | None = None,
    forbidden_mode_bits: int = 0,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> bytes:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            _fail("ai_panorama_nofollow_unavailable")
        descriptor = os.open(path, flags | nofollow)
        details = os.fstat(descriptor)
        opened_identity = (
            int(details.st_dev),
            int(details.st_ino),
            int(details.st_size),
            int(details.st_mtime_ns),
        )
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size <= 0
            or details.st_size > maximum_bytes
            or (required_uid is not None and details.st_uid != required_uid)
            or stat.S_IMODE(details.st_mode) & forbidden_mode_bits
            or (
                expected_identity is not None
                and opened_identity != expected_identity
            )
        ):
            _fail(code)
        chunks: list[bytes] = []
        remaining = int(details.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                _fail(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        observed = os.fstat(descriptor)
        if (
            observed.st_dev != details.st_dev
            or observed.st_ino != details.st_ino
            or observed.st_size != details.st_size
            or observed.st_mtime_ns != details.st_mtime_ns
        ):
            _fail("ai_panorama_source_changed")
        return b"".join(chunks)
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_private_ai_panorama_install_request(path: Path) -> dict[str, object]:
    """Load the operator request without following links or exposing its values."""

    request_path = _request_path(path, code="ai_panorama_request_path_invalid")
    _require_no_symlink_components(
        request_path,
        code="ai_panorama_request_permissions_invalid",
    )
    try:
        details = request_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_request_unreadable") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        _fail("ai_panorama_request_permissions_invalid")
    request_identity = (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
    )
    encoded = _open_regular_nofollow(
        request_path,
        maximum_bytes=_PRIVATE_REQUEST_MAX_BYTES,
        code="ai_panorama_request_permissions_invalid",
        required_uid=os.geteuid(),
        forbidden_mode_bits=0o077,
        expected_identity=request_identity,
    )
    try:
        after_details = request_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_request_permissions_invalid") from exc
    if (
        stat.S_ISLNK(after_details.st_mode)
        or not stat.S_ISREG(after_details.st_mode)
        or after_details.st_uid != os.geteuid()
        or stat.S_IMODE(after_details.st_mode) & 0o077
        or (
        int(after_details.st_dev),
        int(after_details.st_ino),
        int(after_details.st_size),
        int(after_details.st_mtime_ns),
        )
        != request_identity
    ):
        _fail("ai_panorama_request_permissions_invalid")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError("ai_panorama_request_invalid") from exc
    if not isinstance(payload, dict):
        _fail("ai_panorama_request_invalid")
    request = dict(payload)
    if request.get("contract") not in {
        AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1,
        AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
    }:
        _fail("ai_panorama_request_contract_invalid")
    return request


def _safe_relpath(value: object) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(part.startswith(".") for part in candidate.parts)
        or len(normalized.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        _fail("ai_panorama_source_relpath_invalid")
    return candidate.as_posix()


def _configured_incoming_tour_dir() -> Path:
    raw = str(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or "/data/incoming_property_tours"
    ).strip()
    return Path(raw).expanduser()


def _directory_identity(path: Path, *, code: str) -> tuple[int, int]:
    _require_no_symlink_components(path, code=code)
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError(code) from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        _fail(code)
    return int(details.st_dev), int(details.st_ino)


def _confined_source_bundle(
    path: Path,
    *,
    admitted_incoming_root: Path | None = None,
) -> Path:
    incoming_root = _request_path(
        (
            admitted_incoming_root
            if admitted_incoming_root is not None
            else _configured_incoming_tour_dir()
        ),
        code="ai_panorama_incoming_root_invalid",
    )
    _directory_identity(incoming_root, code="ai_panorama_incoming_root_invalid")
    try:
        relative = path.relative_to(incoming_root)
    except ValueError:
        _fail("ai_panorama_source_outside_incoming_root")
    if not relative.parts:
        _fail("ai_panorama_source_outside_incoming_root")
    current = incoming_root
    for part in relative.parts:
        current /= part
        _directory_identity(current, code="ai_panorama_source_path_unsafe")
        try:
            details = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise AiPanoramaIntakeError("ai_panorama_source_path_unsafe") from exc
        if stat.S_IMODE(details.st_mode) & 0o022:
            _fail("ai_panorama_source_path_unsafe")
    return path


def _hash_regular_file(path: Path, *, details: os.stat_result) -> str:
    descriptor = -1
    digest = hashlib.sha256()
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != details.st_dev
            or opened.st_ino != details.st_ino
            or opened.st_size != details.st_size
            or opened.st_mtime_ns != details.st_mtime_ns
        ):
            _fail("ai_panorama_source_changed")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        closed = os.fstat(descriptor)
        if (
            closed.st_dev != opened.st_dev
            or closed.st_ino != opened.st_ino
            or closed.st_size != opened.st_size
            or closed.st_mtime_ns != opened.st_mtime_ns
        ):
            _fail("ai_panorama_source_changed")
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_source_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _scan_source_bundle(source_bundle: Path) -> _SourceSnapshot:
    try:
        root_details = source_bundle.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_source_missing") from exc
    if (
        stat.S_ISLNK(root_details.st_mode)
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) & 0o022
    ):
        _fail("ai_panorama_source_directory_unsafe")
    rows: list[_SourceFile] = []
    total_bytes = 0
    try:
        walker = os.walk(source_bundle, topdown=True, followlinks=False)
        for current_raw, directory_names, file_names in walker:
            current = Path(current_raw)
            current_details = current.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(current_details.st_mode)
                or not stat.S_ISDIR(current_details.st_mode)
                or stat.S_IMODE(current_details.st_mode) & 0o022
            ):
                _fail("ai_panorama_source_directory_unsafe")
            directory_names.sort()
            file_names.sort()
            for directory_name in directory_names:
                directory_path = current / directory_name
                directory_details = directory_path.stat(follow_symlinks=False)
                _safe_relpath(directory_path.relative_to(source_bundle).as_posix())
                if stat.S_ISLNK(directory_details.st_mode) or not stat.S_ISDIR(
                    directory_details.st_mode
                ):
                    _fail("ai_panorama_source_symlink_forbidden")
            for file_name in file_names:
                file_path = current / file_name
                relpath = _safe_relpath(file_path.relative_to(source_bundle).as_posix())
                details = file_path.stat(follow_symlinks=False)
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                    _fail("ai_panorama_source_symlink_forbidden")
                if stat.S_IMODE(details.st_mode) & 0o022:
                    _fail("ai_panorama_source_file_unsafe")
                if file_name == "tour.private.json":
                    _fail("ai_panorama_source_private_receipt_forbidden")
                total_bytes += int(details.st_size)
                if len(rows) >= _SOURCE_MAX_FILES or total_bytes > _SOURCE_MAX_BYTES:
                    _fail("ai_panorama_source_budget_exceeded")
                rows.append(
                    _SourceFile(
                        relpath=relpath,
                        size_bytes=int(details.st_size),
                        sha256=_hash_regular_file(file_path, details=details),
                        device=int(details.st_dev),
                        inode=int(details.st_ino),
                        modified_ns=int(details.st_mtime_ns),
                    )
                )
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_source_unreadable") from exc
    rows.sort(key=lambda row: row.relpath)
    if not rows or rows[0].relpath == "tour.private.json":
        _fail("ai_panorama_source_empty")
    by_path = {row.relpath: row for row in rows}
    if "tour.json" not in by_path:
        _fail("ai_panorama_source_manifest_missing")
    canonical = json.dumps(
        [
            {
                "relpath": row.relpath,
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
            }
            for row in rows
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _SourceSnapshot(
        files=tuple(rows),
        tree_sha256=hashlib.sha256(canonical).hexdigest(),
        tour_sha256=by_path["tour.json"].sha256,
        total_bytes=total_bytes,
    )


def snapshot_ai_panorama_installer_source_bundle(
    source_bundle: Path,
) -> AiPanoramaInstallerSourceIdentity:
    """Return the authoritative installer-facing identity for a sealed bundle.

    This is the sole public implementation of the installer file-manifest hash.
    Callers that hand a candidate to ``install_sealed_ai_panorama_bundle`` must
    bind this identity, rather than independently reproducing its serialization.
    The snapshot is content identity only; it does not confer install or release
    authority.
    """

    snapshot = _scan_source_bundle(Path(source_bundle))
    return AiPanoramaInstallerSourceIdentity(
        contract_name=AI_PANORAMA_INSTALL_SOURCE_IDENTITY_CONTRACT,
        tree_algorithm=AI_PANORAMA_INSTALL_SOURCE_TREE_ALGORITHM,
        relative_root=AI_PANORAMA_INSTALL_SOURCE_RELATIVE_ROOT,
        relative_path_semantics=AI_PANORAMA_INSTALL_SOURCE_RELATIVE_PATH_SEMANTICS,
        tree_sha256=snapshot.tree_sha256,
        tour_sha256=snapshot.tour_sha256,
        file_count=len(snapshot.files),
        total_bytes=snapshot.total_bytes,
    )


def _load_source_manifest(source_bundle: Path) -> dict[str, object]:
    encoded = _open_regular_nofollow(
        source_bundle / "tour.json",
        maximum_bytes=_SOURCE_MANIFEST_MAX_BYTES,
        code="ai_panorama_source_manifest_invalid",
    )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError("ai_panorama_source_manifest_invalid") from exc
    if not isinstance(payload, dict):
        _fail("ai_panorama_source_manifest_invalid")
    return dict(payload)


def _canonical_lineage_json_bytes(value: Mapping[str, object], *, code: str) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AiPanoramaIntakeError(code) from exc


def _exact_json_value(actual: object, expected: object) -> bool:
    """Compare receipt scalars without JSON bool/int equivalence."""

    return type(actual) is type(expected) and actual == expected


def _lineage_file_identity(path: Path, *, code: str) -> tuple[int, int, int, int]:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError(code) from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        _fail(code)
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _load_lineage_json(
    path: Path,
    *,
    code: str,
) -> tuple[dict[str, object], bytes, tuple[int, int, int, int]]:
    before_identity = _lineage_file_identity(path, code=code)
    encoded = _open_regular_nofollow(
        path,
        maximum_bytes=_SOURCE_MANIFEST_MAX_BYTES,
        code=code,
        forbidden_mode_bits=0o022,
        expected_identity=before_identity,
    )

    def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    def _reject_nonfinite(_value: str) -> None:
        _fail(code)

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except AiPanoramaIntakeError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError(code) from exc
    if not isinstance(payload, dict):
        _fail(code)
    normalized = dict(payload)
    if encoded != _canonical_lineage_json_bytes(normalized, code=code):
        _fail(code)
    after_identity = _lineage_file_identity(path, code=code)
    if before_identity != after_identity:
        _fail(code)
    return normalized, encoded, after_identity


def _validate_materialization_lineage(
    *,
    request: Mapping[str, object],
    source_bundle: Path,
    snapshot: _SourceSnapshot,
    slug: str,
    core_manifest_sha256: str,
    governed_admission: bool,
) -> dict[str, str]:
    request_contract = str(request.get("contract") or "").strip()
    release_eligible = request_contract == AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2
    supplied = {key for key in _MATERIALIZATION_LINEAGE_REQUEST_KEYS if key in request}
    if not supplied:
        if release_eligible:
            _fail("ai_panorama_materialization_lineage_required")
        return {
            "install_request_contract": request_contract,
            "release_eligible": "false",
        }
    if supplied != _MATERIALIZATION_LINEAGE_REQUEST_KEYS:
        _fail(
            "ai_panorama_materialization_lineage_required"
            if release_eligible
            else "ai_panorama_materialization_lineage_incomplete"
        )

    candidate_directory_identity = _directory_identity(
        source_bundle,
        code="ai_panorama_materialization_candidate_binding_mismatch",
    )

    expected_receipt_sha256 = _require_digest(
        request.get("expected_materialization_receipt_sha256"),
        code="ai_panorama_materialization_receipt_sha256_invalid",
        required=True,
    )
    expected_marker_sha256 = _require_digest(
        request.get("expected_candidate_marker_sha256"),
        code="ai_panorama_candidate_marker_sha256_invalid",
        required=True,
    )
    receipt_path = _request_path(
        request.get("materialization_receipt_path"),
        code="ai_panorama_materialization_receipt_path_invalid",
    )
    _require_no_symlink_components(
        receipt_path,
        code="ai_panorama_materialization_receipt_path_invalid",
    )
    receipt, receipt_bytes, receipt_file_identity = _load_lineage_json(
        receipt_path,
        code="ai_panorama_materialization_receipt_invalid",
    )
    if not hmac.compare_digest(
        expected_receipt_sha256,
        hashlib.sha256(receipt_bytes).hexdigest(),
    ):
        _fail("ai_panorama_materialization_receipt_sha256_mismatch")

    candidate_root = _request_path(
        receipt.get("candidate_public_root"),
        code="ai_panorama_materialization_candidate_root_invalid",
    )
    candidate_relpath = _safe_relpath(receipt.get("candidate_bundle_relpath"))
    relocated = candidate_root != source_bundle.parent
    if relocated:
        governed_relocation = (
            governed_admission
            and request_contract == AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2
            and slug == _GOVERNED_RELOCATION_SLUG
            and candidate_root
            == _GOVERNED_RELOCATION_HISTORICAL_CANDIDATE_ROOT
            and source_bundle
            == _GOVERNED_RELOCATION_ROOT / "bundle" / _GOVERNED_RELOCATION_SLUG
            and receipt_path
            == _GOVERNED_RELOCATION_ROOT / "materialization.receipt.json"
            and hmac.compare_digest(
                expected_receipt_sha256,
                _GOVERNED_RELOCATION_RECEIPT_SHA256,
            )
            and hmac.compare_digest(
                expected_marker_sha256,
                _GOVERNED_RELOCATION_MARKER_SHA256,
            )
            and hmac.compare_digest(
                snapshot.tree_sha256,
                _GOVERNED_RELOCATION_SOURCE_TREE_SHA256,
            )
            and hmac.compare_digest(
                snapshot.tour_sha256,
                _GOVERNED_RELOCATION_TOUR_SHA256,
            )
            and hmac.compare_digest(
                core_manifest_sha256,
                _GOVERNED_RELOCATION_CORE_MANIFEST_SHA256,
            )
        )
        if not governed_relocation:
            _fail("ai_panorama_materialization_relocation_forbidden")
    else:
        _require_no_symlink_components(
            candidate_root,
            code="ai_panorama_materialization_candidate_root_invalid",
        )
    live_candidate_root = source_bundle.parent
    candidate_bundle = live_candidate_root / candidate_relpath
    if candidate_relpath != slug or candidate_bundle != source_bundle:
        _fail("ai_panorama_materialization_candidate_binding_mismatch")
    candidate_root_identity = _directory_identity(
        live_candidate_root,
        code="ai_panorama_materialization_candidate_root_invalid",
    )
    source_parent_identity = _directory_identity(
        source_bundle.parent,
        code="ai_panorama_source_path_unsafe",
    )
    candidate_bundle_identity = _directory_identity(
        candidate_bundle,
        code="ai_panorama_materialization_candidate_binding_mismatch",
    )
    if (
        candidate_root_identity != source_parent_identity
        or candidate_bundle_identity != candidate_directory_identity
    ):
        _fail("ai_panorama_materialization_candidate_binding_mismatch")

    expected_receipt_fields: dict[str, object] = {
        "contract_name": AI_PANORAMA_MATERIALIZATION_RECEIPT_CONTRACT,
        "status": "pass",
        "slug": slug,
        "candidate_public_root": str(candidate_root),
        "candidate_bundle_relpath": slug,
        "candidate_marker_relpath": AI_PANORAMA_CANDIDATE_MARKER_RELPATH,
        "candidate_marker_sha256": expected_marker_sha256,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "candidate_file_count": len(snapshot.files),
        "candidate_size_bytes": snapshot.total_bytes,
        "installer_source_identity_contract": AI_PANORAMA_INSTALL_SOURCE_IDENTITY_CONTRACT,
        "installer_source_tree_algorithm": AI_PANORAMA_INSTALL_SOURCE_TREE_ALGORITHM,
        "installer_source_relative_root": AI_PANORAMA_INSTALL_SOURCE_RELATIVE_ROOT,
        "installer_source_relative_path_semantics": AI_PANORAMA_INSTALL_SOURCE_RELATIVE_PATH_SEMANTICS,
        "installer_source_tree_sha256": snapshot.tree_sha256,
        "installer_source_tour_sha256": snapshot.tour_sha256,
        "installer_source_file_count": len(snapshot.files),
        "installer_source_total_bytes": snapshot.total_bytes,
        "tour_manifest_sha256": snapshot.tour_sha256,
        "core_manifest_sha256": core_manifest_sha256,
        "source_copy_identity_verified": True,
        "source_bundle_unchanged": True,
        "source_unchanged_after_candidate_seal": True,
        "candidate_identity_rechecked_after_receipt_write": True,
        "production_mutation_performed": False,
        "controller_bypass_performed": False,
    }
    if (
        any(
            not _exact_json_value(receipt.get(key), value)
            for key, value in expected_receipt_fields.items()
        )
        or any(
            type(receipt.get(key)) is not str
            or _DIGEST_PATTERN.fullmatch(receipt[key]) is None
            for key in (
                "candidate_tree_sha256",
                "source_tree_sha256",
                "bundle_material_sha256",
            )
        )
        or type(receipt.get("source_file_count")) is not int
        or int(receipt.get("source_file_count") or 0) <= 0
        or type(receipt.get("source_size_bytes")) is not int
        or int(receipt.get("source_size_bytes") or 0) <= 0
    ):
        _fail("ai_panorama_materialization_receipt_binding_mismatch")
    external_receipt = receipt.get("external_receipt")
    if (
        not isinstance(external_receipt, Mapping)
        or external_receipt.get("written") is not True
        or external_receipt.get("source_unchanged_post_write") is not True
        or external_receipt.get("candidate_unchanged_post_write") is not True
    ):
        _fail("ai_panorama_materialization_receipt_binding_mismatch")

    marker_path = live_candidate_root / AI_PANORAMA_CANDIDATE_MARKER_RELPATH
    marker, marker_bytes, marker_file_identity = _load_lineage_json(
        marker_path,
        code="ai_panorama_candidate_marker_invalid",
    )
    actual_marker_sha256 = hashlib.sha256(marker_bytes).hexdigest()
    if not hmac.compare_digest(expected_marker_sha256, actual_marker_sha256):
        _fail("ai_panorama_candidate_marker_sha256_mismatch")
    expected_marker_fields: dict[str, object] = {
        "contract_name": AI_PANORAMA_CANDIDATE_MARKER_CONTRACT,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "slug": slug,
        "source_tree_sha256": receipt.get("source_tree_sha256"),
        "source_file_count": receipt.get("source_file_count"),
        "source_size_bytes": receipt.get("source_size_bytes"),
        "core_manifest_sha256": core_manifest_sha256,
        "bundle_material_sha256": receipt.get("bundle_material_sha256"),
    }
    if any(
        not _exact_json_value(marker.get(key), value)
        for key, value in expected_marker_fields.items()
    ):
        _fail("ai_panorama_candidate_marker_binding_mismatch")
    if {
        "installer_source_tree_sha256",
        "installer_source_tour_sha256",
    }.intersection(marker):
        _fail("ai_panorama_candidate_marker_binding_mismatch")

    rechecked_snapshot = _scan_source_bundle(source_bundle)
    if (
        _directory_identity(
            live_candidate_root,
            code="ai_panorama_materialization_candidate_root_invalid",
        )
        != candidate_root_identity
        or _directory_identity(
            source_bundle.parent,
            code="ai_panorama_source_path_unsafe",
        )
        != candidate_root_identity
        or _directory_identity(
            source_bundle,
            code="ai_panorama_materialization_candidate_binding_mismatch",
        )
        != candidate_directory_identity
        or rechecked_snapshot.tree_sha256 != snapshot.tree_sha256
        or rechecked_snapshot.tour_sha256 != snapshot.tour_sha256
        or len(rechecked_snapshot.files) != len(snapshot.files)
        or rechecked_snapshot.total_bytes != snapshot.total_bytes
    ):
        _fail("ai_panorama_materialization_candidate_changed")
    rechecked_receipt, rechecked_receipt_bytes, rechecked_receipt_identity = (
        _load_lineage_json(
            receipt_path,
            code="ai_panorama_materialization_receipt_invalid",
        )
    )
    rechecked_marker, rechecked_marker_bytes, rechecked_marker_identity = (
        _load_lineage_json(
            marker_path,
            code="ai_panorama_candidate_marker_invalid",
        )
    )
    if (
        rechecked_receipt != receipt
        or rechecked_receipt_bytes != receipt_bytes
        or rechecked_receipt_identity != receipt_file_identity
    ):
        _fail("ai_panorama_materialization_receipt_changed")
    if (
        rechecked_marker != marker
        or rechecked_marker_bytes != marker_bytes
        or rechecked_marker_identity != marker_file_identity
    ):
        _fail("ai_panorama_candidate_marker_changed")
    final_snapshot = _scan_source_bundle(source_bundle)
    if (
        _directory_identity(
            live_candidate_root,
            code="ai_panorama_materialization_candidate_root_invalid",
        )
        != candidate_root_identity
        or _directory_identity(
            source_bundle.parent,
            code="ai_panorama_source_path_unsafe",
        )
        != candidate_root_identity
        or _directory_identity(
            source_bundle,
            code="ai_panorama_materialization_candidate_binding_mismatch",
        )
        != candidate_directory_identity
        or final_snapshot.tree_sha256 != snapshot.tree_sha256
        or final_snapshot.tour_sha256 != snapshot.tour_sha256
        or len(final_snapshot.files) != len(snapshot.files)
        or final_snapshot.total_bytes != snapshot.total_bytes
    ):
        _fail("ai_panorama_materialization_candidate_changed")
    return {
        "install_request_contract": request_contract,
        "release_eligible": "true" if release_eligible else "false",
        "materialization_receipt_sha256": expected_receipt_sha256,
        "candidate_marker_sha256": expected_marker_sha256,
    }


def _declared_public_files(
    source_bundle: Path,
    payload: Mapping[str, object],
) -> set[str]:
    declared = {"tour.json"}
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, Mapping):
        _fail("ai_panorama_walkable_scene_missing")
    floorplan_relpath = _hosted_property_tour_public_asset_relpath(
        walkable_scene.get("floorplan_relpath")
    )
    if not floorplan_relpath:
        _fail("ai_panorama_floorplan_relpath_invalid")
    declared.add(_safe_relpath(floorplan_relpath))
    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, Mapping):
        scenes = tuple(raw_scenes.values())
    elif isinstance(raw_scenes, list):
        scenes = tuple(raw_scenes)
    else:
        _fail("ai_panorama_scenes_invalid")
    for scene in scenes:
        if not isinstance(scene, Mapping):
            _fail("ai_panorama_scenes_invalid")
        values = {
            _hosted_property_tour_public_asset_relpath(scene.get(key))
            for key in (
                "asset_relpath",
                "panorama_relpath",
                "equirect_relpath",
                "image_relpath",
            )
            if str(scene.get(key) or "").strip()
        }
        if "" in values or len(values) != 1:
            _fail("ai_panorama_scene_asset_invalid")
        declared.add(_safe_relpath(next(iter(values))))
    acceptance = walkable_scene.get("acceptance")
    if not isinstance(acceptance, Mapping):
        _fail("ai_panorama_acceptance_missing")
    for key in ("provenance_relpath", "browser_receipt_relpath"):
        relpath = _hosted_property_tour_public_asset_relpath(acceptance.get(key))
        if not relpath:
            _fail("ai_panorama_proof_relpath_invalid")
        declared.add(_safe_relpath(relpath))
    browser_relpath = _safe_relpath(str(acceptance.get("browser_receipt_relpath") or ""))
    browser_encoded = _open_regular_nofollow(
        source_bundle / browser_relpath,
        maximum_bytes=_SOURCE_MANIFEST_MAX_BYTES,
        code="ai_panorama_browser_receipt_invalid",
    )
    try:
        browser_receipt = json.loads(browser_encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError("ai_panorama_browser_receipt_invalid") from exc
    if not isinstance(browser_receipt, Mapping):
        _fail("ai_panorama_browser_receipt_invalid")
    for surface in ("desktop", "mobile", "dollhouse"):
        surface_receipt = browser_receipt.get(surface)
        if not isinstance(surface_receipt, Mapping):
            _fail("ai_panorama_browser_receipt_invalid")
        relpath = _hosted_property_tour_public_asset_relpath(
            surface_receipt.get("screenshot_relpath")
        )
        if not relpath:
            _fail("ai_panorama_browser_receipt_invalid")
        declared.add(_safe_relpath(relpath))
    return declared


def _validate_source_identity(
    *,
    request: Mapping[str, object],
    source_bundle: Path,
    snapshot: _SourceSnapshot,
    payload: dict[str, object],
    apply: bool,
    governed_admission: bool = False,
) -> dict[str, str]:
    expected_slug = str(request.get("expected_slug") or "").strip()
    if not _SAFE_SLUG_PATTERN.fullmatch(expected_slug):
        _fail("ai_panorama_expected_slug_invalid")
    if str(payload.get("slug") or "").strip() != expected_slug:
        _fail("ai_panorama_source_slug_mismatch")
    if _PRIVATE_MANIFEST_KEYS.intersection(payload):
        _fail("ai_panorama_source_public_manifest_contains_private_identity")

    principal_id = _require_principal(request.get("principal_id"))
    search_run_id = _require_safe_id(
        request.get("search_run_id"), code="ai_panorama_search_run_id_invalid"
    )
    candidate_ref = _require_safe_id(
        request.get("candidate_ref"), code="ai_panorama_candidate_ref_invalid"
    )
    external_id = _require_safe_id(
        request.get("external_id"), code="ai_panorama_external_id_invalid"
    )
    source_ref = str(request.get("source_ref") or "").strip()
    if (
        not source_ref
        or len(source_ref) > 512
        or ":" not in source_ref
        or any(ord(character) < 32 or ord(character) == 127 for character in source_ref)
    ):
        _fail("ai_panorama_source_ref_invalid")
    raw_source_provider, _separator, raw_source_listing_id = source_ref.partition(":")
    if not raw_source_provider.strip() or raw_source_listing_id.strip() != external_id:
        _fail("ai_panorama_source_ref_identity_mismatch")

    provider_key = _normalized_provider_key(request.get("provider_key"))
    if not provider_key:
        _fail("ai_panorama_provider_key_invalid")
    source_provider, source_listing_id = _source_ref_identity(source_ref)
    if source_listing_id != external_id or (
        source_provider and source_provider != provider_key
    ):
        _fail("ai_panorama_source_ref_identity_mismatch")

    listing_url = str(request.get("listing_url") or "").strip()
    canonical_listing_url = canonical_property_source_url(listing_url)
    if not canonical_listing_url or canonical_listing_url != listing_url:
        _fail("ai_panorama_listing_url_not_canonical")
    if _provider_key_from_url(canonical_listing_url) != provider_key:
        _fail("ai_panorama_provider_url_mismatch")
    if not _property_url_contains_listing_id(canonical_listing_url, external_id):
        _fail("ai_panorama_listing_url_identity_mismatch")
    property_url_sha256 = property_search_source_url_sha256(canonical_listing_url)
    if str(payload.get("property_url_sha256") or "").strip().lower() != property_url_sha256:
        _fail("ai_panorama_property_url_sha256_mismatch")

    expected_tree_sha256 = _require_digest(
        request.get("expected_source_tree_sha256"),
        code="ai_panorama_expected_source_tree_sha256_invalid",
        required=apply,
    )
    expected_tour_sha256 = _require_digest(
        request.get("expected_tour_sha256"),
        code="ai_panorama_expected_tour_sha256_invalid",
        required=apply,
    )
    if expected_tree_sha256 and not hmac.compare_digest(
        expected_tree_sha256, snapshot.tree_sha256
    ):
        _fail("ai_panorama_source_tree_sha256_mismatch")
    if expected_tour_sha256 and not hmac.compare_digest(
        expected_tour_sha256, snapshot.tour_sha256
    ):
        _fail("ai_panorama_source_tour_sha256_mismatch")

    contract = _hosted_property_tour_ai_panorama_contract(
        bundle_dir=source_bundle,
        payload=payload,
        mode="full",
    )
    if contract.get("ready") is not True:
        reason = str(contract.get("reason") or "strict_contract_failed").strip()
        raise AiPanoramaIntakeError(f"ai_panorama_strict_contract:{reason}")
    if str(contract.get("property_url_sha256") or "").strip().lower() != property_url_sha256:
        _fail("ai_panorama_strict_property_binding_mismatch")
    core_manifest_sha256 = _require_digest(
        contract.get("core_manifest_sha256"),
        code="ai_panorama_core_manifest_sha256_invalid",
        required=True,
    )
    expected_core_manifest_sha256 = _require_digest(
        request.get("expected_core_manifest_sha256"),
        code="ai_panorama_expected_core_manifest_sha256_invalid",
        required=False,
    )
    if expected_core_manifest_sha256 and not hmac.compare_digest(
        expected_core_manifest_sha256,
        core_manifest_sha256,
    ):
        _fail("ai_panorama_core_manifest_sha256_mismatch")

    declared = _declared_public_files(source_bundle, payload)
    actual = {row.relpath for row in snapshot.files}
    if declared != actual:
        _fail("ai_panorama_source_file_set_mismatch")
    acceptance = dict(dict(payload.get("walkable_scene") or {}).get("acceptance") or {})
    provenance_relpath = _safe_relpath(str(acceptance.get("provenance_relpath") or ""))
    provenance_encoded = _open_regular_nofollow(
        source_bundle / provenance_relpath,
        maximum_bytes=_SOURCE_MANIFEST_MAX_BYTES,
        code="ai_panorama_provenance_invalid",
    )
    try:
        provenance = json.loads(provenance_encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError("ai_panorama_provenance_invalid") from exc
    expected_binding_kind = f"{provider_key}_source_listing_url_sha256"
    if (
        not isinstance(provenance, Mapping)
        or str(provenance.get("property_binding_kind") or "").strip()
        != expected_binding_kind
        or str(provenance.get("property_url_sha256") or "").strip().lower()
        != property_url_sha256
    ):
        _fail("ai_panorama_provider_qualified_provenance_mismatch")
    identity = {
        "principal_id": principal_id,
        "search_run_id": search_run_id,
        "candidate_ref": candidate_ref,
        "external_id": external_id,
        "source_ref": source_ref,
        "provider_key": provider_key,
        "listing_url": canonical_listing_url,
        "property_url_sha256": property_url_sha256,
        "core_manifest_sha256": core_manifest_sha256,
    }
    identity.update(
        _validate_materialization_lineage(
            request=request,
            source_bundle=source_bundle,
            snapshot=snapshot,
            slug=expected_slug,
            core_manifest_sha256=identity["core_manifest_sha256"],
            governed_admission=governed_admission,
        )
    )
    return identity


def _validate_v2_publication_authority(
    *,
    request: Mapping[str, object],
    identity: Mapping[str, str],
    connection: object | None = None,
    for_update: bool = False,
    expected_record_sha256: str = "",
) -> dict[str, str]:
    if request.get("contract") != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2:
        return {}
    record = load_property_search_run_record_for_publication(
        run_id=identity["search_run_id"],
        principal_id=identity["principal_id"],
        connection=connection,
        for_update=for_update,
    )
    if not isinstance(record, Mapping):
        _fail("ai_panorama_publication_run_not_found")
    bundle_identity: dict[str, object] = {
        "owner_verified": True,
        "search_run_id": identity["search_run_id"],
        "candidate_ref": identity["candidate_ref"],
        "listing_url": identity["listing_url"],
        "property_url": identity["listing_url"],
        "property_url_sha256": identity["property_url_sha256"],
        "provider_key": identity["provider_key"],
        "source_ref": identity["source_ref"],
        "external_id": identity["external_id"],
    }
    try:
        authority = authorize_property_search_candidate_tour_install(
            record,
            principal_id=identity["principal_id"],
            run_id=identity["search_run_id"],
            candidate_ref=identity["candidate_ref"],
            expected_listing_id=identity["external_id"],
            expected_source_ref=identity["source_ref"],
            bundle_identity=bundle_identity,
        )
    except PropertySearchTourBindingError as exc:
        raise AiPanoramaIntakeError(
            f"ai_panorama_publication_authority:{exc.code}"
        ) from exc
    record_sha256 = _require_digest(
        authority.get("record_sha256"),
        code="ai_panorama_publication_record_sha256_invalid",
        required=True,
    )
    expected_record_sha256 = _require_digest(
        expected_record_sha256,
        code="ai_panorama_expected_publication_record_sha256_invalid",
        required=False,
    )
    if expected_record_sha256 and not hmac.compare_digest(
        expected_record_sha256,
        record_sha256,
    ):
        _fail("ai_panorama_publication_record_drift")
    return {
        "publication_authorization_verified": "true",
        "publication_authorization_record_sha256": record_sha256,
        "principal_binding_verified": "true",
        "run_binding_verified": "true",
        "candidate_binding_verified": "true",
        "listing_identity_verified": "true",
        "source_identity_verified": "true",
        "run_terminal_verified": "true",
    }


def _copy_snapshot(source: Path, stage: Path, snapshot: _SourceSnapshot) -> None:
    for row in snapshot.files:
        destination = stage / row.relpath
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        source_path = source / row.relpath
        source_fd = -1
        destination_fd = -1
        digest = hashlib.sha256()
        try:
            source_fd = os.open(
                source_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(source_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != row.device
                or opened.st_ino != row.inode
                or opened.st_size != row.size_bytes
                or opened.st_mtime_ns != row.modified_ns
            ):
                _fail("ai_panorama_source_changed")
            destination_fd = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            if digest.hexdigest() != row.sha256 or os.fstat(source_fd).st_mtime_ns != row.modified_ns:
                _fail("ai_panorama_source_changed")
            os.fchmod(destination_fd, 0o644)
            os.fsync(destination_fd)
        except AiPanoramaIntakeError:
            raise
        except OSError as exc:
            raise AiPanoramaIntakeError("ai_panorama_stage_copy_failed") from exc
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
            if source_fd >= 0:
                os.close(source_fd)
    if _scan_source_bundle(source).tree_sha256 != snapshot.tree_sha256:
        _fail("ai_panorama_source_changed")


def _load_json_manifest(path: Path, *, code: str) -> dict[str, object]:
    encoded = _open_regular_nofollow(path, maximum_bytes=_SOURCE_MANIFEST_MAX_BYTES, code=code)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaIntakeError(code) from exc
    if not isinstance(payload, dict):
        _fail(code)
    return dict(payload)


def _fsync_directory_tree(root: Path) -> None:
    directories = [root]
    for current_raw, directory_names, _file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_raw)
        for directory_name in directory_names:
            candidate = current / directory_name
            details = candidate.stat(follow_symlinks=False)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                _fail("ai_panorama_stage_directory_invalid")
            directories.append(candidate)
    for directory in reversed(directories):
        descriptor = -1
        try:
            descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            os.fsync(descriptor)
        except OSError as exc:
            raise AiPanoramaIntakeError("ai_panorama_stage_fsync_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _apply_runtime_ownership(
    root: Path,
    *,
    uid: int,
    gid: int,
    expected_device: int,
) -> None:
    """Make a staged tree inherit the controller-verified volume owner."""

    paths: list[Path] = []
    for current_raw, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_raw)
        paths.append(current)
        for name in sorted(directory_names):
            paths.append(current / name)
        for name in sorted(file_names):
            paths.append(current / name)
    unique_paths = sorted(
        set(paths),
        key=lambda value: (len(value.parts), value.as_posix()),
        reverse=True,
    )
    for path in unique_paths:
        try:
            details = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_stage_ownership_failed"
            ) from exc
        if stat.S_ISLNK(details.st_mode) or not (
            stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)
        ) or int(details.st_dev) != expected_device:
            _fail("ai_panorama_stage_ownership_failed")
        if details.st_uid == uid and details.st_gid == gid:
            continue
        if os.geteuid() != 0:
            _fail("ai_panorama_stage_ownership_failed")
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except OSError as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_stage_ownership_failed"
            ) from exc
        observed = path.stat(follow_symlinks=False)
        if (
            observed.st_dev != details.st_dev
            or observed.st_ino != details.st_ino
            or observed.st_uid != uid
            or observed.st_gid != gid
        ):
            _fail("ai_panorama_stage_ownership_failed")


def _semantic_manifest_sha256(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AiPanoramaIntakeError("ai_panorama_manifest_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_existing_target(
    *,
    target: Path,
    source_payload: Mapping[str, object],
    snapshot: _SourceSnapshot,
    identity: Mapping[str, str],
) -> bool:
    try:
        details = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AiPanoramaIntakeError("ai_panorama_target_invalid") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        _fail("ai_panorama_target_invalid")
    expected_public_device = int(
        identity.get("public_tour_root_device") or details.st_dev
    )
    if int(details.st_dev) != expected_public_device:
        _fail("ai_panorama_target_volume_crossing_forbidden")
    private_payload = _load_hosted_property_tour_private_receipt(target)
    existing_owner = str(private_payload.get("principal_id") or "").strip()
    if not existing_owner:
        _fail("ai_panorama_target_owner_receipt_missing")
    if not hmac.compare_digest(existing_owner, identity["principal_id"]):
        _fail("ai_panorama_target_owner_mismatch")
    expected_private = {
        "search_run_id": identity["search_run_id"],
        "candidate_ref": identity["candidate_ref"],
        "listing_url": identity["listing_url"],
        "property_url": identity["listing_url"],
        "source_ref": identity["source_ref"],
        "external_id": identity["external_id"],
    }
    if any(str(private_payload.get(key) or "").strip() != value for key, value in expected_private.items()):
        _fail("ai_panorama_target_private_identity_conflict")
    installed_payload = _load_json_manifest(
        target / "tour.json", code="ai_panorama_target_manifest_invalid"
    )
    installed_contract = _hosted_property_tour_ai_panorama_contract(
        bundle_dir=target,
        payload=installed_payload,
        mode="full",
    )
    if installed_contract.get("ready") is not True:
        _fail("ai_panorama_target_contract_invalid")
    if (
        str(installed_contract.get("core_manifest_sha256") or "")
        != identity["core_manifest_sha256"]
        or _semantic_manifest_sha256(installed_payload)
        != _semantic_manifest_sha256(source_payload)
    ):
        _fail("ai_panorama_target_replace_forbidden")
    expected_paths = {row.relpath for row in snapshot.files} | {"tour.private.json"}
    observed_paths: set[str] = set()
    for current_raw, directory_names, file_names in os.walk(target, topdown=True, followlinks=False):
        current = Path(current_raw)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            directory_path = current / directory_name
            directory_details = directory_path.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(directory_details.st_mode)
                or not stat.S_ISDIR(directory_details.st_mode)
                or int(directory_details.st_dev) != expected_public_device
            ):
                _fail("ai_panorama_target_invalid")
        for file_name in file_names:
            file_path = current / file_name
            relpath = _safe_relpath(file_path.relative_to(target).as_posix())
            details = file_path.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISREG(details.st_mode)
                or int(details.st_dev) != expected_public_device
            ):
                _fail("ai_panorama_target_invalid")
            observed_paths.add(relpath)
    if observed_paths != expected_paths:
        _fail("ai_panorama_target_replace_forbidden")
    source_by_path = {row.relpath: row for row in snapshot.files}
    for relpath, source_row in source_by_path.items():
        if relpath == "tour.json":
            continue
        details = (target / relpath).stat(follow_symlinks=False)
        if _hash_regular_file(target / relpath, details=details) != source_row.sha256:
            _fail("ai_panorama_target_replace_forbidden")
    return True


def inspect_ai_panorama_historical_publication_target(
    request: Mapping[str, object],
    *,
    historical_consumption: object,
) -> dict[str, object]:
    """Observe exact source/target identity without granting write authority."""

    from app.product.property_tour_ai_panorama_admission import (
        VerifiedAiPanoramaHistoricalConsumptionProof,
    )

    if (
        type(historical_consumption)
        is not VerifiedAiPanoramaHistoricalConsumptionProof
        or request.get("contract")
        != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2
    ):
        _fail("ai_panorama_historical_observation_invalid")
    source_bundle = Path(str(request.get("source_bundle") or ""))
    public_root = Path(str(request.get("public_tour_dir") or ""))
    expected_device = request.get("public_tour_root_device")
    expected_inode = request.get("public_tour_root_inode")
    if (
        not source_bundle.is_absolute()
        or not public_root.is_absolute()
        or type(expected_device) is not int
        or type(expected_inode) is not int
        or expected_device < 1
        or expected_inode < 1
    ):
        _fail("ai_panorama_historical_observation_invalid")
    root_before = public_root.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(root_before.st_mode)
        or not stat.S_ISDIR(root_before.st_mode)
        or int(root_before.st_dev) != expected_device
        or int(root_before.st_ino) != expected_inode
    ):
        _fail("ai_panorama_public_tour_root_invalid")
    snapshot = _scan_source_bundle(source_bundle)
    source_payload = _load_json_manifest(
        source_bundle / "tour.json",
        code="ai_panorama_source_manifest_invalid",
    )
    identity = _validate_source_identity(
        request=request,
        source_bundle=source_bundle,
        snapshot=snapshot,
        payload=source_payload,
        apply=True,
        governed_admission=True,
    )
    identity["public_tour_root_device"] = str(expected_device)
    target = public_root / str(request.get("expected_slug") or "")
    exact = _validate_existing_target(
        target=target,
        source_payload=source_payload,
        snapshot=snapshot,
        identity=identity,
    )
    root_after = public_root.stat(follow_symlinks=False)
    if (
        int(root_after.st_dev) != expected_device
        or int(root_after.st_ino) != expected_inode
    ):
        _fail("ai_panorama_public_tour_root_changed")
    return {
        "state": "exact" if exact else "absent",
        "source_tree_sha256": snapshot.tree_sha256,
        "source_tour_sha256": snapshot.tour_sha256,
        "core_manifest_sha256": identity["core_manifest_sha256"],
        "public_root_device": expected_device,
        "public_root_inode": expected_inode,
        "private_values_redacted": True,
    }


def _publication_bundle_identity(
    *,
    bundle_dir: Path,
    identity: Mapping[str, str],
) -> dict[str, object]:
    private_payload = _load_hosted_property_tour_private_receipt(bundle_dir)
    expected_private = {
        "principal_id": identity["principal_id"],
        "search_run_id": identity["search_run_id"],
        "candidate_ref": identity["candidate_ref"],
        "listing_url": identity["listing_url"],
        "property_url": identity["listing_url"],
        "source_ref": identity["source_ref"],
        "external_id": identity["external_id"],
    }
    if any(
        str(private_payload.get(key) or "").strip() != value
        for key, value in expected_private.items()
    ):
        _fail("ai_panorama_publication_owner_receipt_mismatch")
    return {
        **expected_private,
        "owner_verified": True,
        "property_url_sha256": identity["property_url_sha256"],
        "provider_key": identity["provider_key"],
    }


def _bind_candidate_in_publication_transaction(
    *,
    connection: object,
    bundle_dir: Path,
    request: Mapping[str, object],
    identity: Mapping[str, str],
    publication_admission: object = None,
) -> dict[str, str]:
    """Persist the candidate binding before the target rename can commit."""

    if connection is None:
        _fail("ai_panorama_publication_binding_durable_storage_required")
    slug = str(request.get("expected_slug") or "").strip()
    public_control_url = str(request.get("public_control_url") or "").strip()
    expected_public_control_url = (
        f"https://propertyquarry.com/tours/{slug}/control"
    )
    if not public_control_url or not hmac.compare_digest(
        public_control_url,
        expected_public_control_url,
    ):
        _fail("ai_panorama_publication_control_url_invalid")
    record = load_property_search_run_record_for_publication(
        run_id=identity["search_run_id"],
        principal_id=identity["principal_id"],
        connection=connection,
        for_update=True,
    )
    if not isinstance(record, Mapping):
        _fail("ai_panorama_publication_run_not_found")
    expected_record_sha256 = _require_digest(
        identity.get("expected_publication_record_sha256"),
        code="ai_panorama_expected_publication_record_sha256_invalid",
        required=True,
    )
    observed_record_sha256 = property_search_run_record_sha256(record)
    if not hmac.compare_digest(
        observed_record_sha256,
        expected_record_sha256,
    ):
        _fail("ai_panorama_publication_record_drift")
    bundle_identity = _publication_bundle_identity(
        bundle_dir=bundle_dir,
        identity=identity,
    )
    binding_arguments = {
        "principal_id": identity["principal_id"],
        "run_id": identity["search_run_id"],
        "candidate_ref": identity["candidate_ref"],
        "expected_listing_id": identity["external_id"],
        "generated_reconstruction_url": public_control_url,
        "bundle_identity": bundle_identity,
        "reconstruction_kind": "ai_panorama_360",
    }
    governed_prater_binding = governed_prater_control_url_reserved(
        public_control_url
    )

    def _plan(
        binding_record: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        if governed_prater_binding:
            return plan_governed_prater_candidate_tour_binding(
                binding_record,
                publication_admission=publication_admission,
                bound_at=str(
                    request.get("publication_binding_bound_at") or ""
                ),
                **binding_arguments,
            )
        return plan_property_search_candidate_tour_binding(
            binding_record,
            **binding_arguments,
        )

    try:
        updated_record, binding_receipt = _plan(record)
    except PropertySearchTourBindingError as exc:
        raise AiPanoramaIntakeError(
            f"ai_panorama_publication_binding:{exc.code}"
        ) from exc
    before_sha256 = _require_digest(
        binding_receipt.get("before_sha256"),
        code="ai_panorama_publication_binding_before_invalid",
        required=True,
    )
    after_sha256 = _require_digest(
        binding_receipt.get("after_sha256"),
        code="ai_panorama_publication_binding_after_invalid",
        required=True,
    )
    if governed_prater_binding:
        expected_before_sha256 = _require_digest(
            request.get("publication_binding_expected_before_sha256"),
            code="ai_panorama_publication_binding_before_invalid",
            required=True,
        )
        expected_after_sha256 = _require_digest(
            request.get("publication_binding_expected_after_sha256"),
            code="ai_panorama_publication_binding_after_invalid",
            required=True,
        )
        expected_status = str(
            request.get("publication_binding_expected_status") or ""
        ).strip()
        observed_status = (
            "change-required"
            if binding_receipt.get("changed") is True
            else "already-bound"
        )
        if (
            not hmac.compare_digest(
                before_sha256,
                expected_before_sha256,
            )
            or not hmac.compare_digest(
                after_sha256,
                expected_after_sha256,
            )
            or expected_status != observed_status
        ):
            _fail("ai_panorama_publication_binding_plan_drift")
    if not hmac.compare_digest(before_sha256, expected_record_sha256):
        _fail("ai_panorama_publication_record_drift")
    if binding_receipt.get("changed") is True:
        try:
            cas_result = update_property_search_run_record_for_publication(
                connection,
                principal_id=identity["principal_id"],
                run_id=identity["search_run_id"],
                expected_record_sha256=expected_record_sha256,
                updated_record=updated_record,
            )
        except Exception as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_publication_binding_store_failed"
            ) from exc
        if (
            str(cas_result.get("status") or "").strip() != "applied"
            or not isinstance(cas_result.get("record"), Mapping)
            or not hmac.compare_digest(
                str(cas_result.get("record_sha256") or "").strip().lower(),
                after_sha256,
            )
        ):
            _fail("ai_panorama_publication_binding_store_rejected")
        persisted_record = dict(cas_result["record"])
        try:
            _unchanged_record, verified_receipt = _plan(persisted_record)
        except PropertySearchTourBindingError as exc:
            raise AiPanoramaIntakeError(
                f"ai_panorama_publication_binding:{exc.code}"
            ) from exc
        if (
            verified_receipt.get("changed") is not False
            or not hmac.compare_digest(
                str(verified_receipt.get("before_sha256") or "").strip().lower(),
                after_sha256,
            )
        ):
            _fail("ai_panorama_publication_binding_store_verification_failed")
        binding_status = "applied"
    elif hmac.compare_digest(before_sha256, after_sha256):
        binding_status = "already_bound"
    else:
        _fail("ai_panorama_publication_binding_plan_invalid")
    return {
        "publication_binding_verified": "true",
        "publication_binding_status": binding_status,
        "publication_binding_before_sha256": before_sha256,
        "publication_binding_after_sha256": after_sha256,
    }


def _classify_publication_commit_outcome(
    *,
    bundle_dir: Path,
    request: Mapping[str, object],
    identity: Mapping[str, str],
    publication_admission: object = None,
) -> str:
    """Re-read exact durable state after an ambiguous transaction exit."""

    before_sha256 = _require_digest(
        identity.get("publication_binding_before_sha256"),
        code="ai_panorama_publication_binding_before_invalid",
        required=True,
    )
    after_sha256 = _require_digest(
        identity.get("publication_binding_after_sha256"),
        code="ai_panorama_publication_binding_after_invalid",
        required=True,
    )
    try:
        with property_account_publication_authority(
            identity["principal_id"],
            run_id=identity["search_run_id"],
        ) as connection:
            record = load_property_search_run_record_for_publication(
                run_id=identity["search_run_id"],
                principal_id=identity["principal_id"],
                connection=connection,
                for_update=True,
            )
            if not isinstance(record, Mapping):
                return "ambiguous"
            observed_sha256 = property_search_run_record_sha256(record)
            bundle_identity = _publication_bundle_identity(
                bundle_dir=bundle_dir,
                identity=identity,
            )
            public_control_url = str(
                request.get("public_control_url") or ""
            ).strip()
            binding_arguments = {
                "principal_id": identity["principal_id"],
                "run_id": identity["search_run_id"],
                "candidate_ref": identity["candidate_ref"],
                "expected_listing_id": identity["external_id"],
                "generated_reconstruction_url": public_control_url,
                "bundle_identity": bundle_identity,
                "reconstruction_kind": "ai_panorama_360",
            }
            if governed_prater_control_url_reserved(public_control_url):
                updated_record, binding_receipt = (
                    plan_governed_prater_candidate_tour_binding(
                        record,
                        publication_admission=publication_admission,
                        **binding_arguments,
                    )
                )
            else:
                updated_record, binding_receipt = (
                    plan_property_search_candidate_tour_binding(
                        record,
                        **binding_arguments,
                    )
                )
            planned_before = str(
                binding_receipt.get("before_sha256") or ""
            ).strip().lower()
            planned_after = str(
                binding_receipt.get("after_sha256") or ""
            ).strip().lower()
            if (
                hmac.compare_digest(observed_sha256, after_sha256)
                and binding_receipt.get("changed") is False
                and hmac.compare_digest(planned_before, after_sha256)
                and hmac.compare_digest(planned_after, after_sha256)
                and hmac.compare_digest(
                    property_search_run_record_sha256(updated_record),
                    after_sha256,
                )
            ):
                return "committed"
            if (
                not hmac.compare_digest(before_sha256, after_sha256)
                and hmac.compare_digest(observed_sha256, before_sha256)
                and binding_receipt.get("changed") is True
                and hmac.compare_digest(planned_before, before_sha256)
            ):
                return "uncommitted"
    except Exception:
        return "ambiguous"
    return "ambiguous"


def _raise_publication_transaction_failure(
    exc: Exception,
    *,
    rollback_performed: bool,
    publication_outcome: str,
) -> None:
    ambiguous = publication_outcome == "ambiguous"
    if isinstance(exc, AiPanoramaIntakeError):
        exc.rollback_performed = rollback_performed
        exc.commit_outcome_ambiguous = ambiguous
        exc.publication_outcome = publication_outcome
        raise exc
    wrapped = AiPanoramaIntakeError(
        "ai_panorama_publication_transaction_failed"
    )
    wrapped.rollback_performed = rollback_performed
    wrapped.commit_outcome_ambiguous = ambiguous
    wrapped.publication_outcome = publication_outcome
    raise wrapped from exc


def _remove_newly_installed_target_after_transaction_failure(
    *,
    public_tour_dir: Path,
    target: Path,
    source_payload: Mapping[str, object],
    snapshot: _SourceSnapshot,
    identity: Mapping[str, str],
) -> None:
    """Remove only the exact just-published target while its lock is held."""

    if not _validate_existing_target(
        target=target,
        source_payload=source_payload,
        snapshot=snapshot,
        identity=identity,
    ):
        _fail("ai_panorama_compensating_removal_target_missing")
    tombstone = public_tour_dir / f".{target.name}.ai-rollback-{uuid4().hex}"
    if tombstone.exists() or tombstone.is_symlink():
        _fail("ai_panorama_compensating_removal_target_conflict")
    try:
        os.rename(target, tombstone)
        directory_fd = os.open(
            public_tour_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if not _validate_existing_target(
            target=tombstone,
            source_payload=source_payload,
            snapshot=snapshot,
            identity=identity,
        ):
            _fail("ai_panorama_compensating_removal_identity_changed")
        shutil.rmtree(tombstone)
        directory_fd = os.open(
            public_tour_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except AiPanoramaIntakeError:
        raise
    except Exception as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_compensating_removal_failed"
        ) from exc


def _receipt(
    *,
    status: str,
    applied: bool,
    already_installed: bool,
    slug: str,
    snapshot: _SourceSnapshot,
    identity: Mapping[str, str],
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "contract": AI_PANORAMA_INSTALL_RECEIPT_CONTRACT,
        "status": status,
        "mode": "apply" if applied else "dry_run",
        "applied": applied and not already_installed,
        "already_installed": already_installed,
        "slug": slug,
        "control_path": f"/tours/{slug}/control",
        "representation_kind": "ai_panorama_360",
        "provider_key": identity["provider_key"],
        "property_url_sha256": identity["property_url_sha256"],
        "core_manifest_sha256": identity["core_manifest_sha256"],
        "source_identity_contract": AI_PANORAMA_INSTALL_SOURCE_IDENTITY_CONTRACT,
        "source_tree_algorithm": AI_PANORAMA_INSTALL_SOURCE_TREE_ALGORITHM,
        "source_relative_root": AI_PANORAMA_INSTALL_SOURCE_RELATIVE_ROOT,
        "source_relative_path_semantics": AI_PANORAMA_INSTALL_SOURCE_RELATIVE_PATH_SEMANTICS,
        "source_tree_sha256": snapshot.tree_sha256,
        "source_tour_sha256": snapshot.tour_sha256,
        "source_file_count": len(snapshot.files),
        "source_total_bytes": snapshot.total_bytes,
        "principal_binding_verified": identity.get("principal_binding_verified") == "true",
        "run_binding_verified": identity.get("run_binding_verified") == "true",
        "candidate_binding_verified": identity.get("candidate_binding_verified") == "true",
        "listing_identity_verified": identity.get("listing_identity_verified") == "true",
        "source_identity_verified": identity.get("source_identity_verified") == "true",
        "run_terminal_verified": identity.get("run_terminal_verified") == "true",
        "publication_authorization_verified": (
            identity.get("publication_authorization_verified") == "true"
        ),
        "controller_permit_verified": (
            identity.get("controller_permit_verified") == "true"
        ),
        "authenticated_principal_verified": (
            identity.get("authenticated_principal_verified") == "true"
        ),
        "controller_nonce_consumed": (
            identity.get("controller_nonce_consumed") == "true"
        ),
        "public_tour_volume_profile_verified": (
            identity.get("public_tour_volume_profile_verified") == "true"
        ),
        "publication_binding_verified": (
            identity.get("publication_binding_verified") == "true"
        ),
        "install_request_contract": identity["install_request_contract"],
        "materialization_lineage_verified": bool(
            identity.get("materialization_receipt_sha256")
            and identity.get("candidate_marker_sha256")
        ),
        "release_eligible": (
            identity.get("release_eligible") == "true"
            and identity.get("publication_authorization_verified") == "true"
            and identity.get("controller_permit_verified") == "true"
            and identity.get("authenticated_principal_verified") == "true"
            and (
                not applied
                or identity.get("publication_binding_verified") == "true"
            )
            and (
                not applied
                or identity.get("controller_nonce_consumed") == "true"
            )
        ),
        "private_values_redacted": True,
    }
    if identity.get("controller_permit_sha256"):
        receipt["controller_permit_sha256"] = identity["controller_permit_sha256"]
    if identity.get("publication_binding_status"):
        receipt["publication_binding_status"] = identity[
            "publication_binding_status"
        ]
        receipt["publication_binding_before_sha256"] = identity[
            "publication_binding_before_sha256"
        ]
        receipt["publication_binding_after_sha256"] = identity[
            "publication_binding_after_sha256"
        ]
    if identity.get("publication_authorization_record_sha256"):
        receipt["publication_authorization_record_sha256"] = identity[
            "publication_authorization_record_sha256"
        ]
    if identity.get("materialization_receipt_sha256"):
        receipt["materialization_receipt_sha256"] = identity[
            "materialization_receipt_sha256"
        ]
        receipt["candidate_marker_sha256"] = identity[
            "candidate_marker_sha256"
        ]
    return receipt


def install_sealed_ai_panorama_bundle(
    request: Mapping[str, object],
    *,
    apply: bool = False,
    publication_admission: object = None,
    artifact_preflight_only: bool = False,
) -> dict[str, object]:
    """Validate or atomically install a first-party AI panorama bundle.

    Dry-run is the default. Apply is a CAS operation over both the complete
    source tree and its source tour manifest. The returned receipt intentionally
    contains no principal, run, candidate, source-ref, external-id, or URL.
    """

    if request.get("contract") not in {
        AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1,
        AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
    }:
        _fail("ai_panorama_request_contract_invalid")
    if apply and request.get("contract") != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2:
        _fail("ai_panorama_v1_apply_forbidden")
    if (
        type(artifact_preflight_only) is not bool
        or (artifact_preflight_only and apply)
        or (artifact_preflight_only and publication_admission is None)
        or (
            artifact_preflight_only
            and request.get("contract")
            != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2
        )
    ):
        _fail("ai_panorama_artifact_preflight_invalid")
    controller_identity: dict[str, str] = {}
    admitted_incoming_root: Path | None = None
    admitted_public_tour_dir: Path | None = None
    if apply or publication_admission is not None:
        if request.get("contract") != AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2:
            _fail("ai_panorama_controller_admission_contract_invalid")
        (
            effective_request,
            controller_identity,
            admitted_incoming_root,
            admitted_public_tour_dir,
        ) = _controller_publication_admission(
            request,
            publication_admission,
            require_consumed=apply,
        )
        request = effective_request
    source_bundle = _confined_source_bundle(
        _request_path(
            request.get("source_bundle"), code="ai_panorama_source_path_invalid"
        ),
        admitted_incoming_root=admitted_incoming_root,
    )
    requested_public_tour_dir = _request_path(
        request.get("public_tour_dir"), code="ai_panorama_public_dir_invalid"
    )
    public_tour_dir = _request_path(
        (
            admitted_public_tour_dir
            if admitted_public_tour_dir is not None
            else _public_tour_dir()
        ),
        code="ai_panorama_configured_public_dir_invalid",
    )
    requested_identity = _directory_identity(
        requested_public_tour_dir,
        code="ai_panorama_public_dir_invalid",
    )
    configured_identity = _directory_identity(
        public_tour_dir,
        code="ai_panorama_configured_public_dir_invalid",
    )
    public_root_details = public_tour_dir.stat(follow_symlinks=False)
    public_runtime_uid = int(public_root_details.st_uid)
    public_runtime_gid = int(public_root_details.st_gid)
    if controller_identity and configured_identity != (
        int(controller_identity["public_tour_root_device"]),
        int(controller_identity["public_tour_root_inode"]),
    ):
        _fail("ai_panorama_controller_public_volume_identity_changed")
    if requested_identity != configured_identity:
        _fail("ai_panorama_public_dir_not_configured")
    snapshot = _scan_source_bundle(source_bundle)
    source_payload = _load_source_manifest(source_bundle)
    identity = _validate_source_identity(
        request=request,
        source_bundle=source_bundle,
        snapshot=snapshot,
        payload=source_payload,
        apply=apply,
        governed_admission=bool(controller_identity),
    )
    identity.update(controller_identity)
    slug = str(request.get("expected_slug") or "").strip()
    target = public_tour_dir / slug

    if not apply:
        if artifact_preflight_only:
            identity.update(
                {
                    "release_eligible": "false",
                    "publication_authorization_verified": "false",
                }
            )
        else:
            identity.update(
                _validate_v2_publication_authority(
                    request=request,
                    identity=identity,
                    expected_record_sha256=identity.get(
                        "expected_publication_record_sha256", ""
                    ),
                )
            )
        already_installed = _validate_existing_target(
            target=target,
            source_payload=source_payload,
            snapshot=snapshot,
            identity=identity,
        )
        return _receipt(
            status=(
                "artifact_preflight_already_installed"
                if artifact_preflight_only and already_installed
                else "artifact_preflight_validated"
                if artifact_preflight_only
                else "already_installed"
                if already_installed
                else "validated"
            ),
            applied=False,
            already_installed=already_installed,
            slug=slug,
            snapshot=snapshot,
            identity=identity,
        )

    with _hosted_property_tour_publication_lock(
        public_dir=public_tour_dir,
        slug=slug,
    ):
        (
            refreshed_request,
            refreshed_controller_identity,
            refreshed_incoming_root,
            refreshed_public_tour_dir,
        ) = _controller_publication_admission(
            request,
            publication_admission,
            require_consumed=True,
        )
        if (
            refreshed_request != dict(request)
            or refreshed_incoming_root != admitted_incoming_root
            or refreshed_public_tour_dir != public_tour_dir
            or _directory_identity(
                public_tour_dir,
                code="ai_panorama_configured_public_dir_invalid",
            )
            != (
                int(refreshed_controller_identity["public_tour_root_device"]),
                int(refreshed_controller_identity["public_tour_root_inode"]),
            )
        ):
            _fail("ai_panorama_controller_admission_context_changed")
        identity.update(refreshed_controller_identity)
        if _validate_existing_target(
            target=target,
            source_payload=source_payload,
            snapshot=snapshot,
            identity=identity,
        ):
            try:
                with property_account_publication_authority(
                    identity["principal_id"], run_id=identity["search_run_id"]
                ) as connection:
                    identity.update(
                        _validate_v2_publication_authority(
                            request=request,
                            identity=identity,
                            connection=connection,
                            for_update=True,
                            expected_record_sha256=identity.get(
                                "expected_publication_record_sha256", ""
                            ),
                        )
                    )
                    identity.update(
                        _bind_candidate_in_publication_transaction(
                            connection=connection,
                            bundle_dir=target,
                            request=request,
                            identity=identity,
                            publication_admission=publication_admission,
                        )
                    )
            except Exception as exc:
                outcome = (
                    _classify_publication_commit_outcome(
                        bundle_dir=target,
                        request=request,
                        identity=identity,
                        publication_admission=publication_admission,
                    )
                    if identity.get("publication_binding_after_sha256")
                    else "uncommitted"
                )
                if outcome != "committed":
                    _raise_publication_transaction_failure(
                        exc,
                        rollback_performed=False,
                        publication_outcome=outcome,
                    )
            return _receipt(
                status="already_installed",
                applied=True,
                already_installed=True,
                slug=slug,
                snapshot=snapshot,
                identity=identity,
            )

        stage: Path | None = None
        published_new_target = False
        transaction_body_complete = False
        transaction_committed = False
        try:
            with property_account_publication_authority(
                identity["principal_id"], run_id=identity["search_run_id"]
            ) as connection:
                identity.update(
                    _validate_v2_publication_authority(
                        request=request,
                        identity=identity,
                        connection=connection,
                        for_update=True,
                        expected_record_sha256=identity.get(
                            "expected_publication_record_sha256", ""
                        ),
                    )
                )
                stage = public_tour_dir / f".{slug}.ai-intake-{uuid4().hex}"
                os.mkdir(stage, 0o755)
                _copy_snapshot(source_bundle, stage, snapshot)
                staged_payload = _load_source_manifest(stage)
                staged_contract = _hosted_property_tour_ai_panorama_contract(
                    bundle_dir=stage,
                    payload=staged_payload,
                    mode="full",
                )
                if (
                    staged_contract.get("ready") is not True
                    or str(staged_contract.get("core_manifest_sha256") or "")
                    != identity["core_manifest_sha256"]
                ):
                    _fail("ai_panorama_stage_contract_invalid")
                owned_payload = dict(staged_payload)
                owned_payload.update(
                    {
                        "principal_id": identity["principal_id"],
                        "search_run_id": identity["search_run_id"],
                        "candidate_ref": identity["candidate_ref"],
                        "listing_url": identity["listing_url"],
                        "property_url": identity["listing_url"],
                        "source_ref": identity["source_ref"],
                        "external_id": identity["external_id"],
                        "panorama_source": "ai_panorama_360_sealed_bundle",
                    }
                )
                # The sealed manifest has already passed the complete AI-tour
                # contract and contains no private identity. Preserve it byte-
                # semantically here: the generic browser projection intentionally
                # strips server-side acceptance proofs and therefore cannot be
                # used as the hosted on-disk acceptance manifest.
                public_payload = dict(staged_payload)
                if _PRIVATE_MANIFEST_KEYS.intersection(public_payload):
                    _fail("ai_panorama_public_manifest_private_value_leak")
                private_payload = _public_tour_private_receipt(owned_payload)
                expected_private = {
                    "principal_id": identity["principal_id"],
                    "search_run_id": identity["search_run_id"],
                    "candidate_ref": identity["candidate_ref"],
                    "listing_url": identity["listing_url"],
                    "property_url": identity["listing_url"],
                    "source_ref": identity["source_ref"],
                    "external_id": identity["external_id"],
                }
                if any(
                    str(private_payload.get(key) or "").strip() != value
                    for key, value in expected_private.items()
                ):
                    _fail("ai_panorama_private_receipt_binding_failed")
                _write_hosted_property_tour_manifests_atomic(
                    stage,
                    public_payload=public_payload,
                    private_payload=private_payload,
                )
                written_payload = _load_json_manifest(
                    stage / "tour.json", code="ai_panorama_stage_manifest_invalid"
                )
                written_contract = _hosted_property_tour_ai_panorama_contract(
                    bundle_dir=stage,
                    payload=written_payload,
                    mode="full",
                )
                if (
                    written_contract.get("ready") is not True
                    or str(written_contract.get("core_manifest_sha256") or "")
                    != identity["core_manifest_sha256"]
                    or _semantic_manifest_sha256(written_payload)
                    != _semantic_manifest_sha256(source_payload)
                ):
                    _fail("ai_panorama_written_manifest_contract_invalid")
                _apply_runtime_ownership(
                    stage,
                    uid=public_runtime_uid,
                    gid=public_runtime_gid,
                    expected_device=int(
                        controller_identity["public_tour_root_device"]
                    ),
                )
                identity.update(
                    _bind_candidate_in_publication_transaction(
                        connection=connection,
                        bundle_dir=stage,
                        request=request,
                        identity=identity,
                        publication_admission=publication_admission,
                    )
                )
                _fsync_directory_tree(stage)
                if target.exists() or target.is_symlink():
                    _fail("ai_panorama_target_replace_forbidden")
                os.rename(stage, target)
                stage = None
                published_new_target = True
                directory_fd = os.open(
                    public_tour_dir,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                transaction_body_complete = True
            transaction_committed = True
        except Exception as exc:
            rollback_performed = False
            publication_outcome = "uncommitted"
            if transaction_body_complete and not transaction_committed:
                publication_outcome = _classify_publication_commit_outcome(
                    bundle_dir=target,
                    request=request,
                    identity=identity,
                    publication_admission=publication_admission,
                )
            if publication_outcome == "committed":
                transaction_committed = True
            elif (
                published_new_target
                and not transaction_committed
                and publication_outcome == "uncommitted"
            ):
                _remove_newly_installed_target_after_transaction_failure(
                    public_tour_dir=public_tour_dir,
                    target=target,
                    source_payload=source_payload,
                    snapshot=snapshot,
                    identity=identity,
                )
                rollback_performed = True
            if publication_outcome != "committed":
                _raise_publication_transaction_failure(
                    exc,
                    rollback_performed=rollback_performed,
                    publication_outcome=publication_outcome,
                )
        finally:
            if stage is not None and stage.parent == public_tour_dir and stage.name.startswith(
                f".{slug}.ai-intake-"
            ):
                shutil.rmtree(stage, ignore_errors=True)

    return _receipt(
        status="installed",
        applied=True,
        already_installed=False,
        slug=slug,
        snapshot=snapshot,
        identity=identity,
    )


__all__ = [
    "AI_PANORAMA_CANDIDATE_MARKER_CONTRACT",
    "AI_PANORAMA_CANDIDATE_MARKER_RELPATH",
    "AI_PANORAMA_INSTALL_RECEIPT_CONTRACT",
    "AI_PANORAMA_INSTALL_REQUEST_CONTRACT",
    "AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1",
    "AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2",
    "AI_PANORAMA_INSTALL_SOURCE_IDENTITY_CONTRACT",
    "AI_PANORAMA_INSTALL_SOURCE_RELATIVE_PATH_SEMANTICS",
    "AI_PANORAMA_INSTALL_SOURCE_RELATIVE_ROOT",
    "AI_PANORAMA_INSTALL_SOURCE_TREE_ALGORITHM",
    "AI_PANORAMA_MATERIALIZATION_RECEIPT_CONTRACT",
    "AiPanoramaInstallerSourceIdentity",
    "AiPanoramaIntakeError",
    "install_sealed_ai_panorama_bundle",
    "load_private_ai_panorama_install_request",
    "snapshot_ai_panorama_installer_source_bundle",
]
