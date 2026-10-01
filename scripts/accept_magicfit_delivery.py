#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from property_magicfit_contact_sheet import (
        MagicFitContactSheetError,
        validate_magicfit_contact_sheet_bytes,
    )
    from property_magicfit_delivery_contract import (
        ACCEPTED_DELIVERY_CONTRACT,
        AUDIT_ARTIFACT_NAMES,
        AUDIT_CONTRACT,
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_REVIEW_CONTRACT,
        EVIDENCE_CONTRACT,
        MANIFEST_TRANSFORM_CONTRACT,
        PENDING_DELIVERY_CONTRACT,
        PENDING_POINTER_RELPATH,
        PENDING_SIDECAR_FIELDS,
        PUBLIC_VIDEO_EXTENSIONS,
        SHA256_RE,
        VISUAL_REVIEW_CONTRACT,
        audit_relpaths,
        build_audit_entry,
        coverage_proof_from_receipt,
        delivery_digest as _contract_delivery_digest,
        require_exact_candidate_manifest,
        require_positive_json_integer,
        require_positive_json_number,
        validate_magicfit_source_receipt,
    )
    from property_magicfit_secure_io import (
        MagicFitSecureIOError,
        StableFileSnapshot,
        hash_stable_bounded_file,
        lexical_absolute_path,
        load_magicfit_review_receipt_bundle,
        read_stable_bounded_bytes,
    )
    from property_magicfit_reviewer_authority import (
        AUTHORIZATION_MAX_BYTES,
        MagicFitReviewerAuthorityError,
        magicfit_reviewer_test_allowed_owner_uids,
        verify_magicfit_reviewer_authorization,
    )
    from property_tour_publication_lock import property_tour_publication_lock
except ModuleNotFoundError:
    from scripts.property_magicfit_contact_sheet import (  # type: ignore[no-redef]
        MagicFitContactSheetError,
        validate_magicfit_contact_sheet_bytes,
    )
    from scripts.property_magicfit_delivery_contract import (
        ACCEPTED_DELIVERY_CONTRACT,
        AUDIT_ARTIFACT_NAMES,
        AUDIT_CONTRACT,
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_REVIEW_CONTRACT,
        EVIDENCE_CONTRACT,
        MANIFEST_TRANSFORM_CONTRACT,
        PENDING_DELIVERY_CONTRACT,
        PENDING_POINTER_RELPATH,
        PENDING_SIDECAR_FIELDS,
        PUBLIC_VIDEO_EXTENSIONS,
        SHA256_RE,
        VISUAL_REVIEW_CONTRACT,
        audit_relpaths,
        build_audit_entry,
        coverage_proof_from_receipt,
        delivery_digest as _contract_delivery_digest,
        require_exact_candidate_manifest,
        require_positive_json_integer,
        require_positive_json_number,
        validate_magicfit_source_receipt,
    )
    from scripts.property_magicfit_secure_io import (
        MagicFitSecureIOError,
        StableFileSnapshot,
        hash_stable_bounded_file,
        lexical_absolute_path,
        load_magicfit_review_receipt_bundle,
        read_stable_bounded_bytes,
    )
    from scripts.property_magicfit_reviewer_authority import (
        AUTHORIZATION_MAX_BYTES,
        MagicFitReviewerAuthorityError,
        magicfit_reviewer_test_allowed_owner_uids,
        verify_magicfit_reviewer_authorization,
    )
    from scripts.property_tour_publication_lock import (
        property_tour_publication_lock,
    )

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.property_render_video_probe import (  # noqa: E402
    PropertyRenderVideoProbeError,
    probe_local_video,
)


# Pending, accepted, review, and evidence contracts advance independently and
# reject every legacy profile.  Keep compatibility aliases for existing callers.
DELIVERY_CONTRACT = PENDING_DELIVERY_CONTRACT
REVIEW_CONTRACT = DELIVERY_REVIEW_CONTRACT
UTC_TIMESTAMP_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)\Z"
)
MAX_FUTURE_SKEW = timedelta(minutes=5)
ACTIVATION_LOCK_RELPATH = ".magicfit-activation.lock"
EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "source_receipt_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "video",
        "checklist",
        "artifacts",
    }
)
EVIDENCE_VIDEO_FIELDS = frozenset({"sha256", "size_bytes", "duration_seconds"})
EVIDENCE_ARTIFACT_FIELDS = frozenset(
    {
        "contact_sheet_sha256",
        "browser_receipt_sha256",
        "visual_review_sha256",
    }
)
BROWSER_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "route",
        "http_status",
        "video_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "duration_seconds",
        "final_current_time",
        "playback_to_end",
        "video_error",
        "console_errors",
        "request_failures",
        "benign_request_aborts",
        "bad_responses",
    }
)
REVIEW_CHECKS = frozenset(
    {
        "playback_to_end",
        "continuous_walkthrough",
        "no_visible_rotation_jump",
        "intended_property_and_scope",
        "no_sensitive_or_trial_branding",
    }
)
VISUAL_REVIEW_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "video_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "checklist",
    }
)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _load_json_bytes(path: Path, *, error: str) -> tuple[dict[str, Any], bytes]:
    body = _read_regular_file_bytes(
        path,
        error=error,
        maximum_bytes=8 * 1024 * 1024,
    )
    return _load_json_body(body, error=error), body


def _load_json_body(body: bytes, *, error: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except Exception as exc:
        raise SystemExit(f"{error}:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(error)
    return dict(payload)


def _read_regular_file_bytes(
    path: Path,
    *,
    error: str,
    maximum_bytes: int,
) -> bytes:
    """Read/hash one stable view without following any path component."""

    try:
        snapshot = read_stable_bounded_bytes(
            path,
            reason=error,
            maximum_bytes=maximum_bytes,
        )
    except MagicFitSecureIOError as exc:
        raise SystemExit(str(exc)) from exc
    assert snapshot.body is not None
    return snapshot.body


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _stable_hash(
    path: Path,
    *,
    error: str,
    maximum_bytes: int,
) -> StableFileSnapshot:
    try:
        return hash_stable_bounded_file(
            path,
            reason=error,
            maximum_bytes=maximum_bytes,
        )
    except MagicFitSecureIOError as exc:
        raise SystemExit(str(exc)) from exc


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    return flags | getattr(os, "O_CLOEXEC", 0)


def _open_directory_componentwise(path: Path, *, error: str) -> int:
    try:
        absolute = lexical_absolute_path(path)
    except MagicFitSecureIOError as exc:
        raise SystemExit(error) from exc
    parts = absolute.parts
    if not parts or parts[0] != absolute.anchor or not hasattr(os, "O_NOFOLLOW"):
        raise SystemExit(error)
    try:
        current_fd = os.open(absolute.anchor, _directory_open_flags())
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise SystemExit(error)
            next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        metadata = os.fstat(current_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(error)
        return current_fd
    except BaseException:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(current_fd)
        raise


def _open_bundle_parent(
    bundle_fd: int,
    relpath: str,
    *,
    error: str,
    create: bool = False,
    directory_mode: int = 0o700,
    required_prefix: str = "",
) -> tuple[int, str]:
    canonical = _canonical_relpath(relpath)
    if not canonical:
        raise SystemExit(error)
    parts = PurePosixPath(canonical).parts
    if required_prefix and (len(parts) < 2 or parts[0] != required_prefix):
        raise SystemExit(error)
    current_fd = os.dup(bundle_fd)
    try:
        for component in parts[:-1]:
            created = False
            if create:
                try:
                    os.mkdir(component, directory_mode, dir_fd=current_fd)
                    os.fsync(current_fd)
                    created = True
                except FileExistsError:
                    pass
            next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise SystemExit(error)
            if create or created:
                os.fchmod(next_fd, directory_mode)
            os.close(current_fd)
            current_fd = next_fd
        result = current_fd
        current_fd = -1
        return result, parts[-1]
    except (OSError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        raise SystemExit(f"{error}:{type(exc).__name__}") from exc
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _bundle_file_snapshot(
    bundle_fd: int,
    relpath: str,
    *,
    error: str,
    maximum_bytes: int,
    include_body: bool,
) -> StableFileSnapshot:
    parent_fd, name = _open_bundle_parent(bundle_fd, relpath, error=error)
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise SystemExit(error)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SystemExit(error)
            digest.update(chunk)
            if include_body:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_mode),
            int(after.st_nlink),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        before_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_nlink),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        if identity != before_identity or total != before.st_size:
            raise SystemExit(error)
        return StableFileSnapshot(
            body=b"".join(chunks) if include_body else None,
            sha256=digest.hexdigest(),
            size_bytes=total,
            identity=identity,
        )
    except OSError as exc:
        raise SystemExit(f"{error}:{type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _read_bundle_file_bytes(
    bundle_fd: int,
    relpath: str,
    *,
    error: str,
    maximum_bytes: int,
) -> bytes:
    snapshot = _bundle_file_snapshot(
        bundle_fd,
        relpath,
        error=error,
        maximum_bytes=maximum_bytes,
        include_body=True,
    )
    assert snapshot.body is not None
    return snapshot.body


def _hash_bundle_file(
    bundle_fd: int,
    relpath: str,
    *,
    error: str,
    maximum_bytes: int,
) -> StableFileSnapshot:
    return _bundle_file_snapshot(
        bundle_fd,
        relpath,
        error=error,
        maximum_bytes=maximum_bytes,
        include_body=False,
    )


def _bundle_entry_exists(bundle_fd: int, relpath: str, *, error: str) -> bool:
    parent_fd, name = _open_bundle_parent(bundle_fd, relpath, error=error)
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(parent_fd)


def _write_bundle_bytes_atomic(
    bundle_fd: int,
    relpath: str,
    body: bytes,
    *,
    mode: int,
    required_prefix: str = "",
    directory_mode: int = 0o700,
) -> None:
    parent_fd, name = _open_bundle_parent(
        bundle_fd,
        relpath,
        error="magicfit_acceptance_delivery_path_invalid",
        create=True,
        directory_mode=directory_mode,
        required_prefix=required_prefix,
    )
    temporary = f".{name}.{os.urandom(8).hex()}.tmp"
    descriptor = -1
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, mode, dir_fd=parent_fd)
        view = memoryview(body)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_nlink != 1
            or temporary_metadata.st_size != len(body)
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino)
            != (temporary_metadata.st_dev, temporary_metadata.st_ino)
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        os.fsync(parent_fd)
    except OSError as exc:
        raise SystemExit(f"magicfit_acceptance_delivery_path_invalid:{type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
        os.close(parent_fd)


def _copy_bundle_file_atomic(
    bundle_fd: int,
    source_relpath: str,
    destination_relpath: str,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
    maximum_bytes: int,
    destination_prefix: str = "",
    destination_directory_mode: int = 0o700,
    destination_file_mode: int,
) -> StableFileSnapshot:
    source_fd, source_name = _open_bundle_parent(
        bundle_fd,
        source_relpath,
        error="magicfit_acceptance_delivery_path_invalid",
    )
    destination_fd, destination_name = _open_bundle_parent(
        bundle_fd,
        destination_relpath,
        error="magicfit_acceptance_delivery_path_invalid",
        create=True,
        directory_mode=destination_directory_mode,
        required_prefix=destination_prefix,
    )
    source_descriptor = -1
    destination_descriptor = -1
    temporary = f".{destination_name}.{os.urandom(8).hex()}.tmp"
    try:
        source_descriptor = os.open(
            source_name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=source_fd,
        )
        source_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or source_metadata.st_size < 0
            or source_metadata.st_size > maximum_bytes
            or source_metadata.st_size != expected_size_bytes
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        destination_descriptor = os.open(
            temporary,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            destination_file_mode,
            dir_fd=destination_fd,
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(
                source_descriptor,
                min(1024 * 1024, maximum_bytes + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SystemExit("magicfit_acceptance_delivery_path_invalid")
            digest.update(chunk)
            view = memoryview(chunk)
            written = 0
            while written < len(view):
                count = os.write(destination_descriptor, view[written:])
                if count <= 0:
                    raise OSError("short write")
                written += count
        source_after = os.fstat(source_descriptor)
        source_identity = (
            int(source_metadata.st_dev),
            int(source_metadata.st_ino),
            int(source_metadata.st_mode),
            int(source_metadata.st_nlink),
            int(source_metadata.st_size),
            int(source_metadata.st_mtime_ns),
            int(source_metadata.st_ctime_ns),
        )
        source_after_identity = (
            int(source_after.st_dev),
            int(source_after.st_ino),
            int(source_after.st_mode),
            int(source_after.st_nlink),
            int(source_after.st_size),
            int(source_after.st_mtime_ns),
            int(source_after.st_ctime_ns),
        )
        if (
            source_after_identity != source_identity
            or total != expected_size_bytes
            or digest.hexdigest() != expected_sha256
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        os.fchmod(destination_descriptor, destination_file_mode)
        os.fsync(destination_descriptor)
        destination_metadata = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(destination_metadata.st_mode)
            or destination_metadata.st_nlink != 1
            or destination_metadata.st_size != expected_size_bytes
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        os.replace(
            temporary,
            destination_name,
            src_dir_fd=destination_fd,
            dst_dir_fd=destination_fd,
        )
        published_metadata = os.stat(
            destination_name,
            dir_fd=destination_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published_metadata.st_mode)
            or published_metadata.st_nlink != 1
            or (published_metadata.st_dev, published_metadata.st_ino)
            != (destination_metadata.st_dev, destination_metadata.st_ino)
        ):
            raise SystemExit("magicfit_acceptance_delivery_path_invalid")
        os.fsync(source_fd)
        os.fsync(destination_fd)
        return StableFileSnapshot(
            body=None,
            sha256=expected_sha256,
            size_bytes=expected_size_bytes,
            identity=(
                int(destination_metadata.st_dev),
                int(destination_metadata.st_ino),
                int(destination_metadata.st_mode),
                int(destination_metadata.st_nlink),
                int(destination_metadata.st_size),
                int(destination_metadata.st_mtime_ns),
                int(destination_metadata.st_ctime_ns),
            ),
        )
    except OSError as exc:
        raise SystemExit(f"magicfit_acceptance_delivery_path_invalid:{type(exc).__name__}") from exc
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=destination_fd)
        os.close(destination_fd)
        os.close(source_fd)


def _unlink_bundle_entry(bundle_fd: int, relpath: str, *, error: str) -> bool:
    parent_fd, name = _open_bundle_parent(bundle_fd, relpath, error=error)
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit(error)
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _remove_empty_stage_at(bundle_fd: int, delivery_digest: str) -> bool:
    if SHA256_RE.fullmatch(delivery_digest) is None:
        raise SystemExit("magicfit_staging_path_invalid")
    try:
        staging_fd = os.open(".magicfit-staging", _directory_open_flags(), dir_fd=bundle_fd)
    except FileNotFoundError:
        return False
    stage_fd = -1
    try:
        try:
            stage_fd = os.open(delivery_digest, _directory_open_flags(), dir_fd=staging_fd)
        except FileNotFoundError:
            return False
        with os.scandir(stage_fd) as entries:
            if next(entries, None) is not None:
                return False
        os.close(stage_fd)
        stage_fd = -1
        os.rmdir(delivery_digest, dir_fd=staging_fd)
        os.fsync(staging_fd)
        with os.scandir(staging_fd) as entries:
            staging_empty = next(entries, None) is None
        if staging_empty:
            with contextlib.suppress(OSError):
                os.rmdir(".magicfit-staging", dir_fd=bundle_fd)
                os.fsync(bundle_fd)
        return True
    except OSError as exc:
        raise SystemExit(f"magicfit_staging_path_invalid:{type(exc).__name__}") from exc
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)
        os.close(staging_fd)


@contextlib.contextmanager
def _activation_lock(bundle_dir: Path):
    bundle_fd = _open_directory_componentwise(
        bundle_dir,
        error="magicfit_acceptance_bundle_invalid",
    )
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(ACTIVATION_LOCK_RELPATH, flags, 0o600, dir_fd=bundle_fd)
    except BaseException:
        os.close(bundle_fd)
        raise
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit("magicfit_activation_lock_invalid")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield bundle_fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(bundle_fd)


def _confirm_named_bundle_identity(
    bundle_dir: Path,
    bundle_fd: int,
    *,
    error: str = "magicfit_acceptance_bundle_changed",
) -> None:
    """Require the public slug name to still identify the held directory."""

    held_identity = os.fstat(bundle_fd)
    try:
        named_identity = os.stat(bundle_dir, follow_symlinks=False)
    except OSError as exc:
        raise SystemExit(error) from exc
    if (
        not stat.S_ISDIR(held_identity.st_mode)
        or not stat.S_ISDIR(named_identity.st_mode)
        or (held_identity.st_dev, held_identity.st_ino)
        != (named_identity.st_dev, named_identity.st_ino)
    ):
        raise SystemExit(error)


def _activation_failpoint(name: str) -> None:
    # Test-only denial injection proves each crash boundary.  The guard keeps
    # production operators from accidentally enabling a fault through ambient
    # configuration; it can only make tests fail closed.
    if not os.getenv("PYTEST_CURRENT_TEST"):
        return
    if os.getenv("PROPERTYQUARRY_MAGICFIT_ACTIVATION_FAILPOINT") == name:
        raise SystemExit(f"magicfit_activation_test_failpoint:{name}")


def _valid_sha256(value: object) -> str:
    return value if isinstance(value, str) and SHA256_RE.fullmatch(value) else ""


def _canonical_relpath(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if value.startswith("/") or "\\" in value:
        return ""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    return value if PurePosixPath(value).as_posix() == value else ""


def _strict_utc(value: object, *, require_z: bool) -> datetime | None:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        return None
    if require_z and not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _acceptance_reviewed_at(
    *,
    generated_at: datetime,
    browser_receipt: dict[str, Any],
    visual_review: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime | None = None,
) -> str:
    reviewed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0
    )
    observed_at_values = tuple(
        _strict_utc(payload.get("observed_at"), require_z=True)
        for payload in (browser_receipt, visual_review, evidence)
    )
    if any(value is None for value in observed_at_values):
        raise SystemExit("magicfit_acceptance_review_timestamp_invalid")
    latest_subject_timestamp = max(
        generated_at,
        *(value for value in observed_at_values if value is not None),
    )
    if reviewed_at < latest_subject_timestamp:
        raise SystemExit("magicfit_acceptance_review_timestamp_invalid")
    return reviewed_at.isoformat().replace("+00:00", "Z")


def _reviewer_trusted_owner_uids_for_tests() -> list[int] | None:
    try:
        return magicfit_reviewer_test_allowed_owner_uids()
    except MagicFitReviewerAuthorityError as exc:
        raise SystemExit(exc.reason) from exc


def _video_probe(
    path: Path,
    *,
    expected_size_bytes: int | None = None,
    _probe_descriptor: int | None = None,
) -> dict[str, object]:
    suffix = path.suffix.lower()
    if suffix not in PUBLIC_VIDEO_EXTENSIONS:
        raise SystemExit("magicfit_acceptance_video_extension_invalid")
    try:
        if _probe_descriptor is None:
            probe = probe_local_video(path)
        else:
            expected_size = int(expected_size_bytes or 0)
            if expected_size <= 0:
                raise SystemExit("magicfit_acceptance_video_probe_invalid")
            with tempfile.TemporaryDirectory(
                prefix="propertyquarry-video-probe-"
            ) as temporary_root:
                probe_path = Path(temporary_root) / f"asset{suffix}"
                probe_fd = os.open(
                    probe_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    copied = 0
                    while True:
                        chunk = os.read(_probe_descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > expected_size:
                            raise SystemExit(
                                "magicfit_acceptance_video_probe_invalid"
                            )
                        view = memoryview(chunk)
                        while view:
                            written = os.write(probe_fd, view)
                            view = view[written:]
                    if copied != expected_size:
                        raise SystemExit("magicfit_acceptance_video_probe_invalid")
                    os.fsync(probe_fd)
                finally:
                    os.close(probe_fd)
                probe = probe_local_video(probe_path)
                os.lseek(_probe_descriptor, 0, os.SEEK_SET)
    except PropertyRenderVideoProbeError as exc:
        raise SystemExit(f"magicfit_acceptance_video_probe_failed:{exc}") from exc
    except OSError as exc:
        raise SystemExit(
            f"magicfit_acceptance_video_probe_failed:{type(exc).__name__}"
        ) from exc
    expected_size = (
        int(expected_size_bytes)
        if expected_size_bytes is not None
        else int(path.stat(follow_symlinks=False).st_size)
    )
    if int(probe.get("size_bytes") or 0) != expected_size:
        raise SystemExit("magicfit_acceptance_video_probe_invalid")
    return {
        "duration_seconds": probe["duration_seconds"],
        "size_bytes": probe["size_bytes"],
    }


def _probe_bundle_video(
    bundle_fd: int,
    relpath: str,
    *,
    error: str,
    expected_sha256: str,
    expected_size_bytes: int,
    maximum_bytes: int,
) -> tuple[StableFileSnapshot, dict[str, object]]:
    parent_fd, name = _open_bundle_parent(bundle_fd, relpath, error=error)
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum_bytes
            or before.st_size != expected_size_bytes
        ):
            raise SystemExit(error)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, maximum_bytes + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SystemExit(error)
            digest.update(chunk)
        if total != expected_size_bytes or digest.hexdigest() != expected_sha256:
            raise SystemExit(error)
        os.lseek(descriptor, 0, os.SEEK_SET)
        probe = _video_probe(
            Path(relpath),
            expected_size_bytes=expected_size_bytes,
            _probe_descriptor=descriptor,
        )
        after = os.fstat(descriptor)
        identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_mode),
            int(after.st_nlink),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        before_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_nlink),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        if identity != before_identity:
            raise SystemExit(error)
        return (
            StableFileSnapshot(
                body=None,
                sha256=expected_sha256,
                size_bytes=expected_size_bytes,
                identity=identity,
            ),
            probe,
        )
    except OSError as exc:
        raise SystemExit(f"{error}:{type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _source_receipt_valid(payload: dict[str, Any], *, slug: str) -> bool:
    try:
        validate_magicfit_source_receipt(payload, slug=slug)
    except ValueError:
        return False
    return True


def _validate_visual_review(
    payload: dict[str, Any],
    *,
    slug: str,
    generated_at: datetime,
    video_sha256: str,
    base_manifest_sha256: str,
    staged_manifest_sha256: str,
    delivery_digest: str,
    error_prefix: str = "magicfit_acceptance",
) -> dict[str, bool]:
    if (
        set(payload) != VISUAL_REVIEW_FIELDS
        or payload.get("schema") != VISUAL_REVIEW_CONTRACT
        or payload.get("status") != "pass"
        or payload.get("provider") != "magicfit"
        or payload.get("target_slug") != slug
        or payload.get("video_sha256") != video_sha256
        or payload.get("base_manifest_sha256") != base_manifest_sha256
        or payload.get("staged_manifest_sha256") != staged_manifest_sha256
        or payload.get("delivery_digest") != delivery_digest
    ):
        raise SystemExit(f"{error_prefix}_visual_review_contract_invalid")
    observed_at = _strict_utc(payload.get("observed_at"), require_z=True)
    now = datetime.now(timezone.utc)
    if (
        observed_at is None
        or observed_at < generated_at
        or observed_at > now + MAX_FUTURE_SKEW
    ):
        raise SystemExit(f"{error_prefix}_visual_review_timestamp_invalid")
    checklist = payload.get("checklist")
    if not isinstance(checklist, dict) or set(checklist) != REVIEW_CHECKS:
        raise SystemExit(f"{error_prefix}_visual_review_checklist_invalid")
    if not all(checklist.get(key) is True for key in REVIEW_CHECKS):
        raise SystemExit(f"{error_prefix}_visual_review_checklist_failed")
    return {key: True for key in sorted(REVIEW_CHECKS)}


def _validate_evidence(
    payload: dict[str, Any],
    *,
    slug: str,
    generated_at: datetime,
    video_sha256: str,
    video_probe: dict[str, object],
    source_receipt_sha256: str,
    base_manifest_sha256: str,
    staged_manifest_sha256: str,
    delivery_digest: str,
    contact_sheet_sha256: str,
    browser_receipt_sha256: str,
    visual_review_sha256: str,
) -> dict[str, bool]:
    if set(payload) != EVIDENCE_FIELDS:
        raise SystemExit("magicfit_acceptance_evidence_contract_invalid")
    if (
        payload.get("schema") != EVIDENCE_CONTRACT
        or payload.get("status") != "pass"
        or payload.get("provider") != "magicfit"
        or payload.get("target_slug") != slug
        or payload.get("source_receipt_sha256") != source_receipt_sha256
        or payload.get("base_manifest_sha256") != base_manifest_sha256
        or payload.get("staged_manifest_sha256") != staged_manifest_sha256
        or payload.get("delivery_digest") != delivery_digest
    ):
        raise SystemExit("magicfit_acceptance_evidence_contract_invalid")
    observed_at = _strict_utc(payload.get("observed_at"), require_z=True)
    now = datetime.now(timezone.utc)
    if (
        observed_at is None
        or observed_at < generated_at
        or observed_at > now + MAX_FUTURE_SKEW
    ):
        raise SystemExit("magicfit_acceptance_evidence_timestamp_invalid")

    video = payload.get("video")
    if not isinstance(video, dict) or set(video) != EVIDENCE_VIDEO_FIELDS:
        raise SystemExit("magicfit_acceptance_evidence_video_invalid")
    try:
        evidence_size = require_positive_json_integer(video.get("size_bytes"))
        evidence_duration = require_positive_json_number(
            video.get("duration_seconds")
        )
    except ValueError:
        raise SystemExit("magicfit_acceptance_evidence_video_invalid")
    if (
        video.get("sha256") != video_sha256
        or evidence_size != int(video_probe["size_bytes"])
        or not math.isfinite(evidence_duration)
        or evidence_duration <= 0.0
        or abs(evidence_duration - float(video_probe["duration_seconds"])) > 0.1
    ):
        raise SystemExit("magicfit_acceptance_evidence_video_mismatch")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != EVIDENCE_ARTIFACT_FIELDS:
        raise SystemExit("magicfit_acceptance_evidence_artifacts_invalid")
    if (
        artifacts.get("contact_sheet_sha256") != contact_sheet_sha256
        or artifacts.get("browser_receipt_sha256") != browser_receipt_sha256
        or artifacts.get("visual_review_sha256") != visual_review_sha256
    ):
        raise SystemExit("magicfit_acceptance_evidence_artifacts_mismatch")

    checklist = payload.get("checklist")
    if not isinstance(checklist, dict) or set(checklist) != REVIEW_CHECKS:
        raise SystemExit("magicfit_acceptance_evidence_checklist_invalid")
    if not all(checklist.get(key) is True for key in REVIEW_CHECKS):
        raise SystemExit("magicfit_acceptance_evidence_checklist_failed")
    return {key: True for key in sorted(REVIEW_CHECKS)}


def _validate_browser_receipt(
    payload: dict[str, Any],
    *,
    slug: str,
    generated_at: datetime,
    video_sha256: str,
    base_manifest_sha256: str,
    staged_manifest_sha256: str,
    delivery_digest: str,
    video_duration: float,
) -> None:
    if set(payload) != BROWSER_RECEIPT_FIELDS:
        raise SystemExit("magicfit_acceptance_browser_receipt_contract_invalid")
    observed_at = _strict_utc(payload.get("observed_at"), require_z=True)
    now = datetime.now(timezone.utc)
    try:
        duration = require_positive_json_number(payload.get("duration_seconds"))
        final_current_time = require_positive_json_number(
            payload.get("final_current_time")
        )
    except ValueError:
        raise SystemExit("magicfit_acceptance_browser_receipt_contract_invalid")
    benign_request_aborts = payload.get("benign_request_aborts")
    if not isinstance(benign_request_aborts, list) or len(benign_request_aborts) > 1:
        raise SystemExit("magicfit_acceptance_browser_receipt_contract_invalid")
    review_route = (
        f"operator-review://propertyquarry/magicfit/{slug}/{video_sha256}"
    )
    expected_benign_abort = {
        "failure": "net::ERR_ABORTED",
        "method": "GET",
        "resource_type": "media",
        "route": review_route,
    }
    if any(row != expected_benign_abort for row in benign_request_aborts):
        raise SystemExit("magicfit_acceptance_browser_receipt_contract_invalid")
    if (
        payload.get("schema") != BROWSER_RECEIPT_CONTRACT
        or payload.get("status") != "pass"
        or payload.get("provider") != "magicfit"
        or payload.get("target_slug") != slug
        or payload.get("route") != review_route
        or payload.get("http_status") != 200
        or payload.get("video_sha256") != video_sha256
        or payload.get("base_manifest_sha256") != base_manifest_sha256
        or payload.get("staged_manifest_sha256") != staged_manifest_sha256
        or payload.get("delivery_digest") != delivery_digest
        or payload.get("playback_to_end") is not True
        or payload.get("video_error") is not None
        or payload.get("console_errors") != []
        or payload.get("request_failures") != []
        or payload.get("bad_responses") != []
        or observed_at is None
        or observed_at < generated_at
        or observed_at > now + MAX_FUTURE_SKEW
        or not math.isfinite(duration)
        or not math.isfinite(final_current_time)
        or duration <= 0.0
        or abs(duration - video_duration) > 0.1
        or final_current_time < duration - 0.25
    ):
        raise SystemExit("magicfit_acceptance_browser_receipt_contract_invalid")


def _persist_audit_artifact(
    bundle_dir: Path,
    *,
    bundle_fd: int,
    relpath: str,
    body: bytes,
) -> Path:
    parent_fd, name = _open_bundle_parent(
        bundle_fd,
        relpath,
        error="magicfit_acceptance_delivery_path_invalid",
        create=True,
        required_prefix=".magicfit-deliveries",
        directory_mode=0o700,
    )
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
        ):
            raise SystemExit("magicfit_acceptance_audit_artifact_conflict")
    finally:
        os.close(parent_fd)
    if metadata is not None:
        existing = _read_bundle_file_bytes(
            bundle_fd,
            relpath,
            error="magicfit_acceptance_audit_artifact_conflict",
            maximum_bytes=max(len(body), 1),
        )
        if existing != body:
            raise SystemExit("magicfit_acceptance_audit_artifact_conflict")
        return bundle_dir / relpath
    _write_bundle_bytes_atomic(
        bundle_fd,
        relpath,
        body,
        mode=0o600,
        required_prefix=".magicfit-deliveries",
        directory_mode=0o700,
    )
    if _read_bundle_file_bytes(
        bundle_fd,
        relpath,
        error="magicfit_acceptance_audit_artifact_changed",
        maximum_bytes=max(len(body), 1),
    ) != body:
        raise SystemExit("magicfit_acceptance_audit_artifact_changed")
    return bundle_dir / relpath


def _activate_video(
    *,
    bundle_dir: Path,
    bundle_fd: int,
    staged_video_relpath: str,
    final_video_relpath: str,
    video_sha256: str,
    video_size_bytes: int,
) -> Path:
    destination_fd, _destination_name = _open_bundle_parent(
        bundle_fd,
        final_video_relpath,
        error="magicfit_acceptance_delivery_path_invalid",
        create=True,
        required_prefix="magicfit-media",
        directory_mode=0o755,
    )
    os.close(destination_fd)
    final = bundle_dir / final_video_relpath
    if _bundle_entry_exists(
        bundle_fd,
        final_video_relpath,
        error="magicfit_acceptance_active_video_conflict",
    ):
        final_snapshot = _hash_bundle_file(
            bundle_fd,
            final_video_relpath,
            error="magicfit_acceptance_active_video_conflict",
            maximum_bytes=2 * 1024 * 1024 * 1024,
        )
        if (
            final_snapshot.sha256 != video_sha256
            or final_snapshot.size_bytes != video_size_bytes
            or bool(stat.S_IMODE(final_snapshot.identity[2]) & 0o222)
        ):
            raise SystemExit("magicfit_acceptance_active_video_conflict")
        return final
    _copy_bundle_file_atomic(
        bundle_fd,
        staged_video_relpath,
        final_video_relpath,
        expected_sha256=video_sha256,
        expected_size_bytes=video_size_bytes,
        maximum_bytes=2 * 1024 * 1024 * 1024,
        destination_prefix="magicfit-media",
        destination_directory_mode=0o755,
        destination_file_mode=0o444,
    )
    return final


def _acknowledge_existing_acceptance(
    *,
    slug: str,
    bundle_dir: Path,
    bundle_fd: int,
    manifest: dict[str, Any],
) -> int:
    try:
        from property_magicfit_public_eligibility import (
            evaluate_magicfit_public_eligibility,
        )
    except ModuleNotFoundError:
        from scripts.property_magicfit_public_eligibility import (
            evaluate_magicfit_public_eligibility,
        )

    _confirm_named_bundle_identity(bundle_dir, bundle_fd)
    eligibility = evaluate_magicfit_public_eligibility(bundle_dir, manifest)
    _confirm_named_bundle_identity(bundle_dir, bundle_fd)
    if not (
        eligibility.declared
        and eligibility.eligible
        and eligibility.video_relpath
        and eligibility.delivery_digest
    ):
        raise SystemExit("magicfit_acceptance_pending_delivery_missing")
    _remove_empty_stage_at(bundle_fd, eligibility.delivery_digest)
    print(
        json.dumps(
            {
                "status": "delivery_accepted",
                "slug": slug,
                "video_relpath": eligibility.video_relpath,
                "delivery_digest": eligibility.delivery_digest,
                "idempotent_recovery": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _accept_locked(
    *,
    args: argparse.Namespace,
    slug: str,
    bundle_dir: Path,
    bundle_fd: int,
) -> int:
    manifest_bytes = _read_bundle_file_bytes(
        bundle_fd,
        "tour.json",
        error="magicfit_acceptance_manifest_invalid",
        maximum_bytes=8 * 1024 * 1024,
    )
    manifest = _load_json_body(
        manifest_bytes, error="magicfit_acceptance_manifest_invalid"
    )
    try:
        pending_bytes = _read_bundle_file_bytes(
            bundle_fd,
            PENDING_POINTER_RELPATH,
            error="magicfit_acceptance_pending_delivery_missing",
            maximum_bytes=8 * 1024 * 1024,
        )
    except SystemExit as exc:
        if not str(exc).startswith("magicfit_acceptance_pending_delivery_missing"):
            raise
        return _acknowledge_existing_acceptance(
            slug=slug,
            bundle_dir=bundle_dir,
            bundle_fd=bundle_fd,
            manifest=manifest,
        )
    pending = _load_json_body(
        pending_bytes, error="magicfit_acceptance_pending_contract_invalid"
    )
    expected_pending = {
        "contract_name": PENDING_DELIVERY_CONTRACT,
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "rendered_pending_delivery_acceptance",
        "acceptance_status": "pending",
        "launch_eligible": False,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "tour_slug": slug,
    }
    if set(pending) != PENDING_SIDECAR_FIELDS or any(
        pending.get(key) != value for key, value in expected_pending.items()
    ):
        raise SystemExit("magicfit_acceptance_pending_contract_invalid")

    requested_target_relpath = _canonical_relpath(
        pending.get("requested_target_relpath")
    )
    video_relpath = _canonical_relpath(pending.get("video_relpath"))
    staged_video_relpath = _canonical_relpath(pending.get("staged_video_relpath"))
    staged_manifest_relpath = _canonical_relpath(
        pending.get("staged_manifest_relpath")
    )
    accepted_sidecar_relpath = _canonical_relpath(
        pending.get("accepted_sidecar_relpath")
    )
    generated_at_text = pending.get("generated_at")
    generated_at = _strict_utc(generated_at_text, require_z=False)
    video_size_value = pending.get("video_size_bytes")
    video_size_bytes = (
        video_size_value
        if isinstance(video_size_value, int) and not isinstance(video_size_value, bool)
        else 0
    )
    coverage_proof_value = pending.get("coverage_proof")
    coverage_proof = (
        dict(coverage_proof_value)
        if isinstance(coverage_proof_value, dict)
        else None
    )
    video_sha256 = _valid_sha256(pending.get("video_sha256"))
    source_receipt_sha256 = _valid_sha256(pending.get("source_receipt_sha256"))
    base_manifest_sha256 = _valid_sha256(pending.get("base_manifest_sha256"))
    staged_manifest_sha256 = _valid_sha256(
        pending.get("staged_manifest_sha256")
    )
    stage_parts = PurePosixPath(staged_video_relpath).parts
    staged_video_path_value = PurePosixPath(staged_video_relpath)
    stage_manifest_parts = PurePosixPath(staged_manifest_relpath).parts
    sidecar_parts = PurePosixPath(accepted_sidecar_relpath).parts
    delivery_digest = stage_parts[1] if len(stage_parts) >= 3 else ""
    if (
        generated_at is None
        or not isinstance(generated_at_text, str)
        or not requested_target_relpath
        or video_size_bytes <= 0
        or coverage_proof is None
        or not video_sha256
        or not source_receipt_sha256
        or not base_manifest_sha256
        or not staged_manifest_sha256
        or not video_relpath.startswith("magicfit-media/")
        or f".{video_sha256}" not in PurePosixPath(video_relpath).stem
        or len(stage_parts) != 3
        or stage_parts[0] != ".magicfit-staging"
        or staged_video_path_value.stem != "video"
        or staged_video_path_value.suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS
        or SHA256_RE.fullmatch(delivery_digest) is None
        or stage_manifest_parts
        != (".magicfit-staging", delivery_digest, "tour.json")
        or sidecar_parts
        != (".magicfit-deliveries", f"{delivery_digest}.json")
    ):
        raise SystemExit("magicfit_acceptance_pending_contract_invalid")
    if generated_at > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
        raise SystemExit("magicfit_acceptance_pending_timestamp_invalid")

    try:
        expected_delivery_digest = _contract_delivery_digest(
            slug=slug,
            requested_target_relpath=requested_target_relpath,
            video_relpath=video_relpath,
            video_sha256=video_sha256,
            video_size_bytes=video_size_bytes,
            source_receipt_sha256=source_receipt_sha256,
            base_manifest_sha256=base_manifest_sha256,
            generated_at=generated_at_text,
            coverage_proof=coverage_proof,
        )
    except ValueError as exc:
        raise SystemExit("magicfit_acceptance_pending_contract_invalid") from exc
    if delivery_digest != expected_delivery_digest:
        raise SystemExit("magicfit_acceptance_pending_contract_invalid")

    active_manifest_sha256 = _sha256_bytes(manifest_bytes)
    if active_manifest_sha256 not in {
        base_manifest_sha256,
        staged_manifest_sha256,
    }:
        raise SystemExit("magicfit_acceptance_manifest_changed")

    audit_paths = audit_relpaths(delivery_digest)
    if active_manifest_sha256 == base_manifest_sha256:
        base_manifest_bytes = manifest_bytes
        candidate_manifest_bytes = _read_bundle_file_bytes(
            bundle_fd,
            staged_manifest_relpath,
            error="magicfit_acceptance_staged_manifest_invalid",
            maximum_bytes=8 * 1024 * 1024,
        )
    else:
        candidate_manifest_bytes = manifest_bytes
        base_manifest_bytes = _read_bundle_file_bytes(
            bundle_fd,
            audit_paths["base_manifest"],
            error="magicfit_acceptance_base_manifest_audit_invalid",
            maximum_bytes=8 * 1024 * 1024,
        )
    if (
        _sha256_bytes(base_manifest_bytes) != base_manifest_sha256
        or _sha256_bytes(candidate_manifest_bytes) != staged_manifest_sha256
    ):
        raise SystemExit("magicfit_acceptance_manifest_digest_mismatch")
    try:
        require_exact_candidate_manifest(
            staged_manifest_bytes=candidate_manifest_bytes,
            base_manifest_bytes=base_manifest_bytes,
            slug=slug,
            requested_target_relpath=requested_target_relpath,
            video_relpath=video_relpath,
            video_sha256=video_sha256,
            video_size_bytes=video_size_bytes,
            source_receipt_sha256=source_receipt_sha256,
            generated_at=generated_at_text,
            coverage_proof=coverage_proof,
        )
    except ValueError as exc:
        raise SystemExit("magicfit_acceptance_manifest_transform_invalid") from exc

    if _bundle_entry_exists(
        bundle_fd,
        staged_video_relpath,
        error="magicfit_acceptance_pending_video_digest_mismatch",
    ):
        active_video_relpath = staged_video_relpath
    elif _bundle_entry_exists(
        bundle_fd,
        video_relpath,
        error="magicfit_acceptance_pending_video_digest_mismatch",
    ):
        active_video_relpath = video_relpath
    else:
        raise SystemExit("magicfit_acceptance_pending_video_digest_mismatch")
    video_snapshot, video_probe = _probe_bundle_video(
        bundle_fd,
        active_video_relpath,
        error="magicfit_acceptance_pending_video_digest_mismatch",
        expected_sha256=video_sha256,
        expected_size_bytes=video_size_bytes,
        maximum_bytes=2 * 1024 * 1024 * 1024,
    )
    if (
        video_snapshot.size_bytes != video_size_bytes
        or int(video_probe["size_bytes"]) != video_size_bytes
    ):
        raise SystemExit("magicfit_acceptance_pending_video_size_mismatch")

    source_bytes = _read_regular_file_bytes(
        Path(args.source_receipt).expanduser(),
        error="magicfit_acceptance_source_receipt_invalid",
        maximum_bytes=8 * 1024 * 1024,
    )
    source_receipt = _load_json_body(
        source_bytes, error="magicfit_acceptance_source_receipt_invalid"
    )
    try:
        source_coverage_proof = coverage_proof_from_receipt(source_receipt)
    except ValueError as exc:
        raise SystemExit(
            "magicfit_acceptance_source_receipt_digest_mismatch"
        ) from exc
    if (
        _sha256_bytes(source_bytes) != source_receipt_sha256
        or not _source_receipt_valid(source_receipt, slug=slug)
        or source_coverage_proof != coverage_proof
    ):
        raise SystemExit("magicfit_acceptance_source_receipt_digest_mismatch")

    contact_sheet_bytes = _read_regular_file_bytes(
        Path(args.contact_sheet).expanduser(),
        error="magicfit_acceptance_contact_sheet_missing",
        maximum_bytes=128 * 1024 * 1024,
    )
    try:
        reviewer_authority_path = lexical_absolute_path(
            Path(args.reviewer_authority).expanduser()
        )
    except MagicFitSecureIOError as exc:
        raise SystemExit(
            "magicfit_acceptance_reviewer_authority_missing"
        ) from exc
    reviewer_authority_bytes = _read_regular_file_bytes(
        reviewer_authority_path,
        error="magicfit_acceptance_reviewer_authority_missing",
        maximum_bytes=AUTHORIZATION_MAX_BYTES,
    )
    visual_review_bytes = _read_regular_file_bytes(
        Path(args.visual_review).expanduser(),
        error="magicfit_acceptance_visual_review_missing",
        maximum_bytes=64 * 1024,
    )
    try:
        validate_magicfit_contact_sheet_bytes(contact_sheet_bytes)
    except MagicFitContactSheetError:
        raise SystemExit("magicfit_acceptance_contact_sheet_invalid")
    contact_sheet_sha256 = _sha256_bytes(contact_sheet_bytes)
    try:
        review_bundle_path = lexical_absolute_path(args.review_bundle)
    except MagicFitSecureIOError as exc:
        raise SystemExit("magicfit_acceptance_review_receipt_bundle_invalid") from exc
    public_root = bundle_dir.parent
    if review_bundle_path == public_root or public_root in review_bundle_path.parents:
        raise SystemExit("magicfit_acceptance_review_receipt_bundle_public")
    try:
        review_bundle = load_magicfit_review_receipt_bundle(
            review_bundle_path,
            expected_delivery_digest=delivery_digest,
            reason="magicfit_acceptance_review_receipt_bundle_invalid",
        )
    except MagicFitSecureIOError as exc:
        raise SystemExit(str(exc)) from exc
    browser_bytes = review_bundle.browser_receipt_bytes
    browser_payload = _load_json_body(
        browser_bytes, error="magicfit_acceptance_browser_receipt_invalid"
    )
    _validate_browser_receipt(
        browser_payload,
        slug=slug,
        generated_at=generated_at,
        video_sha256=video_sha256,
        base_manifest_sha256=base_manifest_sha256,
        staged_manifest_sha256=staged_manifest_sha256,
        delivery_digest=delivery_digest,
        video_duration=float(video_probe["duration_seconds"]),
    )
    browser_receipt_sha256 = _sha256_bytes(browser_bytes)
    authority_sha256 = _sha256_bytes(reviewer_authority_bytes)
    reviewer_authority_payload = _load_json_body(
        reviewer_authority_bytes,
        error="magicfit_acceptance_reviewer_authorization_invalid",
    )
    reviewer_authority_subject = reviewer_authority_payload.get("subject")
    if not isinstance(reviewer_authority_subject, dict):
        raise SystemExit("magicfit_acceptance_reviewer_authorization_invalid")
    signed_reviewed_at = _strict_utc(
        reviewer_authority_subject.get("reviewed_at"),
        require_z=True,
    )
    if signed_reviewed_at is None:
        raise SystemExit("magicfit_acceptance_reviewer_authorization_invalid")
    visual_review_payload = _load_json_body(
        visual_review_bytes, error="magicfit_acceptance_visual_review_invalid"
    )
    visual_checklist = _validate_visual_review(
        visual_review_payload,
        slug=slug,
        generated_at=generated_at,
        video_sha256=video_sha256,
        base_manifest_sha256=base_manifest_sha256,
        staged_manifest_sha256=staged_manifest_sha256,
        delivery_digest=delivery_digest,
    )
    visual_review_sha256 = _sha256_bytes(visual_review_bytes)

    evidence_bytes = review_bundle.evidence_receipt_bytes
    evidence_sha256 = _sha256_bytes(evidence_bytes)
    evidence = _load_json_body(
        evidence_bytes, error="magicfit_acceptance_evidence_invalid"
    )
    checklist = _validate_evidence(
        evidence,
        slug=slug,
        generated_at=generated_at,
        video_sha256=video_sha256,
        video_probe=video_probe,
        source_receipt_sha256=source_receipt_sha256,
        base_manifest_sha256=base_manifest_sha256,
        staged_manifest_sha256=staged_manifest_sha256,
        delivery_digest=delivery_digest,
        contact_sheet_sha256=contact_sheet_sha256,
        browser_receipt_sha256=browser_receipt_sha256,
        visual_review_sha256=visual_review_sha256,
    )
    if checklist != visual_checklist:
        raise SystemExit("magicfit_acceptance_visual_review_checklist_mismatch")

    reviewed_at = _acceptance_reviewed_at(
        generated_at=generated_at,
        browser_receipt=browser_payload,
        visual_review=visual_review_payload,
        evidence=evidence,
        now=signed_reviewed_at,
    )
    reviewer_authorization_subject = {
        "delivery_digest": delivery_digest,
        "video_sha256": video_sha256,
        "staged_manifest_sha256": staged_manifest_sha256,
        "browser_receipt_sha256": browser_receipt_sha256,
        "evidence_receipt_sha256": evidence_sha256,
        "visual_review_sha256": visual_review_sha256,
        "contact_sheet_sha256": contact_sheet_sha256,
        "reviewed_at": reviewed_at,
    }
    try:
        reviewer_authorization = verify_magicfit_reviewer_authorization(
            reviewer_authority_path,
            expected_subject=reviewer_authorization_subject,
            public_tour_root=public_root,
            allowed_owner_uids=_reviewer_trusted_owner_uids_for_tests(),
        )
    except MagicFitReviewerAuthorityError as exc:
        raise SystemExit(
            f"magicfit_acceptance_reviewer_authorization_invalid:{exc.reason}"
        ) from exc
    if reviewer_authorization.authorization_sha256 != authority_sha256:
        raise SystemExit("magicfit_acceptance_reviewer_authorization_changed")
    reviewer_authorization_projection = reviewer_authorization.as_dict()

    if _read_bundle_file_bytes(
        bundle_fd,
        PENDING_POINTER_RELPATH,
        error="magicfit_acceptance_pending_contract_changed",
        maximum_bytes=max(len(pending_bytes), 1),
    ) != pending_bytes:
        raise SystemExit("magicfit_acceptance_pending_contract_changed")
    observed_manifest_bytes = _read_bundle_file_bytes(
        bundle_fd,
        "tour.json",
        error="magicfit_acceptance_manifest_changed",
        maximum_bytes=8 * 1024 * 1024,
    )
    observed_manifest_sha256 = _sha256_bytes(observed_manifest_bytes)
    if observed_manifest_sha256 != active_manifest_sha256:
        raise SystemExit("magicfit_acceptance_manifest_changed")
    observed_video = _hash_bundle_file(
        bundle_fd,
        active_video_relpath,
        error="magicfit_acceptance_video_changed",
        maximum_bytes=2 * 1024 * 1024 * 1024,
    )
    if (
        observed_video.sha256 != video_sha256
        or observed_video.size_bytes != video_size_bytes
    ):
        raise SystemExit("magicfit_acceptance_video_changed")

    audit_bodies = {
        "base_manifest": base_manifest_bytes,
        "source_receipt": source_bytes,
        "browser_receipt": browser_bytes,
        "evidence_receipt": evidence_bytes,
        "visual_review": visual_review_bytes,
        "reviewer_authority": reviewer_authority_bytes,
        "contact_sheet": contact_sheet_bytes,
    }
    audit_artifacts: dict[str, dict[str, object]] = {}
    for name in AUDIT_ARTIFACT_NAMES:
        body = audit_bodies[name]
        relpath = audit_paths[name]
        _persist_audit_artifact(
            bundle_dir,
            bundle_fd=bundle_fd,
            relpath=relpath,
            body=body,
        )
        audit_artifacts[name] = build_audit_entry(relpath=relpath, body=body)
    audit_payload: dict[str, object] = {
        "contract_name": AUDIT_CONTRACT,
        "artifacts": audit_artifacts,
    }

    accepted: dict[str, object] = {
        "contract_name": ACCEPTED_DELIVERY_CONTRACT,
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "delivery_accepted",
        "acceptance_status": "accepted",
        "launch_eligible": True,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "requested_target_relpath": requested_target_relpath,
        "video_relpath": video_relpath,
        "video_sha256": video_sha256,
        "video_size_bytes": video_size_bytes,
        "source_receipt_sha256": source_receipt_sha256,
        "coverage_proof": coverage_proof,
        "base_manifest_sha256": base_manifest_sha256,
        "staged_manifest_sha256": staged_manifest_sha256,
        "delivery_digest": delivery_digest,
        "generated_at": pending["generated_at"],
        "review": {
            "contract_name": REVIEW_CONTRACT,
            "reviewed_at": reviewed_at,
            "reviewer_authority_sha256": authority_sha256,
            "reviewer_authorization": reviewer_authorization_projection,
            "evidence_sha256": evidence_sha256,
            "visual_review_sha256": visual_review_sha256,
            "subject": {
                "tour_slug": slug,
                "provider": "magicfit",
                "delivery_contract_name": ACCEPTED_DELIVERY_CONTRACT,
                "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
                "requested_target_relpath": requested_target_relpath,
                "source_receipt_sha256": source_receipt_sha256,
                "video_relpath": video_relpath,
                "video_sha256": video_sha256,
                "video_size_bytes": video_size_bytes,
                "coverage_proof": coverage_proof,
                "base_manifest_sha256": base_manifest_sha256,
                "staged_manifest_sha256": staged_manifest_sha256,
                "delivery_digest": delivery_digest,
            },
            "checklist": checklist,
        },
        "audit": audit_payload,
    }

    # Activation ordering is the safety property: digest-unique media first,
    # its private accepted review second, and the public manifest last as the
    # sole visibility commit point.  Every pre-commit crash leaves the prior
    # accepted manifest and bytes intact; a post-commit crash leaves a complete
    # new bundle and only a stale private pending pointer to clean up.
    _activate_video(
        bundle_dir=bundle_dir,
        bundle_fd=bundle_fd,
        staged_video_relpath=staged_video_relpath,
        final_video_relpath=video_relpath,
        video_sha256=video_sha256,
        video_size_bytes=video_size_bytes,
    )
    _activation_failpoint("after_final_video")

    accepted_sidecar_body = (
        json.dumps(accepted, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_bundle_bytes_atomic(
        bundle_fd,
        accepted_sidecar_relpath,
        accepted_sidecar_body,
        mode=0o600,
        required_prefix=".magicfit-deliveries",
        directory_mode=0o700,
    )
    accepted_sidecar_bytes = _read_bundle_file_bytes(
        bundle_fd,
        accepted_sidecar_relpath,
        error="magicfit_acceptance_subject_changed",
        maximum_bytes=8 * 1024 * 1024,
    )
    _activation_failpoint("after_sidecar")

    committed_now = False
    current_manifest_sha256 = _sha256_bytes(
        _read_bundle_file_bytes(
            bundle_fd,
            "tour.json",
            error="magicfit_acceptance_manifest_changed",
            maximum_bytes=8 * 1024 * 1024,
        )
    )
    if current_manifest_sha256 == base_manifest_sha256:
        staged_manifest_snapshot = _hash_bundle_file(
            bundle_fd,
            staged_manifest_relpath,
            error="magicfit_acceptance_staged_manifest_changed",
            maximum_bytes=8 * 1024 * 1024,
        )
        if staged_manifest_snapshot.sha256 != staged_manifest_sha256:
            raise SystemExit("magicfit_acceptance_staged_manifest_changed")
        _confirm_named_bundle_identity(bundle_dir, bundle_fd)
        _write_bundle_bytes_atomic(
            bundle_fd,
            "tour.json",
            candidate_manifest_bytes,
            mode=0o644,
        )
        committed_now = True
    elif current_manifest_sha256 != staged_manifest_sha256:
        raise SystemExit("magicfit_acceptance_manifest_changed")

    _activation_failpoint("after_manifest")

    try:
        final_video_snapshot = _hash_bundle_file(
            bundle_fd,
            video_relpath,
            error="magicfit_acceptance_subject_changed",
            maximum_bytes=2 * 1024 * 1024 * 1024,
        )
        if (
            _read_bundle_file_bytes(
                bundle_fd,
                "tour.json",
                error="magicfit_acceptance_subject_changed",
                maximum_bytes=8 * 1024 * 1024,
            )
            != candidate_manifest_bytes
            or final_video_snapshot.sha256 != video_sha256
            or final_video_snapshot.size_bytes != video_size_bytes
            or _read_bundle_file_bytes(
                bundle_fd,
                accepted_sidecar_relpath,
                error="magicfit_acceptance_subject_changed",
                maximum_bytes=8 * 1024 * 1024,
            )
            != accepted_sidecar_bytes
        ):
            raise SystemExit("magicfit_acceptance_subject_changed")
        for name in AUDIT_ARTIFACT_NAMES:
            if _read_bundle_file_bytes(
                bundle_fd,
                audit_paths[name],
                error="magicfit_acceptance_audit_artifact_changed",
                maximum_bytes=max(len(audit_bodies[name]), 1),
            ) != audit_bodies[name]:
                raise SystemExit("magicfit_acceptance_audit_artifact_changed")
    except BaseException:
        if committed_now:
            _write_bundle_bytes_atomic(
                bundle_fd,
                "tour.json",
                base_manifest_bytes,
                mode=0o644,
            )
            _write_bundle_bytes_atomic(
                bundle_fd,
                staged_manifest_relpath,
                candidate_manifest_bytes,
                mode=0o600,
                required_prefix=".magicfit-staging",
                directory_mode=0o700,
            )
        raise

    if _bundle_entry_exists(
        bundle_fd,
        staged_video_relpath,
        error="magicfit_acceptance_staged_video_changed",
    ):
        staged_video_snapshot = _hash_bundle_file(
            bundle_fd,
            staged_video_relpath,
            error="magicfit_acceptance_staged_video_changed",
            maximum_bytes=2 * 1024 * 1024 * 1024,
        )
        if (
            staged_video_snapshot.sha256 != video_sha256
            or staged_video_snapshot.size_bytes != video_size_bytes
        ):
            raise SystemExit("magicfit_acceptance_staged_video_changed")
        _unlink_bundle_entry(
            bundle_fd,
            staged_video_relpath,
            error="magicfit_acceptance_staged_video_changed",
        )
    if _bundle_entry_exists(
        bundle_fd,
        staged_manifest_relpath,
        error="magicfit_acceptance_staged_manifest_changed",
    ):
        if _read_bundle_file_bytes(
            bundle_fd,
            staged_manifest_relpath,
            error="magicfit_acceptance_staged_manifest_changed",
            maximum_bytes=8 * 1024 * 1024,
        ) != candidate_manifest_bytes:
            raise SystemExit("magicfit_acceptance_staged_manifest_changed")
        _unlink_bundle_entry(
            bundle_fd,
            staged_manifest_relpath,
            error="magicfit_acceptance_staged_manifest_changed",
        )

    if _read_bundle_file_bytes(
        bundle_fd,
        PENDING_POINTER_RELPATH,
        error="magicfit_acceptance_pending_contract_changed",
        maximum_bytes=max(len(pending_bytes), 1),
    ) == pending_bytes:
        _unlink_bundle_entry(
            bundle_fd,
            PENDING_POINTER_RELPATH,
            error="magicfit_acceptance_pending_contract_changed",
        )
    _activation_failpoint("after_pending_unlink")
    _remove_empty_stage_at(bundle_fd, delivery_digest)
    _activation_failpoint("after_stage_cleanup")

    print(
        json.dumps(
            {
                "status": "delivery_accepted",
                "slug": slug,
                "video_relpath": video_relpath,
                "video_sha256": video_sha256,
                "source_receipt_sha256": source_receipt_sha256,
                "evidence_sha256": evidence_sha256,
                "visual_review_sha256": visual_review_sha256,
                "reviewer_authority_sha256": authority_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Accept an exact staged MagicFit delivery using closed local evidence."
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--source-receipt", required=True)
    parser.add_argument("--contact-sheet", required=True)
    parser.add_argument(
        "--review-bundle",
        help=(
            "Committed digest-named private-review receipt bundle containing "
            "the closed browser/evidence pair."
        ),
    )
    parser.add_argument(
        "--browser-receipt",
        help="Legacy loose input; always forbidden.",
    )
    parser.add_argument(
        "--evidence-receipt",
        help="Legacy loose input; always forbidden.",
    )
    parser.add_argument("--visual-review", required=True)
    parser.add_argument(
        "--reviewer-authority",
        required=True,
        help=(
            "Private detached Ed25519 reviewer authorization JSON bound to "
            "the exact delivery and review-evidence digests."
        ),
    )
    args = parser.parse_args()

    if str(args.browser_receipt or "").strip() or str(
        args.evidence_receipt or ""
    ).strip():
        raise SystemExit("magicfit_acceptance_legacy_loose_receipts_forbidden")
    if not str(args.review_bundle or "").strip():
        raise SystemExit("magicfit_acceptance_review_receipt_bundle_missing")

    slug = _canonical_relpath(args.slug)
    if not slug or "/" in slug:
        raise SystemExit("magicfit_acceptance_slug_invalid")
    root = Path(
        os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours"
    ).expanduser().resolve()
    bundle_dir = root / slug
    try:
        with property_tour_publication_lock(public_dir=root, slug=slug):
            with _activation_lock(bundle_dir) as bundle_fd:
                return _accept_locked(
                    args=args,
                    slug=slug,
                    bundle_dir=bundle_dir,
                    bundle_fd=bundle_fd,
                )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
