#!/usr/bin/env python3
"""Descriptor-bound, no-follow reads for MagicFit integrity subjects."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping

try:
    from property_magicfit_delivery_contract import (
        REVIEW_RECEIPT_BUNDLE_ARTIFACT_FIELDS,
        REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES,
        REVIEW_RECEIPT_BUNDLE_CONTRACT,
        REVIEW_RECEIPT_BUNDLE_MANIFEST_FIELDS,
        REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
        canonical_json_bytes,
        strict_json_object_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_magicfit_delivery_contract import (
        REVIEW_RECEIPT_BUNDLE_ARTIFACT_FIELDS,
        REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES,
        REVIEW_RECEIPT_BUNDLE_CONTRACT,
        REVIEW_RECEIPT_BUNDLE_MANIFEST_FIELDS,
        REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
        canonical_json_bytes,
        strict_json_object_bytes,
    )


class MagicFitSecureIOError(RuntimeError):
    """A stable fail-closed read error with the originating errno, if any."""

    def __init__(
        self,
        reason: str,
        *,
        detail: str = "",
        error_number: int | None = None,
    ) -> None:
        self.reason = str(reason)
        self.detail = str(detail)
        self.error_number = error_number
        super().__init__(
            self.reason if not self.detail else f"{self.reason}:{self.detail}"
        )

    @property
    def missing(self) -> bool:
        return self.error_number == errno.ENOENT


@dataclass(frozen=True)
class StableFileSnapshot:
    """One stable regular-file view read and hashed through a single fd."""

    body: bytes | None
    sha256: str
    size_bytes: int
    identity: tuple[int, int, int, int, int, int, int]
    prefix: bytes = b""


@dataclass(frozen=True)
class MagicFitReviewReceiptBundle:
    """One closed, digest-bound private-review publication."""

    path: Path
    delivery_digest: str
    manifest: Mapping[str, object]
    manifest_bytes: bytes
    browser_receipt_bytes: bytes
    evidence_receipt_bytes: bytes


MAGICFIT_STAGING_ROOT_NAME = ".magicfit-staging"
MAGICFIT_STAGE_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
MAGICFIT_STAGE_VIDEO_NAMES = frozenset(
    {"video.mp4", "video.m4v", "video.mov", "video.webm"}
)
MAGICFIT_STAGE_STABLE_NAMES = frozenset(
    {"tour.json", *MAGICFIT_STAGE_VIDEO_NAMES}
)
MAGICFIT_STAGE_TEMP_NAMES = frozenset({".tour.json.tmp", ".video.tmp"})
MAGICFIT_STAGE_LAYOUT_LIMIT = 8
MAGICFIT_STAGE_ORPHAN_SCAN_LIMIT = 128
MAGICFIT_STAGE_ORPHAN_REMOVE_LIMIT = 8


def lexical_absolute_path(path: str | os.PathLike[str] | Path) -> Path:
    """Expand a user path without resolving away a symlink component."""

    candidate = Path(path).expanduser()
    if any(part == ".." for part in candidate.parts):
        raise MagicFitSecureIOError("magicfit_secure_path_invalid")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _open_componentwise_no_follow(path: Path, *, reason: str) -> int:
    absolute = lexical_absolute_path(path)
    parts = absolute.parts
    if len(parts) < 2 or parts[0] != absolute.anchor or not hasattr(os, "O_NOFOLLOW"):
        raise MagicFitSecureIOError(reason, detail="path_invalid")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    try:
        current_fd = os.open(absolute.anchor, directory_flags)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        for component in parts[1:-1]:
            if component in {"", ".", ".."}:
                raise MagicFitSecureIOError(reason, detail="path_invalid")
            try:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        final_component = parts[-1]
        if final_component in {"", ".", ".."}:
            raise MagicFitSecureIOError(reason, detail="path_invalid")
        try:
            return os.open(final_component, file_flags, dir_fd=current_fd)
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc
    finally:
        os.close(current_fd)


def _open_directory_componentwise_no_follow(path: Path, *, reason: str) -> int:
    absolute = lexical_absolute_path(path)
    parts = absolute.parts
    if len(parts) < 1 or parts[0] != absolute.anchor or not hasattr(os, "O_NOFOLLOW"):
        raise MagicFitSecureIOError(reason, detail="path_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        current_fd = os.open(absolute.anchor, flags)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason, detail=type(exc).__name__, error_number=exc.errno
        ) from exc
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise MagicFitSecureIOError(reason, detail="path_invalid")
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _stage_digest(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or MAGICFIT_STAGE_DIGEST_RE.fullmatch(value) is None:
        raise MagicFitSecureIOError(reason, detail="digest_invalid")
    return value


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def open_directory_componentwise_no_follow(path: Path, *, reason: str) -> int:
    """Public wrapper for one securely opened directory descriptor."""

    return _open_directory_componentwise_no_follow(path, reason=reason)


def _relative_parts(relpath: str, *, reason: str) -> tuple[str, ...]:
    if (
        not isinstance(relpath, str)
        or not relpath
        or relpath != relpath.strip()
        or relpath.startswith("/")
        or "\\" in relpath
    ):
        raise MagicFitSecureIOError(reason, detail="path_invalid")
    parts = PurePosixPath(relpath).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise MagicFitSecureIOError(reason, detail="path_invalid")
    if PurePosixPath(*parts).as_posix() != relpath:
        raise MagicFitSecureIOError(reason, detail="path_invalid")
    return parts


def _open_relative_parent(
    directory_fd: int,
    relpath: str,
    *,
    reason: str,
) -> tuple[int, str]:
    parts = _relative_parts(relpath, reason=reason)
    try:
        current_fd = os.dup(directory_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason, detail=type(exc).__name__, error_number=exc.errno
        ) from exc
    try:
        current_details = os.fstat(current_fd)
        if not stat.S_ISDIR(current_details.st_mode):
            raise MagicFitSecureIOError(reason, detail="parent_invalid")
        for component in parts[:-1]:
            try:
                next_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        result = current_fd
        current_fd = -1
        return result, parts[-1]
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def open_regular_file_at(
    directory_fd: int,
    relpath: str,
    *,
    reason: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> int:
    """Open one bounded regular file relative to a held directory fd."""

    maximum = int(maximum_bytes)
    if maximum < 0:
        raise MagicFitSecureIOError(reason, detail="limit_invalid")
    parent_fd, name = _open_relative_parent(
        directory_fd, relpath, reason=reason
    )
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc
        details = os.fstat(descriptor)
        size = int(details.st_size)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or size < 0
            or (size == 0 and not allow_empty)
            or size > maximum
        ):
            raise MagicFitSecureIOError(reason, detail="invalid_type_or_bounds")
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def read_stable_bounded_file_at(
    directory_fd: int,
    relpath: str,
    *,
    reason: str,
    maximum_bytes: int,
    allow_empty: bool = False,
    capture_body: bool = True,
    copy_to_fd: int | None = None,
    prefix_bytes: int = 0,
) -> StableFileSnapshot:
    """Read/hash one file through a descriptor-relative no-follow open."""

    prefix_limit = int(prefix_bytes)
    if prefix_limit < 0:
        raise MagicFitSecureIOError(reason, detail="limit_invalid")
    descriptor = open_regular_file_at(
        directory_fd,
        relpath,
        reason=reason,
        maximum_bytes=maximum_bytes,
        allow_empty=allow_empty,
    )
    try:
        before = os.fstat(descriptor)
        size = int(before.st_size)
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture_body else None
        prefix = bytearray()
        remaining = size
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if not chunk:
                raise MagicFitSecureIOError(reason, detail="short_read")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
            if len(prefix) < prefix_limit:
                prefix.extend(chunk[: prefix_limit - len(prefix)])
            if copy_to_fd is not None:
                view = memoryview(chunk)
                while view:
                    try:
                        written = os.write(copy_to_fd, view)
                    except OSError as exc:
                        raise MagicFitSecureIOError(
                            reason,
                            detail=f"copy_{type(exc).__name__}",
                            error_number=exc.errno,
                        ) from exc
                    if written <= 0:
                        raise MagicFitSecureIOError(
                            reason, detail="copy_short_write"
                        )
                    view = view[written:]
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MagicFitSecureIOError(reason, detail="grew_during_read")
        after = os.fstat(descriptor)
        before_identity = _identity(before)
        if before_identity != _identity(after):
            raise MagicFitSecureIOError(reason, detail="changed_during_read")
        return StableFileSnapshot(
            body=b"".join(chunks) if chunks is not None else None,
            sha256=digest.hexdigest(),
            size_bytes=size,
            identity=before_identity,
            prefix=bytes(prefix),
        )
    finally:
        os.close(descriptor)


def read_stable_bounded_bytes_at(
    directory_fd: int,
    relpath: str,
    *,
    reason: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> StableFileSnapshot:
    snapshot = read_stable_bounded_file_at(
        directory_fd,
        relpath,
        reason=reason,
        maximum_bytes=maximum_bytes,
        allow_empty=allow_empty,
        capture_body=True,
    )
    assert snapshot.body is not None
    return snapshot


def hash_stable_bounded_file_at(
    directory_fd: int,
    relpath: str,
    *,
    reason: str,
    maximum_bytes: int,
    prefix_bytes: int = 0,
) -> StableFileSnapshot:
    return read_stable_bounded_file_at(
        directory_fd,
        relpath,
        reason=reason,
        maximum_bytes=maximum_bytes,
        capture_body=False,
        prefix_bytes=prefix_bytes,
    )


def write_regular_file_atomic_at(
    directory_fd: int,
    relpath: str,
    body: bytes,
    *,
    reason: str,
    mode: int,
) -> None:
    """Durably replace one regular file relative to a held directory fd."""

    if not isinstance(body, bytes) or not body:
        raise MagicFitSecureIOError(reason, detail="body_invalid")
    parent_fd, name = _open_relative_parent(
        directory_fd, relpath, reason=reason
    )
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    temporary_created = False
    replaced = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(
                temporary, flags, mode, dir_fd=parent_fd
            )
            temporary_created = True
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc
        view = memoryview(body)
        while view:
            try:
                written = os.write(descriptor, view)
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if written <= 0:
                raise MagicFitSecureIOError(reason, detail="short_write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        temporary_details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_details.st_mode)
            or temporary_details.st_nlink != 1
            or stat.S_IMODE(temporary_details.st_mode) != mode
            or temporary_details.st_size != len(body)
        ):
            raise MagicFitSecureIOError(reason, detail="temporary_invalid")
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        replaced = True
        target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(target.st_mode)
            or target.st_nlink != 1
            or stat.S_IMODE(target.st_mode) != mode
            or target.st_size != len(body)
            or (target.st_dev, target.st_ino)
            != (temporary_details.st_dev, temporary_details.st_ino)
        ):
            raise MagicFitSecureIOError(reason, detail="commit_invalid")
        os.fsync(parent_fd)
    except MagicFitSecureIOError:
        raise
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason, detail=type(exc).__name__, error_number=exc.errno
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_created and not replaced:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent_fd)
        os.close(parent_fd)


def _open_magicfit_staging_root_at(
    bundle_fd: int, *, reason: str, create: bool
) -> int:
    try:
        if create:
            try:
                os.mkdir(MAGICFIT_STAGING_ROOT_NAME, 0o700, dir_fd=bundle_fd)
                os.fsync(bundle_fd)
            except FileExistsError:
                pass
        try:
            root_fd = os.open(
                MAGICFIT_STAGING_ROOT_NAME,
                _directory_flags(),
                dir_fd=bundle_fd,
            )
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc
    except MagicFitSecureIOError:
        raise
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MagicFitSecureIOError(reason, detail="root_invalid")
        os.fchmod(root_fd, 0o700)
    except MagicFitSecureIOError:
        os.close(root_fd)
        raise
    except OSError as exc:
        os.close(root_fd)
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    return root_fd


def _open_magicfit_staging_root(
    bundle_dir: Path, *, reason: str, create: bool
) -> int:
    bundle_fd = _open_directory_componentwise_no_follow(bundle_dir, reason=reason)
    try:
        return _open_magicfit_staging_root_at(
            bundle_fd, reason=reason, create=create
        )
    finally:
        os.close(bundle_fd)


def _open_magicfit_stage(
    root_fd: int, digest: str, *, reason: str
) -> int:
    normalized = _stage_digest(digest, reason=reason)
    try:
        stage_fd = os.open(normalized, _directory_flags(), dir_fd=root_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        metadata = os.fstat(stage_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MagicFitSecureIOError(reason, detail="stage_invalid")
        os.fchmod(stage_fd, 0o700)
    except MagicFitSecureIOError:
        os.close(stage_fd)
        raise
    except OSError as exc:
        os.close(stage_fd)
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    return stage_fd


def _magicfit_stage_entries(
    stage_fd: int, *, allow_temporary: bool
) -> list[str] | None:
    allowed = set(MAGICFIT_STAGE_STABLE_NAMES)
    if allow_temporary:
        allowed.update(MAGICFIT_STAGE_TEMP_NAMES)
    names: list[str] = []
    try:
        with os.scandir(stage_fd) as rows:
            for index, row in enumerate(rows):
                if index >= MAGICFIT_STAGE_LAYOUT_LIMIT:
                    return None
                name = row.name
                if name not in allowed:
                    return None
                try:
                    metadata = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                except OSError:
                    return None
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    return None
                names.append(name)
    except OSError:
        return None
    stable_videos = [name for name in names if name in MAGICFIT_STAGE_VIDEO_NAMES]
    if len(stable_videos) > 1 or len(names) != len(set(names)):
        return None
    return names


def create_magicfit_stage_directory_at(bundle_fd: int, digest: str) -> bool:
    """Create/open a lowercase digest stage without following path aliases."""

    reason = "magicfit_staging_path_invalid"
    normalized = _stage_digest(digest, reason=reason)
    root_fd = _open_magicfit_staging_root_at(
        bundle_fd, reason=reason, create=True
    )
    created = False
    try:
        try:
            try:
                os.mkdir(normalized, 0o700, dir_fd=root_fd)
                created = True
                os.fsync(root_fd)
            except FileExistsError:
                pass
            stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
            try:
                if _magicfit_stage_entries(stage_fd, allow_temporary=False) is None:
                    raise MagicFitSecureIOError(reason, detail="layout_invalid")
            finally:
                os.close(stage_fd)
        except BaseException as original:
            if created:
                try:
                    os.rmdir(normalized, dir_fd=root_fd)
                    os.fsync(root_fd)
                except OSError:
                    # Never recurse through an entry that appeared in a stage
                    # we could not finish validating.
                    pass
            if isinstance(original, MagicFitSecureIOError):
                raise
            if not isinstance(original, OSError):
                raise
            raise MagicFitSecureIOError(
                reason,
                detail=type(original).__name__,
                error_number=original.errno,
            ) from original
    finally:
        os.close(root_fd)
    return created


def create_magicfit_stage_directory(bundle_dir: Path, digest: str) -> bool:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staging_path_invalid"
    )
    try:
        return create_magicfit_stage_directory_at(bundle_fd, digest)
    finally:
        os.close(bundle_fd)


def _stage_target_path(bundle_dir: Path, digest: str, name: str) -> Path:
    return bundle_dir / MAGICFIT_STAGING_ROOT_NAME / digest / name


def _open_stage_temporary(stage_fd: int, name: str, *, reason: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(name, flags, 0o600, dir_fd=stage_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc


def _publish_stage_temporary(
    stage_fd: int, temporary_name: str, target_name: str, *, reason: str
) -> None:
    try:
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=stage_fd,
            dst_dir_fd=stage_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=stage_fd)
        os.fsync(stage_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc


def write_magicfit_stage_bytes_at(
    bundle_fd: int,
    digest: str,
    *,
    name: str,
    body: bytes,
    maximum_bytes: int,
) -> None:
    reason = "magicfit_staged_manifest_conflict"
    normalized = _stage_digest(digest, reason=reason)
    if name != "tour.json" or not isinstance(body, bytes) or not body:
        raise MagicFitSecureIOError(reason, detail="subject_invalid")
    if len(body) > int(maximum_bytes):
        raise MagicFitSecureIOError(reason, detail="too_large")
    root_fd = _open_magicfit_staging_root_at(
        bundle_fd, reason=reason, create=False
    )
    try:
        stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
        try:
            entries = _magicfit_stage_entries(stage_fd, allow_temporary=False)
            if entries is None:
                raise MagicFitSecureIOError(reason, detail="layout_invalid")
            if name in entries:
                existing = read_stable_bounded_bytes_at(
                    stage_fd,
                    name,
                    reason=reason,
                    maximum_bytes=int(maximum_bytes),
                )
                if existing.body != body:
                    raise MagicFitSecureIOError(reason, detail="content_mismatch")
                return
            temporary = ".tour.json.tmp"
            descriptor = _open_stage_temporary(stage_fd, temporary, reason=reason)
            try:
                view = memoryview(body)
                while view:
                    try:
                        written = os.write(descriptor, view)
                    except OSError as exc:
                        raise MagicFitSecureIOError(
                            reason,
                            detail=type(exc).__name__,
                            error_number=exc.errno,
                        ) from exc
                    if written <= 0:
                        raise MagicFitSecureIOError(reason, detail="short_write")
                    view = view[written:]
                try:
                    os.fsync(descriptor)
                    os.fchmod(descriptor, 0o600)
                except OSError as exc:
                    raise MagicFitSecureIOError(
                        reason,
                        detail=type(exc).__name__,
                        error_number=exc.errno,
                    ) from exc
            finally:
                os.close(descriptor)
            _publish_stage_temporary(stage_fd, temporary, name, reason=reason)
        except BaseException:
            try:
                os.unlink(".tour.json.tmp", dir_fd=stage_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(stage_fd)
    finally:
        os.close(root_fd)


def write_magicfit_stage_bytes(
    bundle_dir: Path,
    digest: str,
    *,
    name: str,
    body: bytes,
    maximum_bytes: int,
) -> None:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staged_manifest_conflict"
    )
    try:
        write_magicfit_stage_bytes_at(
            bundle_fd,
            digest,
            name=name,
            body=body,
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(bundle_fd)


def copy_magicfit_stage_video_at(
    source: Path,
    bundle_fd: int,
    digest: str,
    *,
    name: str,
    expected_sha256: str,
    maximum_bytes: int,
) -> StableFileSnapshot:
    reason = "magicfit_staged_video_conflict"
    normalized = _stage_digest(digest, reason=reason)
    if name not in MAGICFIT_STAGE_VIDEO_NAMES or MAGICFIT_STAGE_DIGEST_RE.fullmatch(
        expected_sha256
    ) is None:
        raise MagicFitSecureIOError(reason, detail="subject_invalid")
    root_fd = _open_magicfit_staging_root_at(
        bundle_fd, reason=reason, create=False
    )
    try:
        stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
        try:
            entries = _magicfit_stage_entries(stage_fd, allow_temporary=False)
            if entries is None or any(
                entry in MAGICFIT_STAGE_VIDEO_NAMES and entry != name
                for entry in entries or []
            ):
                raise MagicFitSecureIOError(reason, detail="layout_invalid")
            if name in entries:
                existing = hash_stable_bounded_file_at(
                    stage_fd,
                    name,
                    reason=reason,
                    maximum_bytes=int(maximum_bytes),
                )
                if existing.sha256 != expected_sha256:
                    raise MagicFitSecureIOError(reason, detail="content_mismatch")
                return existing
            descriptor = _open_stage_temporary(
                stage_fd, ".video.tmp", reason=reason
            )
            try:
                copied = read_stable_bounded_file(
                    source,
                    reason="magicfit_video",
                    maximum_bytes=int(maximum_bytes),
                    capture_body=False,
                    copy_to_fd=descriptor,
                )
                if copied.sha256 != expected_sha256:
                    raise MagicFitSecureIOError(
                        reason, detail="digest_mismatch"
                    )
                try:
                    os.fsync(descriptor)
                    os.fchmod(descriptor, 0o600)
                except OSError as exc:
                    raise MagicFitSecureIOError(
                        reason,
                        detail=type(exc).__name__,
                        error_number=exc.errno,
                    ) from exc
            finally:
                os.close(descriptor)
            _publish_stage_temporary(stage_fd, ".video.tmp", name, reason=reason)
            return copied
        except BaseException:
            try:
                os.unlink(".video.tmp", dir_fd=stage_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(stage_fd)
    finally:
        os.close(root_fd)


def copy_magicfit_stage_video(
    source: Path,
    bundle_dir: Path,
    digest: str,
    *,
    name: str,
    expected_sha256: str,
    maximum_bytes: int,
) -> StableFileSnapshot:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staged_video_conflict"
    )
    try:
        return copy_magicfit_stage_video_at(
            source,
            bundle_fd,
            digest,
            name=name,
            expected_sha256=expected_sha256,
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(bundle_fd)


def require_complete_magicfit_stage_at(
    bundle_fd: int, digest: str, *, video_name: str
) -> None:
    reason = "magicfit_staging_layout_invalid"
    normalized = _stage_digest(digest, reason=reason)
    if video_name not in MAGICFIT_STAGE_VIDEO_NAMES:
        raise MagicFitSecureIOError(reason, detail="video_name_invalid")
    root_fd = _open_magicfit_staging_root_at(
        bundle_fd, reason=reason, create=False
    )
    try:
        stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
        try:
            entries = _magicfit_stage_entries(stage_fd, allow_temporary=False)
            if entries is None or set(entries) != {"tour.json", video_name}:
                raise MagicFitSecureIOError(reason, detail="layout_invalid")
        finally:
            os.close(stage_fd)
    finally:
        os.close(root_fd)


def require_complete_magicfit_stage(
    bundle_dir: Path, digest: str, *, video_name: str
) -> None:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staging_layout_invalid"
    )
    try:
        require_complete_magicfit_stage_at(
            bundle_fd, digest, video_name=video_name
        )
    finally:
        os.close(bundle_fd)


def remove_closed_magicfit_stage_at(bundle_fd: int, digest: str) -> bool:
    """Remove only the recognized regular-file layout; never recurse."""

    reason = "magicfit_staging_cleanup_invalid"
    normalized = _stage_digest(digest, reason=reason)
    try:
        root_fd = _open_magicfit_staging_root_at(
            bundle_fd, reason=reason, create=False
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            return False
        raise
    try:
        try:
            stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
        except MagicFitSecureIOError as exc:
            if exc.missing:
                return False
            raise
        try:
            entries = _magicfit_stage_entries(stage_fd, allow_temporary=True)
            if entries is None:
                return False
            for name in entries:
                os.unlink(name, dir_fd=stage_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        try:
            os.rmdir(normalized, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        os.fsync(root_fd)
        return True
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    finally:
        os.close(root_fd)


def remove_closed_magicfit_stage(bundle_dir: Path, digest: str) -> bool:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staging_cleanup_invalid"
    )
    try:
        return remove_closed_magicfit_stage_at(bundle_fd, digest)
    finally:
        os.close(bundle_fd)


def remove_empty_magicfit_stage(bundle_dir: Path, digest: str) -> bool:
    reason = "magicfit_staging_cleanup_invalid"
    normalized = _stage_digest(digest, reason=reason)
    try:
        root_fd = _open_magicfit_staging_root(
            bundle_dir, reason=reason, create=False
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            return False
        raise
    try:
        try:
            stage_fd = _open_magicfit_stage(root_fd, normalized, reason=reason)
        except MagicFitSecureIOError as exc:
            if exc.missing:
                return False
            raise
        try:
            with os.scandir(stage_fd) as rows:
                if next(rows, None) is not None:
                    return False
        finally:
            os.close(stage_fd)
        try:
            os.rmdir(normalized, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        os.fsync(root_fd)
        return True
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    finally:
        os.close(root_fd)


def collect_bounded_magicfit_stage_orphans_at(
    bundle_fd: int,
    *,
    protected_digests: set[str] | frozenset[str],
    scan_limit: int = MAGICFIT_STAGE_ORPHAN_SCAN_LIMIT,
    removal_limit: int = MAGICFIT_STAGE_ORPHAN_REMOVE_LIMIT,
) -> int:
    """Collect a bounded number of closed-layout orphans, ignoring unknowns."""

    reason = "magicfit_staging_collection_invalid"
    protected = {
        _stage_digest(value, reason=reason) for value in protected_digests
    }
    bounded_scan = max(1, min(int(scan_limit), 1024))
    bounded_remove = max(0, min(int(removal_limit), 64))
    try:
        root_fd = _open_magicfit_staging_root_at(
            bundle_fd, reason=reason, create=False
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            return 0
        raise
    candidates: list[str] = []
    try:
        with os.scandir(root_fd) as rows:
            for index, row in enumerate(rows):
                if index >= bounded_scan:
                    break
                name = row.name
                if (
                    name not in protected
                    and MAGICFIT_STAGE_DIGEST_RE.fullmatch(name) is not None
                    and row.is_dir(follow_symlinks=False)
                ):
                    candidates.append(name)
    finally:
        os.close(root_fd)
    removed = 0
    for digest in sorted(candidates):
        if removed >= bounded_remove:
            break
        if remove_closed_magicfit_stage_at(bundle_fd, digest):
            removed += 1
    return removed


def collect_bounded_magicfit_stage_orphans(
    bundle_dir: Path,
    *,
    protected_digests: set[str] | frozenset[str],
    scan_limit: int = MAGICFIT_STAGE_ORPHAN_SCAN_LIMIT,
    removal_limit: int = MAGICFIT_STAGE_ORPHAN_REMOVE_LIMIT,
) -> int:
    bundle_fd = _open_directory_componentwise_no_follow(
        bundle_dir, reason="magicfit_staging_collection_invalid"
    )
    try:
        return collect_bounded_magicfit_stage_orphans_at(
            bundle_fd,
            protected_digests=protected_digests,
            scan_limit=scan_limit,
            removal_limit=removal_limit,
        )
    finally:
        os.close(bundle_fd)


def _identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_nlink),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
    )


def stat_regular_file_identity(
    path: str | os.PathLike[str] | Path,
    *,
    reason: str,
) -> tuple[int, int, int, int, int, int, int]:
    """Re-stat one regular file through a component-wise no-follow open."""

    descriptor = _open_componentwise_no_follow(
        lexical_absolute_path(path), reason=reason
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise MagicFitSecureIOError(reason, detail="invalid_type")
        return _identity(details)
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc


def read_stable_bounded_prefix(
    path: str | os.PathLike[str] | Path,
    *,
    reason: str,
    maximum_bytes: int,
    prefix_bytes: int = 64,
    allow_empty: bool = False,
) -> StableFileSnapshot:
    """Read at most 64 prefix bytes and bind them to one stable fd identity.

    This is for format sniffing only.  It intentionally does not hash the full
    file; integrity callers must use :func:`hash_stable_bounded_file`.
    """

    maximum = int(maximum_bytes)
    prefix_limit = int(prefix_bytes)
    if maximum < 0 or prefix_limit < 0 or prefix_limit > 64:
        raise MagicFitSecureIOError(reason, detail="limit_invalid")
    descriptor = _open_componentwise_no_follow(
        lexical_absolute_path(path), reason=reason
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MagicFitSecureIOError(reason, detail="invalid_type")
        size = int(before.st_size)
        if size < 0 or (size == 0 and not allow_empty):
            raise MagicFitSecureIOError(reason, detail="empty")
        if size > maximum:
            raise MagicFitSecureIOError(reason, detail="too_large")

        prefix = bytearray()
        target = min(size, prefix_limit)
        while len(prefix) < target:
            try:
                chunk = os.read(descriptor, target - len(prefix))
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if not chunk:
                raise MagicFitSecureIOError(reason, detail="short_read")
            prefix.extend(chunk)
        after = os.fstat(descriptor)
        before_identity = _identity(before)
        if before_identity != _identity(after):
            raise MagicFitSecureIOError(reason, detail="changed_during_read")
        return StableFileSnapshot(
            body=None,
            sha256="",
            size_bytes=size,
            identity=before_identity,
            prefix=bytes(prefix),
        )
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            raise MagicFitSecureIOError(
                reason,
                detail=type(exc).__name__,
                error_number=exc.errno,
            ) from exc


def read_stable_bounded_file(
    path: str | os.PathLike[str] | Path,
    *,
    reason: str,
    maximum_bytes: int,
    allow_empty: bool = False,
    capture_body: bool = True,
    copy_to_fd: int | None = None,
    prefix_bytes: int = 0,
) -> StableFileSnapshot:
    """Read/hash one regular file without following any path component.

    The same descriptor supplies the size check, bytes, digest, optional copy,
    EOF check, and final identity check.  A path replacement after open cannot
    redirect any later part of this read to different bytes.
    """

    maximum = int(maximum_bytes)
    prefix_limit = int(prefix_bytes)
    if maximum < 0 or prefix_limit < 0:
        raise MagicFitSecureIOError(reason, detail="limit_invalid")
    descriptor = _open_componentwise_no_follow(
        lexical_absolute_path(path), reason=reason
    )
    close_error: OSError | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MagicFitSecureIOError(reason, detail="invalid_type")
        size = int(before.st_size)
        if size < 0 or (size == 0 and not allow_empty):
            raise MagicFitSecureIOError(reason, detail="empty")
        if size > maximum:
            raise MagicFitSecureIOError(reason, detail="too_large")

        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture_body else None
        prefix = bytearray()
        remaining = size
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if not chunk:
                raise MagicFitSecureIOError(reason, detail="short_read")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
            if len(prefix) < prefix_limit:
                prefix.extend(chunk[: prefix_limit - len(prefix)])
            if copy_to_fd is not None:
                view = memoryview(chunk)
                while view:
                    try:
                        written = os.write(copy_to_fd, view)
                    except OSError as exc:
                        raise MagicFitSecureIOError(
                            reason,
                            detail=f"copy_{type(exc).__name__}",
                            error_number=exc.errno,
                        ) from exc
                    if written <= 0:
                        raise MagicFitSecureIOError(reason, detail="copy_short_write")
                    view = view[written:]
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MagicFitSecureIOError(reason, detail="grew_during_read")
        after = os.fstat(descriptor)
        before_identity = _identity(before)
        if before_identity != _identity(after):
            raise MagicFitSecureIOError(reason, detail="changed_during_read")
        return StableFileSnapshot(
            body=b"".join(chunks) if chunks is not None else None,
            sha256=digest.hexdigest(),
            size_bytes=size,
            identity=before_identity,
            prefix=bytes(prefix),
        )
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
        if close_error is not None:
            raise MagicFitSecureIOError(
                reason,
                detail=type(close_error).__name__,
                error_number=close_error.errno,
            ) from close_error


def read_stable_bounded_bytes(
    path: str | os.PathLike[str] | Path,
    *,
    reason: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> StableFileSnapshot:
    snapshot = read_stable_bounded_file(
        path,
        reason=reason,
        maximum_bytes=maximum_bytes,
        allow_empty=allow_empty,
        capture_body=True,
    )
    assert snapshot.body is not None
    return snapshot


def hash_stable_bounded_file(
    path: str | os.PathLike[str] | Path,
    *,
    reason: str,
    maximum_bytes: int,
    prefix_bytes: int = 0,
) -> StableFileSnapshot:
    return read_stable_bounded_file(
        path,
        reason=reason,
        maximum_bytes=maximum_bytes,
        capture_body=False,
        prefix_bytes=prefix_bytes,
    )


REVIEW_RECEIPT_BUNDLE_MANIFEST_MAX_BYTES = 64 * 1024
REVIEW_RECEIPT_BROWSER_MAX_BYTES = 64 * 1024
REVIEW_RECEIPT_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
_REVIEW_RECEIPT_TEMP_NAME = ".magicfit-review-bundle.tmp"
_REVIEW_RECEIPT_LOCK_NAME = ".magicfit-review-bundles.lock"
_RENAME_NOREPLACE = 1


def _review_receipt_digest(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or MAGICFIT_STAGE_DIGEST_RE.fullmatch(value) is None:
        raise MagicFitSecureIOError(reason, detail="delivery_digest_invalid")
    return value


def _review_receipt_root_fd(path: Path, *, reason: str) -> int:
    root_fd = _open_directory_componentwise_no_follow(path, reason=reason)
    try:
        details = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise MagicFitSecureIOError(reason, detail="parent_permissions_invalid")
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def _review_receipt_child_directory_fd(
    parent_fd: int, name: str, *, reason: str
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise MagicFitSecureIOError(reason, detail="directory_invalid")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _review_receipt_entries(
    directory_fd: int, *, reason: str, maximum_entries: int = 4
) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as rows:
            for index, row in enumerate(rows):
                if index >= maximum_entries:
                    raise MagicFitSecureIOError(reason, detail="layout_invalid")
                names.append(row.name)
    except MagicFitSecureIOError:
        raise
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    return names


def _read_review_receipt_at(
    directory_fd: int,
    filename: str,
    *,
    reason: str,
    maximum_bytes: int,
) -> StableFileSnapshot:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        before = os.fstat(descriptor)
        size = int(before.st_size)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or size <= 0
            or size > int(maximum_bytes)
        ):
            raise MagicFitSecureIOError(reason, detail="artifact_invalid")
        chunks: list[bytes] = []
        remaining = size
        digest = hashlib.sha256()
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if not chunk:
                raise MagicFitSecureIOError(reason, detail="short_read")
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MagicFitSecureIOError(reason, detail="grew_during_read")
        after = os.fstat(descriptor)
        identity = _identity(before)
        if identity != _identity(after):
            raise MagicFitSecureIOError(reason, detail="changed_during_read")
        return StableFileSnapshot(
            body=b"".join(chunks),
            sha256=digest.hexdigest(),
            size_bytes=size,
            identity=identity,
        )
    finally:
        os.close(descriptor)


def _load_review_receipt_bundle_at(
    root_fd: int,
    *,
    delivery_digest: str,
    path: Path,
    reason: str,
) -> MagicFitReviewReceiptBundle:
    directory_fd = _review_receipt_child_directory_fd(
        root_fd, delivery_digest, reason=reason
    )
    try:
        before = _identity(os.fstat(directory_fd))
        expected_names = {
            REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
            *REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES.values(),
        }
        if set(_review_receipt_entries(directory_fd, reason=reason)) != expected_names:
            raise MagicFitSecureIOError(reason, detail="layout_invalid")
        manifest_snapshot = _read_review_receipt_at(
            directory_fd,
            REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
            reason=reason,
            maximum_bytes=REVIEW_RECEIPT_BUNDLE_MANIFEST_MAX_BYTES,
        )
        assert manifest_snapshot.body is not None
        try:
            manifest = strict_json_object_bytes(
                manifest_snapshot.body, reason=reason
            )
        except ValueError as exc:
            raise MagicFitSecureIOError(reason, detail="manifest_invalid") from exc
        if (
            set(manifest) != REVIEW_RECEIPT_BUNDLE_MANIFEST_FIELDS
            or manifest.get("contract_name") != REVIEW_RECEIPT_BUNDLE_CONTRACT
            or manifest.get("delivery_digest") != delivery_digest
        ):
            raise MagicFitSecureIOError(reason, detail="manifest_invalid")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(
            REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES
        ):
            raise MagicFitSecureIOError(reason, detail="manifest_invalid")
        snapshots: dict[str, StableFileSnapshot] = {}
        limits = {
            "browser_receipt": REVIEW_RECEIPT_BROWSER_MAX_BYTES,
            "evidence_receipt": REVIEW_RECEIPT_EVIDENCE_MAX_BYTES,
        }
        for artifact_name, filename in REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES.items():
            entry = artifacts.get(artifact_name)
            if (
                not isinstance(entry, dict)
                or set(entry) != REVIEW_RECEIPT_BUNDLE_ARTIFACT_FIELDS
                or entry.get("filename") != filename
                or not isinstance(entry.get("sha256"), str)
                or MAGICFIT_STAGE_DIGEST_RE.fullmatch(str(entry.get("sha256")))
                is None
                or isinstance(entry.get("size_bytes"), bool)
                or not isinstance(entry.get("size_bytes"), int)
                or int(entry.get("size_bytes", 0)) <= 0
            ):
                raise MagicFitSecureIOError(reason, detail="manifest_invalid")
            snapshot = _read_review_receipt_at(
                directory_fd,
                filename,
                reason=reason,
                maximum_bytes=limits[artifact_name],
            )
            if (
                snapshot.sha256 != entry.get("sha256")
                or snapshot.size_bytes != entry.get("size_bytes")
            ):
                raise MagicFitSecureIOError(reason, detail="artifact_digest_mismatch")
            snapshots[artifact_name] = snapshot
        if before != _identity(os.fstat(directory_fd)):
            raise MagicFitSecureIOError(reason, detail="changed_during_read")
        browser_body = snapshots["browser_receipt"].body
        evidence_body = snapshots["evidence_receipt"].body
        assert browser_body is not None and evidence_body is not None
        return MagicFitReviewReceiptBundle(
            path=path,
            delivery_digest=delivery_digest,
            manifest=dict(manifest),
            manifest_bytes=manifest_snapshot.body,
            browser_receipt_bytes=browser_body,
            evidence_receipt_bytes=evidence_body,
        )
    finally:
        os.close(directory_fd)


def load_magicfit_review_receipt_bundle(
    path: str | os.PathLike[str] | Path,
    *,
    expected_delivery_digest: str,
    reason: str = "magicfit_review_receipt_bundle_invalid",
) -> MagicFitReviewReceiptBundle:
    """Safe-open and fully rehash one committed receipt bundle."""

    digest = _review_receipt_digest(expected_delivery_digest, reason=reason)
    absolute = lexical_absolute_path(path)
    if absolute.name != digest:
        raise MagicFitSecureIOError(reason, detail="path_digest_mismatch")
    root_fd = _review_receipt_root_fd(absolute.parent, reason=reason)
    try:
        return _load_review_receipt_bundle_at(
            root_fd,
            delivery_digest=digest,
            path=absolute,
            reason=reason,
        )
    finally:
        os.close(root_fd)


def _write_review_receipt_at(
    directory_fd: int, filename: str, body: bytes, *, reason: str
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        view = memoryview(body)
        while view:
            try:
                written = os.write(descriptor, view)
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            if written <= 0:
                raise MagicFitSecureIOError(reason, detail="short_write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _review_receipt_failpoint(name: str) -> None:
    if (
        os.getenv("PYTEST_CURRENT_TEST")
        and os.getenv("PROPERTYQUARRY_MAGICFIT_REVIEW_BUNDLE_FAILPOINT") == name
    ):
        os._exit(86)


def _cleanup_recognized_review_receipt_temp(
    root_fd: int, temporary_name: str, *, reason: str
) -> bool:
    try:
        temporary_fd = _review_receipt_child_directory_fd(
            root_fd, temporary_name, reason=reason
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            return False
        raise
    try:
        names = _review_receipt_entries(temporary_fd, reason=reason)
        allowed = {
            REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
            *REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES.values(),
        }
        if not set(names).issubset(allowed) or len(names) != len(set(names)):
            raise MagicFitSecureIOError(reason, detail="temporary_layout_invalid")
        for name in names:
            details = os.stat(name, dir_fd=temporary_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise MagicFitSecureIOError(
                    reason, detail="temporary_layout_invalid"
                )
        for name in names:
            os.unlink(name, dir_fd=temporary_fd)
        os.fsync(temporary_fd)
    except MagicFitSecureIOError:
        raise
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    finally:
        os.close(temporary_fd)
    try:
        os.rmdir(temporary_name, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    return True


def _rename_directory_noreplace(
    parent_fd: int, source_name: str, destination_name: str, *, reason: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise MagicFitSecureIOError(reason, detail="rename_noreplace_unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise MagicFitSecureIOError(
            reason,
            detail=f"rename_{errno.errorcode.get(error_number, 'error')}",
            error_number=error_number,
        )


@contextlib.contextmanager
def _review_receipt_digest_lock(
    root_fd: int, delivery_digest: str, *, reason: str
) -> Iterator[None]:
    # One constant root lock is a bounded, stronger serialization boundary than
    # one persistent lock inode per generated digest.  Keep the validated
    # digest parameter explicit so callers cannot enter publication unbound.
    _review_receipt_digest(delivery_digest, reason=reason)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(_REVIEW_RECEIPT_LOCK_NAME, flags, 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise MagicFitSecureIOError(
            reason,
            detail=type(exc).__name__,
            error_number=exc.errno,
        ) from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
        ):
            raise MagicFitSecureIOError(reason, detail="lock_invalid")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def publish_magicfit_review_receipt_bundle(
    root: str | os.PathLike[str] | Path,
    *,
    delivery_digest: str,
    browser_receipt_bytes: bytes,
    evidence_receipt_bytes: bytes,
    reason: str = "magicfit_private_review_receipt_bundle_invalid",
) -> MagicFitReviewReceiptBundle:
    """Crash-atomically publish or recover one exact full-review receipt bundle."""

    digest = _review_receipt_digest(delivery_digest, reason=reason)
    if (
        not isinstance(browser_receipt_bytes, bytes)
        or not browser_receipt_bytes
        or len(browser_receipt_bytes) > REVIEW_RECEIPT_BROWSER_MAX_BYTES
        or not isinstance(evidence_receipt_bytes, bytes)
        or not evidence_receipt_bytes
        or len(evidence_receipt_bytes) > REVIEW_RECEIPT_EVIDENCE_MAX_BYTES
    ):
        raise MagicFitSecureIOError(reason, detail="artifact_invalid")
    absolute_root = lexical_absolute_path(root)
    root_fd = _review_receipt_root_fd(absolute_root, reason=reason)
    try:
        with _review_receipt_digest_lock(root_fd, digest, reason=reason):
            try:
                existing = _load_review_receipt_bundle_at(
                    root_fd,
                    delivery_digest=digest,
                    path=absolute_root / digest,
                    reason=reason,
                )
            except MagicFitSecureIOError as exc:
                if not exc.missing:
                    raise
            else:
                return existing

            temporary_name = _REVIEW_RECEIPT_TEMP_NAME
            _cleanup_recognized_review_receipt_temp(
                root_fd, temporary_name, reason=reason
            )
            try:
                os.mkdir(temporary_name, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError as exc:
                raise MagicFitSecureIOError(
                    reason,
                    detail=type(exc).__name__,
                    error_number=exc.errno,
                ) from exc
            temporary_fd = _review_receipt_child_directory_fd(
                root_fd, temporary_name, reason=reason
            )
            try:
                _write_review_receipt_at(
                    temporary_fd,
                    REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES[
                        "browser_receipt"
                    ],
                    browser_receipt_bytes,
                    reason=reason,
                )
                _review_receipt_failpoint("after_browser_receipt")
                _write_review_receipt_at(
                    temporary_fd,
                    REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES[
                        "evidence_receipt"
                    ],
                    evidence_receipt_bytes,
                    reason=reason,
                )
                _review_receipt_failpoint("after_evidence_receipt")
                artifacts: dict[str, object] = {}
                for artifact_name, body in (
                    ("browser_receipt", browser_receipt_bytes),
                    ("evidence_receipt", evidence_receipt_bytes),
                ):
                    artifacts[artifact_name] = {
                        "filename": REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES[
                            artifact_name
                        ],
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size_bytes": len(body),
                    }
                manifest_body = canonical_json_bytes(
                    {
                        "contract_name": REVIEW_RECEIPT_BUNDLE_CONTRACT,
                        "delivery_digest": digest,
                        "artifacts": artifacts,
                    }
                )
                _write_review_receipt_at(
                    temporary_fd,
                    REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME,
                    manifest_body,
                    reason=reason,
                )
                _review_receipt_failpoint("after_bundle_manifest")
                os.fsync(temporary_fd)
                _review_receipt_failpoint("before_bundle_rename")
            finally:
                os.close(temporary_fd)
            _rename_directory_noreplace(
                root_fd, temporary_name, digest, reason=reason
            )
            os.fsync(root_fd)
            _review_receipt_failpoint("after_bundle_rename")
            return _load_review_receipt_bundle_at(
                root_fd,
                delivery_digest=digest,
                path=absolute_root / digest,
                reason=reason,
            )
    finally:
        os.close(root_fd)
