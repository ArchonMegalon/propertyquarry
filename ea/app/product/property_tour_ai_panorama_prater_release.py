from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from app.product.property_search_storage import (
    load_property_search_run_record_for_publication,
    load_unique_property_search_run_record_for_discovery,
    property_account_publication_recovery_observation,
)
from app.product.property_search_tour_binding import (
    PropertySearchTourBindingError,
    authorize_property_search_candidate_tour_install,
    exact_property_search_candidate_tour_binding_receipt,
    property_search_run_record_sha256,
    property_search_source_url_sha256,
)
from app.product.property_tour_ai_panorama_admission import (
    AiPanoramaInstallExpectedBindings,
)
from app.product.property_tour_ai_panorama_intake import (
    AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
    AiPanoramaIntakeError,
    _revalidate_ai_panorama_install_admission,
    inspect_ai_panorama_historical_publication_target,
    install_sealed_ai_panorama_bundle,
    prepare_ai_panorama_publication_binding,
)
from app.product.property_tour_ai_panorama_operation_journal import (
    AiPanoramaHistoricalOperationObservation,
    _finish_historical_ai_panorama_install_operation,
    _record_consumed_without_operation_failed_clean,
    begin_ai_panorama_install_operation,
    finish_ai_panorama_install_operation,
    load_historical_ai_panorama_install_operation,
)
from app.product.property_tour_governed_reservations import (
    GOVERNED_PRATER_CANDIDATE_MARKER_SHA256 as PRATER_CANDIDATE_MARKER_SHA256,
    GOVERNED_PRATER_CANDIDATE_REF as PRATER_CANDIDATE_REF,
    GOVERNED_PRATER_CONTROL_URL as PRATER_PUBLIC_CONTROL_URL,
    GOVERNED_PRATER_CORE_MANIFEST_SHA256 as PRATER_CORE_MANIFEST_SHA256,
    GOVERNED_PRATER_EXTERNAL_ID as PRATER_EXTERNAL_ID,
    GOVERNED_PRATER_LISTING_URL as PRATER_LISTING_URL,
    GOVERNED_PRATER_MATERIALIZATION_RECEIPT_SHA256 as PRATER_MATERIALIZATION_RECEIPT_SHA256,
    GOVERNED_PRATER_PROVIDER_KEY as PRATER_PROVIDER_KEY,
    GOVERNED_PRATER_REVOCATION_FILENAME,
    GOVERNED_PRATER_REVOCATION_MAX_BYTES,
    GOVERNED_PRATER_REVOCATION_MODE,
    GOVERNED_PRATER_REVOCATION_REQUIRED_GID,
    GOVERNED_PRATER_REVOCATION_REQUIRED_UID,
    GOVERNED_PRATER_SEARCH_RUN_ID as PRATER_SEARCH_RUN_ID,
    GOVERNED_PRATER_SLUG as PRATER_SLUG,
    GOVERNED_PRATER_SOURCE_REF as PRATER_SOURCE_REF,
    GOVERNED_PRATER_SOURCE_TREE_SHA256 as PRATER_SOURCE_TREE_SHA256,
    GOVERNED_PRATER_TOUR_SHA256 as PRATER_TOUR_SHA256,
    GOVERNED_PUBLIC_TOUR_MOUNT_TARGET as PRATER_PUBLIC_MOUNT_TARGET,
    GOVERNED_PUBLIC_TOUR_VOLUME_NAME as PRATER_PUBLIC_VOLUME_NAME,
    validate_governed_prater_revocation_bytes,
)


PRATER_AI_PANORAMA_RELEASE_CONTRACT = (
    "propertyquarry.prater_ai_panorama_governed_release.v1"
)
PRATER_PUBLICATION_RECORD_DISCOVERY_CONTRACT = (
    "propertyquarry.prater_ai_panorama_publication_record_discovery.v1"
)
PRATER_RECOVERY_CLASSIFICATION_SCHEMA = (
    "propertyquarry.prater-ai-panorama-recovery-evidence.v1"
)
PRATER_RECOVERY_CLASSIFICATION_AUTHORITY = (
    "propertyquarry-release-control"
)
PRATER_PROPERTY_URL_SHA256 = (
    "f451d904167c5b1a2b27f698ec38c18f6760fe55b79cca32c99bc986f8293d8e"
)
PRATER_CONTROLLER_ARTIFACT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/"
    "ai-panorama-artifacts/prater-v1"
)
PRATER_ARTIFACT_RELPATH = (
    "bundle/prater-messe-maisonette-ai-360-053ad185e1c44b2e"
)
PRATER_MATERIALIZATION_RECEIPT_RELPATH = "materialization.receipt.json"
PRATER_MATERIALIZATION_ORIGINAL_CANDIDATE_ROOT = (
    "/docker/property/state/incoming_property_tours/"
    "prater-053ad185e1c44b2e/ai-panorama-v2-yaw65-final"
)
_TARGET_MAX_FILES = 512
_TARGET_MAX_DIRECTORIES = 256
_TARGET_MAX_BYTES = 128 * 1024 * 1024
_TARGET_MAX_RESERVED_ENTRIES = 64
_RESERVED_OPERATION_NAME_RE = re.compile(
    rf"\.{re.escape(PRATER_SLUG)}\."
    r"ai-(?:intake|rollback)-[0-9a-f]{32}\Z"
)
_OWNER_PRINCIPAL_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}\Z"
)


def _fail(code: str) -> None:
    raise AiPanoramaIntakeError(code)


def _exact(actual: object, expected: str) -> bool:
    return hmac.compare_digest(str(actual or "").strip(), expected)


def _validated_prater_admission(
    admission: object,
    *,
    require_consumed: bool,
) -> object:
    verified = _revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=require_consumed,
    )
    exact_bindings = (
        ("search_run_id", PRATER_SEARCH_RUN_ID),
        ("candidate_ref", PRATER_CANDIDATE_REF),
        ("external_id", PRATER_EXTERNAL_ID),
        ("source_ref", PRATER_SOURCE_REF),
        ("provider_key", PRATER_PROVIDER_KEY),
        ("expected_slug", PRATER_SLUG),
        ("public_control_url", PRATER_PUBLIC_CONTROL_URL),
        ("expected_source_tree_sha256", PRATER_SOURCE_TREE_SHA256),
        ("expected_tour_sha256", PRATER_TOUR_SHA256),
        ("expected_core_manifest_sha256", PRATER_CORE_MANIFEST_SHA256),
        (
            "expected_materialization_receipt_sha256",
            PRATER_MATERIALIZATION_RECEIPT_SHA256,
        ),
        (
            "expected_candidate_marker_sha256",
            PRATER_CANDIDATE_MARKER_SHA256,
        ),
        ("artifact_relpath", PRATER_ARTIFACT_RELPATH),
        (
            "materialization_receipt_relpath",
            PRATER_MATERIALIZATION_RECEIPT_RELPATH,
        ),
    )
    if any(
        not _exact(getattr(verified, field, None), expected)
        for field, expected in exact_bindings
    ):
        _fail("ai_panorama_prater_permit_binding_mismatch")
    listing_url = str(getattr(verified, "listing_url", "") or "").strip()
    if (
        not hmac.compare_digest(listing_url, PRATER_LISTING_URL)
        or not hmac.compare_digest(
            property_search_source_url_sha256(listing_url),
            PRATER_PROPERTY_URL_SHA256,
        )
    ):
        _fail("ai_panorama_prater_listing_binding_mismatch")
    if Path(getattr(verified, "incoming_root", "")) != PRATER_CONTROLLER_ARTIFACT_ROOT:
        _fail("ai_panorama_prater_artifact_root_mismatch")
    public_tour_dir = Path(getattr(verified, "public_tour_dir", ""))
    if (
        not _exact(
            getattr(verified, "public_tour_volume_name", None),
            PRATER_PUBLIC_VOLUME_NAME,
        )
        or not _exact(
            getattr(verified, "public_tour_mount_target", None),
            PRATER_PUBLIC_MOUNT_TARGET,
        )
        or not public_tour_dir.is_absolute()
        or type(getattr(verified, "public_tour_root_device", None)) is not int
        or type(getattr(verified, "public_tour_root_inode", None)) is not int
        or getattr(verified, "public_tour_root_device") < 1
        or getattr(verified, "public_tour_root_inode") < 1
    ):
        _fail("ai_panorama_prater_public_volume_profile_mismatch")
    if Path(getattr(verified, "source_bundle", "")) != (
        PRATER_CONTROLLER_ARTIFACT_ROOT / PRATER_ARTIFACT_RELPATH
    ):
        _fail("ai_panorama_prater_artifact_path_mismatch")
    if Path(getattr(verified, "materialization_receipt_path", "")) != (
        PRATER_CONTROLLER_ARTIFACT_ROOT
        / PRATER_MATERIALIZATION_RECEIPT_RELPATH
    ):
        _fail("ai_panorama_prater_receipt_path_mismatch")
    return verified


def _prater_install_request(verified: object) -> dict[str, object]:
    """Project only controller-admitted values into the private V2 request."""

    return {
        "contract": AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
        "source_bundle": str(getattr(verified, "source_bundle")),
        "materialization_receipt_path": str(
            getattr(verified, "materialization_receipt_path")
        ),
        "public_tour_dir": str(getattr(verified, "public_tour_dir")),
        "principal_id": str(getattr(verified, "authenticated_principal_id")),
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "external_id": PRATER_EXTERNAL_ID,
        "listing_url": str(getattr(verified, "listing_url")),
        "source_ref": PRATER_SOURCE_REF,
        "provider_key": PRATER_PROVIDER_KEY,
        "expected_slug": PRATER_SLUG,
        "public_control_url": PRATER_PUBLIC_CONTROL_URL,
        "expected_source_tree_sha256": PRATER_SOURCE_TREE_SHA256,
        "expected_tour_sha256": PRATER_TOUR_SHA256,
        "expected_core_manifest_sha256": PRATER_CORE_MANIFEST_SHA256,
        "expected_materialization_receipt_sha256": (
            PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "expected_candidate_marker_sha256": PRATER_CANDIDATE_MARKER_SHA256,
        "expected_publication_record_sha256": str(
            getattr(verified, "expected_publication_record_sha256")
        ),
    }


def _historical_prater_install_request(
    expected: AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    """Project the full signed historical binding into read-only observation."""

    if type(expected) is not AiPanoramaInstallExpectedBindings:
        _fail("ai_panorama_prater_historical_binding_invalid")
    fixed = {
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "external_id": PRATER_EXTERNAL_ID,
        "listing_url": PRATER_LISTING_URL,
        "source_ref": PRATER_SOURCE_REF,
        "provider_key": PRATER_PROVIDER_KEY,
        "expected_slug": PRATER_SLUG,
        "expected_source_tree_sha256": PRATER_SOURCE_TREE_SHA256,
        "expected_tour_sha256": PRATER_TOUR_SHA256,
        "expected_core_manifest_sha256": PRATER_CORE_MANIFEST_SHA256,
        "expected_materialization_receipt_sha256": (
            PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "expected_candidate_marker_sha256": (
            PRATER_CANDIDATE_MARKER_SHA256
        ),
        "artifact_relpath": PRATER_ARTIFACT_RELPATH,
        "materialization_receipt_relpath": (
            PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
    }
    if any(
        not hmac.compare_digest(
            str(getattr(expected, field)),
            value,
        )
        for field, value in fixed.items()
    ):
        _fail("ai_panorama_prater_historical_binding_invalid")
    if (
        expected.public_tour_root_device < 1
        or expected.public_tour_root_inode < 1
    ):
        _fail("ai_panorama_prater_historical_binding_invalid")
    return {
        "contract": AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
        "source_bundle": str(
            PRATER_CONTROLLER_ARTIFACT_ROOT / PRATER_ARTIFACT_RELPATH
        ),
        "materialization_receipt_path": str(
            PRATER_CONTROLLER_ARTIFACT_ROOT
            / PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
        "public_tour_dir": PRATER_PUBLIC_MOUNT_TARGET,
        "public_tour_root_device": expected.public_tour_root_device,
        "public_tour_root_inode": expected.public_tour_root_inode,
        "principal_id": expected.owner_principal_id,
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "external_id": PRATER_EXTERNAL_ID,
        "listing_url": PRATER_LISTING_URL,
        "source_ref": PRATER_SOURCE_REF,
        "provider_key": PRATER_PROVIDER_KEY,
        "expected_slug": PRATER_SLUG,
        "public_control_url": PRATER_PUBLIC_CONTROL_URL,
        "expected_source_tree_sha256": PRATER_SOURCE_TREE_SHA256,
        "expected_tour_sha256": PRATER_TOUR_SHA256,
        "expected_core_manifest_sha256": PRATER_CORE_MANIFEST_SHA256,
        "expected_materialization_receipt_sha256": (
            PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "expected_candidate_marker_sha256": (
            PRATER_CANDIDATE_MARKER_SHA256
        ),
        "expected_publication_record_sha256": (
            expected.expected_publication_record_sha256
        ),
    }


def _binding_receipt_projection(
    receipt: Mapping[str, object],
) -> dict[str, object]:
    allowed = (
        "status",
        "mode",
        "changed",
        "before_sha256",
        "after_sha256",
        "persisted_sha256",
    )
    return {key: receipt[key] for key in allowed if key in receipt}


def discover_prater_ai_panorama_publication_record() -> dict[str, object]:
    """Read and validate the exact owner-scoped record before permit signing."""

    record = load_unique_property_search_run_record_for_discovery(
        run_id=PRATER_SEARCH_RUN_ID,
    )
    if not isinstance(record, Mapping):
        _fail("ai_panorama_prater_discovery_record_not_unique")
    principal_id = str(record.get("principal_id") or "").strip()
    if (
        type(record.get("principal_id")) is not str
        or principal_id != record["principal_id"]
        or _OWNER_PRINCIPAL_ID_RE.fullmatch(principal_id) is None
    ):
        _fail("ai_panorama_prater_discovery_owner_invalid")
    bundle_identity = {
        "owner_verified": True,
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "listing_url": PRATER_LISTING_URL,
        "property_url": PRATER_LISTING_URL,
        "property_url_sha256": PRATER_PROPERTY_URL_SHA256,
        "provider_key": PRATER_PROVIDER_KEY,
        "source_ref": PRATER_SOURCE_REF,
        "external_id": PRATER_EXTERNAL_ID,
    }
    try:
        authority = authorize_property_search_candidate_tour_install(
            record,
            principal_id=principal_id,
            run_id=PRATER_SEARCH_RUN_ID,
            candidate_ref=PRATER_CANDIDATE_REF,
            expected_listing_id=PRATER_EXTERNAL_ID,
            expected_source_ref=PRATER_SOURCE_REF,
            bundle_identity=bundle_identity,
        )
    except PropertySearchTourBindingError as exc:
        raise AiPanoramaIntakeError(
            f"ai_panorama_prater_discovery:{exc.code}"
        ) from exc
    observed_sha256 = property_search_run_record_sha256(record)
    authority_sha256 = str(authority.get("record_sha256") or "").strip().lower()
    if (
        len(authority_sha256) != 64
        or not hmac.compare_digest(authority_sha256, observed_sha256)
    ):
        _fail("ai_panorama_prater_discovery_record_invalid")
    return {
        "contract": PRATER_PUBLICATION_RECORD_DISCOVERY_CONTRACT,
        "status": "record-discovered",
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "owner_principal_id": principal_id,
        "expected_publication_record_sha256": observed_sha256,
        "database_mutation_performed": False,
        "release_authorized": False,
        "private_values_redacted": False,
    }


def _stable_target_file_sha256(
    path: Path,
    *,
    expected_device: int,
    maximum_bytes: int,
) -> tuple[str, os.stat_result]:
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_dev) != expected_device
            or int(before.st_size) < 0
            or int(before.st_size) > maximum_bytes
        ):
            _fail("ai_panorama_prater_target_manifest_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        identity = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_mode),
            int(opened.st_uid),
            int(opened.st_gid),
            int(opened.st_nlink),
            int(opened.st_size),
            int(opened.st_mtime_ns),
            int(opened.st_ctime_ns),
        )
        if (
            identity
            != (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_mode),
                int(before.st_uid),
                int(before.st_gid),
                int(before.st_nlink),
                int(before.st_size),
                int(before.st_mtime_ns),
                int(before.st_ctime_ns),
            )
            or opened.st_nlink != 1
        ):
            _fail("ai_panorama_prater_target_manifest_changed")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                _fail("ai_panorama_prater_target_manifest_budget_exceeded")
            digest.update(chunk)
        closed = os.fstat(descriptor)
        after = path.stat(follow_symlinks=False)
        for observed in (closed, after):
            if (
                int(observed.st_dev),
                int(observed.st_ino),
                int(observed.st_mode),
                int(observed.st_uid),
                int(observed.st_gid),
                int(observed.st_nlink),
                int(observed.st_size),
                int(observed.st_mtime_ns),
                int(observed.st_ctime_ns),
            ) != identity:
                _fail("ai_panorama_prater_target_manifest_changed")
        return digest.hexdigest(), after
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_target_manifest_invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reserved_operation_entries(
    public_root: Path,
    *,
    expected_device: int,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    try:
        with os.scandir(public_root) as entries:
            for entry in entries:
                if entry.name == PRATER_SLUG:
                    continue
                details = entry.stat(follow_symlinks=False)
                if entry.name == GOVERNED_PRATER_REVOCATION_FILENAME:
                    descriptor = -1
                    try:
                        if (
                            entry.is_symlink()
                            or int(details.st_dev) != expected_device
                            or not stat.S_ISREG(details.st_mode)
                            or int(details.st_uid)
                            != GOVERNED_PRATER_REVOCATION_REQUIRED_UID
                            or int(details.st_gid)
                            != GOVERNED_PRATER_REVOCATION_REQUIRED_GID
                            or stat.S_IMODE(details.st_mode)
                            != GOVERNED_PRATER_REVOCATION_MODE
                            or int(details.st_nlink) != 1
                            or int(details.st_size) <= 0
                            or int(details.st_size)
                            > GOVERNED_PRATER_REVOCATION_MAX_BYTES
                        ):
                            _fail(
                                "ai_panorama_prater_revocation_entry_invalid"
                            )
                        descriptor = os.open(
                            entry.path,
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_NONBLOCK", 0),
                        )
                        opened = os.fstat(descriptor)
                        identity = (
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
                        if (
                            int(opened.st_dev),
                            int(opened.st_ino),
                            int(opened.st_mode),
                            int(opened.st_uid),
                            int(opened.st_gid),
                            int(opened.st_nlink),
                            int(opened.st_size),
                            int(opened.st_mtime_ns),
                            int(opened.st_ctime_ns),
                        ) != identity:
                            _fail(
                                "ai_panorama_prater_revocation_entry_changed"
                            )
                        remaining = int(opened.st_size)
                        chunks: list[bytes] = []
                        while remaining:
                            chunk = os.read(descriptor, min(remaining, 4096))
                            if not chunk:
                                _fail(
                                    "ai_panorama_prater_revocation_entry_changed"
                                )
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        if os.read(descriptor, 1):
                            _fail(
                                "ai_panorama_prater_revocation_entry_changed"
                            )
                        closed = os.fstat(descriptor)
                        after = entry.stat(follow_symlinks=False)
                        for observed in (closed, after):
                            if (
                                int(observed.st_dev),
                                int(observed.st_ino),
                                int(observed.st_mode),
                                int(observed.st_uid),
                                int(observed.st_gid),
                                int(observed.st_nlink),
                                int(observed.st_size),
                                int(observed.st_mtime_ns),
                                int(observed.st_ctime_ns),
                            ) != identity:
                                _fail(
                                    "ai_panorama_prater_revocation_entry_changed"
                                )
                        raw = b"".join(chunks)
                        validate_governed_prater_revocation_bytes(raw)
                    except AiPanoramaIntakeError:
                        raise
                    except (OSError, ValueError) as exc:
                        raise AiPanoramaIntakeError(
                            "ai_panorama_prater_revocation_entry_invalid"
                        ) from exc
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    rows.append(
                        {
                            "name": entry.name,
                            "kind": "revocation",
                            "device": int(details.st_dev),
                            "inode": int(details.st_ino),
                            "mode": stat.S_IMODE(details.st_mode),
                            "uid": int(details.st_uid),
                            "gid": int(details.st_gid),
                            "nlink": int(details.st_nlink),
                            "size_bytes": int(details.st_size),
                            "content_sha256": hashlib.sha256(raw).hexdigest(),
                            "mtime_ns": int(details.st_mtime_ns),
                            "ctime_ns": int(details.st_ctime_ns),
                        }
                    )
                elif _RESERVED_OPERATION_NAME_RE.fullmatch(entry.name):
                    if (
                        entry.is_symlink()
                        or int(details.st_dev) != expected_device
                        or not stat.S_ISDIR(details.st_mode)
                    ):
                        _fail("ai_panorama_prater_reserved_entry_invalid")
                    rows.append(
                        {
                            "name": entry.name,
                            "kind": "directory",
                            "device": int(details.st_dev),
                            "inode": int(details.st_ino),
                            "mode": stat.S_IMODE(details.st_mode),
                            "uid": int(details.st_uid),
                            "gid": int(details.st_gid),
                            "size_bytes": int(details.st_size),
                            "mtime_ns": int(details.st_mtime_ns),
                            "ctime_ns": int(details.st_ctime_ns),
                        }
                    )
                else:
                    _fail("ai_panorama_prater_public_root_entry_forbidden")
                if len(rows) > _TARGET_MAX_RESERVED_ENTRIES:
                    _fail("ai_panorama_prater_target_manifest_budget_exceeded")
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_reserved_entry_invalid"
        ) from exc
    rows.sort(key=lambda row: str(row["name"]))
    return {
        "reserved_entry_count": len(rows),
        "reserved_entries_sha256": hashlib.sha256(
            json.dumps(
                rows,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _target_manifest(verified: object) -> dict[str, object]:
    """Hash the exact public target without serializing owner data."""

    public_root = Path(getattr(verified, "public_tour_dir", ""))
    expected_device = int(getattr(verified, "public_tour_root_device"))
    expected_inode = int(getattr(verified, "public_tour_root_inode"))
    try:
        root_before = public_root.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_public_volume_identity_changed"
        ) from exc
    if (
        stat.S_ISLNK(root_before.st_mode)
        or not stat.S_ISDIR(root_before.st_mode)
        or int(root_before.st_dev) != expected_device
        or int(root_before.st_ino) != expected_inode
    ):
        _fail("ai_panorama_prater_public_volume_identity_changed")
    reserved_before = _reserved_operation_entries(
        public_root,
        expected_device=expected_device,
    )

    target = public_root / PRATER_SLUG
    try:
        target_before = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        try:
            root_after = public_root.stat(follow_symlinks=False)
            target_after = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            root_after = public_root.stat(follow_symlinks=False)
            target_after = None
        except OSError as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_target_manifest_invalid"
            ) from exc
        if target_after is not None:
            _fail("ai_panorama_prater_target_manifest_changed")
        if (
            int(root_after.st_dev) != expected_device
            or int(root_after.st_ino) != expected_inode
        ):
            _fail("ai_panorama_prater_public_volume_identity_changed")
        reserved_after = _reserved_operation_entries(
            public_root,
            expected_device=expected_device,
        )
        if reserved_after != reserved_before:
            _fail("ai_panorama_prater_target_manifest_changed")
        return {
            "state": "absent",
            "target_relpath": PRATER_SLUG,
            "public_root_device": expected_device,
            "public_root_inode": expected_inode,
            **reserved_after,
        }
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_target_manifest_invalid"
        ) from exc
    if (
        stat.S_ISLNK(target_before.st_mode)
        or not stat.S_ISDIR(target_before.st_mode)
        or int(target_before.st_dev) != expected_device
    ):
        _fail("ai_panorama_prater_target_manifest_invalid")
    target_identity = (
        int(target_before.st_dev),
        int(target_before.st_ino),
        int(target_before.st_mode),
        int(target_before.st_uid),
        int(target_before.st_gid),
        int(target_before.st_mtime_ns),
        int(target_before.st_ctime_ns),
    )
    rows: list[dict[str, object]] = [
        {
            "kind": "directory",
            "relpath": ".",
            "mode": stat.S_IMODE(target_before.st_mode),
            "uid": int(target_before.st_uid),
            "gid": int(target_before.st_gid),
        }
    ]
    file_count = 0
    directory_count = 1
    total_bytes = 0
    private_receipt_sha256 = ""
    try:
        for current_raw, directory_names, file_names in os.walk(
            target,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_raw)
            current_details = current.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(current_details.st_mode)
                or not stat.S_ISDIR(current_details.st_mode)
                or int(current_details.st_dev) != expected_device
            ):
                _fail("ai_panorama_prater_target_manifest_invalid")
            directory_names.sort()
            file_names.sort()
            for name in directory_names:
                path = current / name
                details = path.stat(follow_symlinks=False)
                relpath = path.relative_to(target).as_posix()
                if (
                    not relpath
                    or relpath.startswith("/")
                    or any(part in {"", ".", ".."} for part in Path(relpath).parts)
                    or stat.S_ISLNK(details.st_mode)
                    or not stat.S_ISDIR(details.st_mode)
                    or int(details.st_dev) != expected_device
                ):
                    _fail("ai_panorama_prater_target_manifest_invalid")
                rows.append(
                    {
                        "kind": "directory",
                        "relpath": relpath,
                        "mode": stat.S_IMODE(details.st_mode),
                        "uid": int(details.st_uid),
                        "gid": int(details.st_gid),
                    }
                )
                directory_count += 1
                if directory_count > _TARGET_MAX_DIRECTORIES:
                    _fail("ai_panorama_prater_target_manifest_budget_exceeded")
            for name in file_names:
                path = current / name
                relpath = path.relative_to(target).as_posix()
                if (
                    not relpath
                    or relpath.startswith("/")
                    or any(part in {"", ".", ".."} for part in Path(relpath).parts)
                ):
                    _fail("ai_panorama_prater_target_manifest_invalid")
                remaining = _TARGET_MAX_BYTES - total_bytes
                digest, details = _stable_target_file_sha256(
                    path,
                    expected_device=expected_device,
                    maximum_bytes=remaining,
                )
                file_count += 1
                total_bytes += int(details.st_size)
                if (
                    file_count > _TARGET_MAX_FILES
                    or total_bytes > _TARGET_MAX_BYTES
                ):
                    _fail("ai_panorama_prater_target_manifest_budget_exceeded")
                rows.append(
                    {
                        "kind": "file",
                        "relpath": relpath,
                        "mode": stat.S_IMODE(details.st_mode),
                        "uid": int(details.st_uid),
                        "gid": int(details.st_gid),
                        "size_bytes": int(details.st_size),
                        "sha256": digest,
                    }
                )
                if relpath == "tour.private.json":
                    private_receipt_sha256 = digest
    except AiPanoramaIntakeError:
        raise
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_target_manifest_invalid"
        ) from exc
    try:
        target_after = target.stat(follow_symlinks=False)
        root_after = public_root.stat(follow_symlinks=False)
    except OSError as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_target_manifest_changed"
        ) from exc
    if (
        (
            int(target_after.st_dev),
            int(target_after.st_ino),
            int(target_after.st_mode),
            int(target_after.st_uid),
            int(target_after.st_gid),
            int(target_after.st_mtime_ns),
            int(target_after.st_ctime_ns),
        )
        != target_identity
        or int(root_after.st_dev) != expected_device
        or int(root_after.st_ino) != expected_inode
    ):
        _fail("ai_panorama_prater_target_manifest_changed")
    reserved_after = _reserved_operation_entries(
        public_root,
        expected_device=expected_device,
    )
    if reserved_after != reserved_before:
        _fail("ai_panorama_prater_target_manifest_changed")
    rows.sort(key=lambda row: (str(row["relpath"]), str(row["kind"])))
    encoded = json.dumps(
        rows,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "state": "present",
        "target_relpath": PRATER_SLUG,
        "public_root_device": expected_device,
        "public_root_inode": expected_inode,
        "target_device": target_identity[0],
        "target_inode": target_identity[1],
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "tour_private_sha256": private_receipt_sha256,
        "file_count": file_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
        **reserved_after,
    }


def _operation_evidence(
    verified: object,
    *,
    phase: str,
    target_manifest: Mapping[str, object],
    install_receipt: Mapping[str, object] | None = None,
    error_code: str = "",
    publication_outcome: str = "",
    binding_preparation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "contract": PRATER_AI_PANORAMA_RELEASE_CONTRACT,
        "phase": phase,
        "slug": PRATER_SLUG,
        "listing_url_sha256": PRATER_PROPERTY_URL_SHA256,
        "source_tree_sha256": PRATER_SOURCE_TREE_SHA256,
        "tour_sha256": PRATER_TOUR_SHA256,
        "core_manifest_sha256": PRATER_CORE_MANIFEST_SHA256,
        "materialization_receipt_sha256": (
            PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "candidate_marker_sha256": PRATER_CANDIDATE_MARKER_SHA256,
        "publication_record_sha256": str(
            getattr(verified, "expected_publication_record_sha256")
        ),
        "volume_profile_sha256": str(
            getattr(verified, "volume_profile_sha256", "")
        ),
        "public_tour_volume_name": PRATER_PUBLIC_VOLUME_NAME,
        "public_tour_mount_target": PRATER_PUBLIC_MOUNT_TARGET,
        "target_manifest": dict(target_manifest),
        "private_values_redacted": True,
    }
    if install_receipt is not None:
        evidence["install"] = {
            "status": str(install_receipt.get("status") or ""),
            "already_installed": install_receipt.get("already_installed") is True,
            "source_tree_sha256": str(
                install_receipt.get("source_tree_sha256") or ""
            ),
            "source_tour_sha256": str(
                install_receipt.get("source_tour_sha256") or ""
            ),
            "publication_binding_status": str(
                install_receipt.get("publication_binding_status") or ""
            ),
            "publication_binding_before_sha256": str(
                install_receipt.get("publication_binding_before_sha256") or ""
            ),
            "publication_binding_after_sha256": str(
                install_receipt.get("publication_binding_after_sha256") or ""
            ),
        }
    if error_code:
        evidence["error_code"] = error_code
    if publication_outcome:
        evidence["publication_outcome"] = publication_outcome
    if binding_preparation is not None:
        evidence["publication_binding_preparation"] = {
            "status": str(binding_preparation.get("status") or ""),
            "publication_binding_expected_before_sha256": str(
                binding_preparation.get(
                    "publication_binding_expected_before_sha256"
                )
                or ""
            ),
            "publication_binding_expected_after_sha256": str(
                binding_preparation.get(
                    "publication_binding_expected_after_sha256"
                )
                or ""
            ),
            "publication_binding_bound_at": str(
                binding_preparation.get("publication_binding_bound_at")
                or ""
            ),
            "database_mutation_performed": (
                binding_preparation.get("database_mutation_performed")
                is True
            ),
            "private_values_redacted": True,
        }
    return evidence


def _historical_prepared_recovery_evidence(
    expected: AiPanoramaInstallExpectedBindings,
    operation: AiPanoramaHistoricalOperationObservation,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = operation.prepared_evidence
    required = {
        "contract",
        "phase",
        "slug",
        "listing_url_sha256",
        "source_tree_sha256",
        "tour_sha256",
        "core_manifest_sha256",
        "materialization_receipt_sha256",
        "candidate_marker_sha256",
        "publication_record_sha256",
        "volume_profile_sha256",
        "public_tour_volume_name",
        "public_tour_mount_target",
        "target_manifest",
        "publication_binding_preparation",
        "admission_recovery_binding",
        "private_values_redacted",
    }
    if (
        type(evidence) is not dict
        or set(evidence) != required
        or evidence.get("contract") != PRATER_AI_PANORAMA_RELEASE_CONTRACT
        or evidence.get("phase") != "prepared"
        or evidence.get("slug") != PRATER_SLUG
        or evidence.get("listing_url_sha256")
        != PRATER_PROPERTY_URL_SHA256
        or evidence.get("source_tree_sha256") != PRATER_SOURCE_TREE_SHA256
        or evidence.get("tour_sha256") != PRATER_TOUR_SHA256
        or evidence.get("core_manifest_sha256")
        != PRATER_CORE_MANIFEST_SHA256
        or evidence.get("materialization_receipt_sha256")
        != PRATER_MATERIALIZATION_RECEIPT_SHA256
        or evidence.get("candidate_marker_sha256")
        != PRATER_CANDIDATE_MARKER_SHA256
        or evidence.get("publication_record_sha256")
        != expected.expected_publication_record_sha256
        or evidence.get("volume_profile_sha256")
        != expected.volume_profile_sha256
        or evidence.get("public_tour_volume_name")
        != PRATER_PUBLIC_VOLUME_NAME
        or evidence.get("public_tour_mount_target")
        != PRATER_PUBLIC_MOUNT_TARGET
        or evidence.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_prater_recovery_evidence_invalid")
    historical = operation.historical_consumption
    if evidence.get("admission_recovery_binding") != {
        "ledger_instance_id": historical.ledger_instance_id,
        "ledger_sequence": historical.ledger_sequence,
        "ledger_entry_sha256": historical.ledger_entry_sha256,
    }:
        _fail("ai_panorama_prater_recovery_evidence_invalid")
    baseline = evidence.get("target_manifest")
    preparation = evidence.get("publication_binding_preparation")
    if (
        type(baseline) is not dict
        or baseline.get("reserved_entry_count") != 0
        or baseline.get("state") not in {"absent", "present"}
        or type(preparation) is not dict
        or set(preparation)
        != {
            "status",
            "publication_binding_expected_before_sha256",
            "publication_binding_expected_after_sha256",
            "publication_binding_bound_at",
            "database_mutation_performed",
            "private_values_redacted",
        }
        or preparation.get("status")
        not in {"change-required", "already-bound"}
        or preparation.get("publication_binding_expected_before_sha256")
        != expected.expected_publication_record_sha256
        or preparation.get("database_mutation_performed") is not False
        or preparation.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_prater_recovery_evidence_invalid")
    before = str(
        preparation["publication_binding_expected_before_sha256"]
    )
    after = str(preparation["publication_binding_expected_after_sha256"])
    changed = not hmac.compare_digest(before, after)
    if (
        len(after) != 64
        or any(character not in "0123456789abcdef" for character in after)
        or (preparation["status"] == "change-required") != changed
        or not str(preparation["publication_binding_bound_at"]).strip()
    ):
        _fail("ai_panorama_prater_recovery_evidence_invalid")
    return dict(baseline), dict(preparation)


def _recovery_terminal_evidence(
    *,
    operation: AiPanoramaHistoricalOperationObservation,
    classification: str,
    event: str,
    classification_basis: str,
    observed_database_record_sha256: str,
    target_manifest: Mapping[str, object],
    target_identity: Mapping[str, object],
    observed_publication_binding_exact: bool,
    expected_before_sha256: str,
    expected_after_sha256: str,
    plan_status: str,
) -> dict[str, object]:
    manifest = dict(target_manifest)
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    historical = operation.historical_consumption
    return {
        "schema": PRATER_RECOVERY_CLASSIFICATION_SCHEMA,
        "version": 1,
        "authority": PRATER_RECOVERY_CLASSIFICATION_AUTHORITY,
        "phase": event,
        "classification": classification,
        "classification_basis": classification_basis,
        "prepared_entry_sha256": (
            operation.prepared_entry_sha256 or "genesis"
        ),
        "prepared_evidence_sha256": (
            operation.prepared_evidence_sha256 or "genesis"
        ),
        "observed_target_manifest": manifest,
        "observed_target_manifest_sha256": manifest_sha256,
        "observed_target_identity": dict(target_identity),
        "observed_database_record_sha256": (
            observed_database_record_sha256
        ),
        "observed_publication_binding_exact": (
            observed_publication_binding_exact
        ),
        "publication_binding_expected_before_sha256": (
            expected_before_sha256
        ),
        "publication_binding_expected_after_sha256": (
            expected_after_sha256
        ),
        "publication_binding_plan_status": plan_status,
        "historical_consumption_binding": {
            "ledger_instance_id": historical.ledger_instance_id,
            "ledger_sequence": historical.ledger_sequence,
            "ledger_entry_sha256": historical.ledger_entry_sha256,
        },
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "private_values_redacted": True,
    }


def _existing_historical_terminal_projection(
    operation: AiPanoramaHistoricalOperationObservation,
) -> dict[str, object]:
    event = operation.terminal_event
    classification = (
        "failed-clean" if event == "consumed-failed-clean" else event
    )
    if classification not in {
        "committed",
        "failed-clean",
        "rolled-back",
        "recovery-required",
    }:
        _fail("ai_panorama_prater_recovery_terminal_invalid")
    evidence = operation.terminal_evidence
    if evidence.get("schema") == PRATER_RECOVERY_CLASSIFICATION_SCHEMA:
        if (
            evidence.get("classification") != classification
            or evidence.get("phase") != event
            or evidence.get("database_mutation_performed") is not False
            or evidence.get("public_target_mutation_performed") is not False
            or evidence.get("private_values_redacted") is not True
        ):
            _fail("ai_panorama_prater_recovery_terminal_invalid")
    elif (
        evidence.get("contract") != PRATER_AI_PANORAMA_RELEASE_CONTRACT
        or evidence.get("phase") != event
        or evidence.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_prater_recovery_terminal_invalid")
    return {
        "classification": classification,
        "event": event,
        "operation_id": operation.operation_id,
        "operation_terminal_entry_sha256": (
            operation.terminal_entry_sha256
        ),
        "operation_terminal_evidence_sha256": (
            operation.terminal_evidence_sha256
        ),
        "permit_sha256": operation.permit_sha256,
        "request_id_sha256": operation.request_id_sha256,
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "private_values_redacted": True,
    }


def recover_prater_ai_panorama_historical_operation(
    permit_relpath: str,
    expected: AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    """Classify and terminalize one historical operation without writes to product state."""

    request = _historical_prater_install_request(expected)
    operation = load_historical_ai_panorama_install_operation(
        permit_relpath,
        expected,
    )
    if operation.state == "terminal":
        return _existing_historical_terminal_projection(operation)

    holder = SimpleNamespace(
        public_tour_dir=Path(str(request["public_tour_dir"])),
        public_tour_root_device=expected.public_tour_root_device,
        public_tour_root_inode=expected.public_tour_root_inode,
    )
    with property_account_publication_recovery_observation(
        expected.owner_principal_id,
        run_id=PRATER_SEARCH_RUN_ID,
    ) as connection:
        record = load_property_search_run_record_for_publication(
            run_id=PRATER_SEARCH_RUN_ID,
            principal_id=expected.owner_principal_id,
            connection=connection,
            for_update=False,
        )
        if not isinstance(record, Mapping):
            _fail("ai_panorama_prater_recovery_record_unavailable")
        observed_database_sha256 = property_search_run_record_sha256(
            record
        )
        target_manifest = _target_manifest(holder)
        target_identity = inspect_ai_panorama_historical_publication_target(
            request,
            historical_consumption=operation.historical_consumption,
        )
        reserved_clean = target_manifest.get("reserved_entry_count") == 0
        try:
            exact_binding = exact_property_search_candidate_tour_binding_receipt(
                record,
                principal_id=expected.owner_principal_id,
                run_id=PRATER_SEARCH_RUN_ID,
                candidate_ref=PRATER_CANDIDATE_REF,
                expected_listing_id=PRATER_EXTERNAL_ID,
                generated_reconstruction_url=(
                    PRATER_PUBLIC_CONTROL_URL
                ),
                reconstruction_kind="ai_panorama_360",
                disclosure="",
            )
        except PropertySearchTourBindingError:
            exact_binding = None

        if operation.state == "consumed-no-operation":
            clean = (
                hmac.compare_digest(
                    observed_database_sha256,
                    expected.expected_publication_record_sha256,
                )
                and reserved_clean
                and target_identity.get("state") in {"absent", "exact"}
            )
            if not clean:
                _fail("ai_panorama_prater_recovery_required")
            event = "consumed-failed-clean"
            classification = "failed-clean"
            evidence = _recovery_terminal_evidence(
                operation=operation,
                classification=classification,
                event=event,
                classification_basis=(
                    "consumed-before-operation-preparation"
                ),
                observed_database_record_sha256=(
                    observed_database_sha256
                ),
                target_manifest=target_manifest,
                target_identity=target_identity,
                observed_publication_binding_exact=(
                    exact_binding is not None
                ),
                expected_before_sha256=(
                    expected.expected_publication_record_sha256
                ),
                expected_after_sha256=(
                    expected.expected_publication_record_sha256
                ),
                plan_status="not-prepared",
            )
            terminal_entry_sha256 = (
                _record_consumed_without_operation_failed_clean(
                    permit_relpath,
                    expected,
                    operation,
                    evidence=evidence,
                )
            )
        else:
            baseline, preparation = _historical_prepared_recovery_evidence(
                expected,
                operation,
            )
            before = str(
                preparation[
                    "publication_binding_expected_before_sha256"
                ]
            )
            after = str(
                preparation[
                    "publication_binding_expected_after_sha256"
                ]
            )
            committed = (
                hmac.compare_digest(observed_database_sha256, after)
                and target_manifest.get("state") == "present"
                and target_identity.get("state") == "exact"
                and reserved_clean
                and exact_binding is not None
            )
            failed_clean = (
                hmac.compare_digest(observed_database_sha256, before)
                and target_manifest == baseline
                and reserved_clean
            )
            if committed:
                classification = "committed"
                basis = "prepared-after-binding-and-exact-target"
            elif failed_clean:
                classification = "failed-clean"
                basis = "prepared-before-binding-and-baseline-target"
            else:
                classification = "recovery-required"
                basis = "prepared-observation-contradiction"
            event = classification
            evidence = _recovery_terminal_evidence(
                operation=operation,
                classification=classification,
                event=event,
                classification_basis=basis,
                observed_database_record_sha256=(
                    observed_database_sha256
                ),
                target_manifest=target_manifest,
                target_identity=target_identity,
                observed_publication_binding_exact=(
                    exact_binding is not None
                ),
                expected_before_sha256=before,
                expected_after_sha256=after,
                plan_status=str(preparation["status"]),
            )
            terminal_entry_sha256 = (
                _finish_historical_ai_panorama_install_operation(
                    permit_relpath,
                    expected,
                    operation,
                    event=event,
                    evidence=evidence,
                )
            )
    return {
        "classification": classification,
        "event": event,
        "operation_id": operation.operation_id,
        "operation_terminal_entry_sha256": terminal_entry_sha256,
        "operation_terminal_evidence_sha256": hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "permit_sha256": operation.permit_sha256,
        "request_id_sha256": operation.request_id_sha256,
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "private_values_redacted": True,
    }


def run_prater_ai_panorama_artifact_preflight(
    admission: object,
) -> dict[str, object]:
    """Run the fixed no-network, read-only preflight without consuming authority."""

    verified = _validated_prater_admission(
        admission,
        require_consumed=False,
    )
    install_receipt = install_sealed_ai_panorama_bundle(
        _prater_install_request(verified),
        apply=False,
        publication_admission=admission,
        artifact_preflight_only=True,
    )
    status = str(install_receipt.get("status") or "")
    if status not in {
        "artifact_preflight_validated",
        "artifact_preflight_already_installed",
    }:
        _fail("ai_panorama_prater_artifact_preflight_failed")
    return {
        "contract": PRATER_AI_PANORAMA_RELEASE_CONTRACT,
        "mode": "artifact_preflight",
        "status": "preflight_passed",
        "slug": PRATER_SLUG,
        "control_path": f"/tours/{PRATER_SLUG}/control",
        "install_receipt": install_receipt,
        "nonce_consumed": False,
        "database_access_performed": False,
        "release_eligible": False,
        "private_values_redacted": True,
    }


def run_prater_ai_panorama_release(
    admission: object,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Run the exact Prater install and owner-receipt CAS binding.

    Admission verification and, for apply, durable nonce consumption happen
    before any artifact path is opened. The source remains in its preserved
    fixed-root location; this operation never copies it into an ad-hoc root.
    """

    verified = _validated_prater_admission(
        admission,
        require_consumed=apply,
    )
    request = _prater_install_request(verified)
    if not apply:
        install_receipt = install_sealed_ai_panorama_bundle(
            request,
            apply=False,
            publication_admission=admission,
        )
    else:
        pre_target_manifest = _target_manifest(verified)
        if pre_target_manifest.get("reserved_entry_count") != 0:
            _fail("ai_panorama_prater_recovery_required")
        binding_preparation = prepare_ai_panorama_publication_binding(
            request,
            publication_admission=admission,
        )
        request.update(
            {
                "publication_binding_expected_before_sha256": (
                    binding_preparation[
                        "publication_binding_expected_before_sha256"
                    ]
                ),
                "publication_binding_expected_after_sha256": (
                    binding_preparation[
                        "publication_binding_expected_after_sha256"
                    ]
                ),
                "publication_binding_bound_at": binding_preparation[
                    "publication_binding_bound_at"
                ],
                "publication_binding_expected_status": (
                    binding_preparation["status"]
                ),
            }
        )
        try:
            operation = begin_ai_panorama_install_operation(
                verified,
                evidence=_operation_evidence(
                    verified,
                    phase="prepared",
                    target_manifest=pre_target_manifest,
                    binding_preparation=binding_preparation,
                ),
            )
        except Exception as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_operation_journal_unavailable"
            ) from exc
        try:
            install_receipt = install_sealed_ai_panorama_bundle(
                request,
                apply=True,
                publication_admission=admission,
            )
            post_target_manifest = _target_manifest(verified)
            if (
                post_target_manifest.get("state") != "present"
                or post_target_manifest.get("reserved_entry_count") != 0
            ):
                _fail("ai_panorama_prater_target_not_published")
        except Exception as exc:
            error_code = (
                exc.code
                if isinstance(exc, AiPanoramaIntakeError)
                else "ai_panorama_prater_install_failed"
            )
            publication_outcome = str(
                getattr(exc, "publication_outcome", "unknown") or "unknown"
            ).strip()
            try:
                failed_target_manifest = _target_manifest(verified)
                unchanged = failed_target_manifest == pre_target_manifest
                event = (
                    "recovery-required"
                    if publication_outcome == "ambiguous"
                    or bool(getattr(exc, "commit_outcome_ambiguous", False))
                    else "rolled-back"
                    if publication_outcome == "uncommitted"
                    and unchanged
                    and bool(getattr(exc, "rollback_performed", False))
                    else "failed-clean"
                    if publication_outcome == "uncommitted" and unchanged
                    else "recovery-required"
                )
                finish_ai_panorama_install_operation(
                    operation,
                    event=event,
                    evidence=_operation_evidence(
                        verified,
                        phase=event,
                        target_manifest=failed_target_manifest,
                        error_code=error_code,
                        publication_outcome=publication_outcome,
                    ),
                )
            except Exception as journal_exc:
                raise AiPanoramaIntakeError(
                    "ai_panorama_prater_recovery_required"
                ) from journal_exc
            if event == "recovery-required":
                raise AiPanoramaIntakeError(
                    "ai_panorama_prater_recovery_required"
                ) from exc
            if isinstance(exc, AiPanoramaIntakeError):
                raise
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_install_failed"
            ) from exc
    result: dict[str, object] = {
        "contract": PRATER_AI_PANORAMA_RELEASE_CONTRACT,
        "mode": "apply" if apply else "dry_run",
        "status": str(install_receipt.get("status") or ""),
        "slug": PRATER_SLUG,
        "control_path": f"/tours/{PRATER_SLUG}/control",
        "install_receipt": install_receipt,
        "binding_status": "requires_installed_owner_receipt",
        "release_eligible": False,
        "private_values_redacted": True,
    }
    if not apply:
        return result

    if install_receipt.get("publication_binding_verified") is not True:
        try:
            finish_ai_panorama_install_operation(
                operation,
                event="recovery-required",
                evidence=_operation_evidence(
                    verified,
                    phase="recovery-required",
                    target_manifest=post_target_manifest,
                    install_receipt=install_receipt,
                    error_code="ai_panorama_prater_owner_receipt_cas_failed",
                ),
            )
        except Exception as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_recovery_required"
            ) from exc
        _fail("ai_panorama_prater_recovery_required")
    binding_status = str(
        install_receipt.get("publication_binding_status") or ""
    ).strip()
    if binding_status not in {"applied", "already_bound"}:
        try:
            finish_ai_panorama_install_operation(
                operation,
                event="recovery-required",
                evidence=_operation_evidence(
                    verified,
                    phase="recovery-required",
                    target_manifest=post_target_manifest,
                    install_receipt=install_receipt,
                    error_code="ai_panorama_prater_owner_receipt_cas_failed",
                ),
            )
        except Exception as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_recovery_required"
            ) from exc
        _fail("ai_panorama_prater_recovery_required")
    result["binding_status"] = binding_status
    result["binding_receipt"] = _binding_receipt_projection(
        {
            "status": binding_status,
            "mode": "apply",
            "before_sha256": install_receipt.get(
                "publication_binding_before_sha256"
            ),
            "after_sha256": install_receipt.get(
                "publication_binding_after_sha256"
            ),
        }
    )
    result["release_eligible"] = (
        install_receipt.get("release_eligible") is True
        and binding_status in {"applied", "already_bound"}
    )
    result["status"] = "released" if result["release_eligible"] else "failed"
    if not result["release_eligible"]:
        try:
            failed_target_manifest = _target_manifest(verified)
            finish_ai_panorama_install_operation(
                operation,
                event="recovery-required",
                evidence=_operation_evidence(
                    verified,
                    phase="recovery-required",
                    target_manifest=failed_target_manifest,
                    install_receipt=install_receipt,
                    error_code="ai_panorama_prater_release_ineligible",
                ),
            )
        except Exception as exc:
            raise AiPanoramaIntakeError(
                "ai_panorama_prater_recovery_required"
            ) from exc
        _fail("ai_panorama_prater_recovery_required")
    try:
        finish_ai_panorama_install_operation(
            operation,
            event="committed",
            evidence=_operation_evidence(
                verified,
                phase="committed",
                target_manifest=post_target_manifest,
                install_receipt=install_receipt,
            ),
        )
    except Exception as exc:
        raise AiPanoramaIntakeError(
            "ai_panorama_prater_recovery_required"
        ) from exc
    return result


__all__ = [
    "PRATER_AI_PANORAMA_RELEASE_CONTRACT",
    "PRATER_ARTIFACT_RELPATH",
    "PRATER_CANDIDATE_MARKER_SHA256",
    "PRATER_CANDIDATE_REF",
    "PRATER_CONTROLLER_ARTIFACT_ROOT",
    "PRATER_CORE_MANIFEST_SHA256",
    "PRATER_MATERIALIZATION_RECEIPT_RELPATH",
    "PRATER_MATERIALIZATION_RECEIPT_SHA256",
    "PRATER_LISTING_URL",
    "PRATER_PUBLICATION_RECORD_DISCOVERY_CONTRACT",
    "PRATER_SEARCH_RUN_ID",
    "PRATER_SLUG",
    "PRATER_SOURCE_TREE_SHA256",
    "PRATER_TOUR_SHA256",
    "discover_prater_ai_panorama_publication_record",
    "recover_prater_ai_panorama_historical_operation",
    "run_prater_ai_panorama_artifact_preflight",
    "run_prater_ai_panorama_release",
]
