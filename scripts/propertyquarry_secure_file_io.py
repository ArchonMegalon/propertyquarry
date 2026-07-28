#!/usr/bin/env python3
"""Small fail-closed filesystem primitives for release evidence.

The helpers in this module deliberately operate relative to retained
directory descriptors.  They do not resolve symlinks, reopen a published
pathname, or separate a no-overwrite decision from publication.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path


class SecureFileIOError(RuntimeError):
    """A release-evidence file could not be consumed or published safely."""


class OutputExistsError(SecureFileIOError):
    """A create-if-absent publication found an existing destination."""


_DirectoryIdentity = tuple[int, int, int, int, int]
_FileIdentity = tuple[int, int, int, int, int, int, int, int, int]
_DirectoryHandle = tuple[
    int,
    int | None,
    str | None,
    _DirectoryIdentity,
]


def _canonical_path(path: Path) -> Path:
    try:
        raw = os.path.abspath(os.path.normpath(os.fspath(path.expanduser())))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SecureFileIOError("path is invalid") from exc
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise SecureFileIOError("path is not absolute")
    # POSIX permits an implementation-defined // anchor.  Normalize it to
    # the one root descriptor from which all traversal below is anchored.
    return Path("/") / Path(*candidate.parts[1:])


def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


def _close_directory_handles(handles: list[_DirectoryHandle]) -> None:
    for descriptor, _, _, _ in reversed(handles):
        with suppress(OSError):
            os.close(descriptor)


def _validate_directory(
    value: os.stat_result,
    *,
    final: bool,
) -> None:
    peer_writable = bool(value.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky = bool(value.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or (peer_writable and (final or not sticky))
    ):
        raise SecureFileIOError(
            "directory chain is not trusted-owner and immutable to peers"
        )


def _revalidate_directory_chain(handles: list[_DirectoryHandle]) -> None:
    for index, (descriptor, parent_descriptor, component, identity) in enumerate(
        handles
    ):
        try:
            opened = os.fstat(descriptor)
            if parent_descriptor is None:
                named = os.lstat("/")
            else:
                named = os.stat(
                    str(component),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
        except OSError as exc:
            raise SecureFileIOError(
                "directory chain changed while it was used"
            ) from exc
        _validate_directory(opened, final=index == len(handles) - 1)
        if (
            _directory_identity(opened) != identity
            or _directory_identity(named) != identity
        ):
            raise SecureFileIOError(
                "directory chain changed while it was used"
            )


def _open_directory_chain(
    parent: Path,
    *,
    create: bool,
) -> list[_DirectoryHandle]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    handles: list[_DirectoryHandle] = []
    components = parent.parts[1:]
    try:
        root_descriptor = os.open("/", flags)
        handles.append((root_descriptor, None, None, (0, 0, 0, 0, 0)))
        root_metadata = os.fstat(root_descriptor)
        root_identity = _directory_identity(root_metadata)
        handles[-1] = (root_descriptor, None, None, root_identity)
        _validate_directory(root_metadata, final=not components)

        for index, component in enumerate(components):
            parent_descriptor = handles[-1][0]
            created = False
            try:
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_descriptor)
                    created = True
                except FileExistsError:
                    pass
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=parent_descriptor,
                )
            handles.append(
                (
                    child_descriptor,
                    parent_descriptor,
                    component,
                    (0, 0, 0, 0, 0),
                )
            )
            child_metadata = os.fstat(child_descriptor)
            child_identity = _directory_identity(child_metadata)
            handles[-1] = (
                child_descriptor,
                parent_descriptor,
                component,
                child_identity,
            )
            named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _validate_directory(
                child_metadata,
                final=index == len(components) - 1,
            )
            if (
                _directory_identity(named) != child_identity
            ):
                raise SecureFileIOError(
                    "directory chain changed while it was opened"
                )
            if created:
                os.fsync(parent_descriptor)

        _revalidate_directory_chain(handles)
        return handles
    except SecureFileIOError:
        _close_directory_handles(handles)
        raise
    except (OSError, ValueError) as exc:
        _close_directory_handles(handles)
        raise SecureFileIOError(
            "directory chain cannot be opened without following links"
        ) from exc


def _validate_regular_file(
    value: os.stat_result,
    *,
    maximum_bytes: int | None = None,
    require_nonempty: bool = False,
) -> None:
    invalid_size = (
        maximum_bytes is not None
        and (value.st_size > maximum_bytes or (require_nonempty and value.st_size <= 0))
    )
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or value.st_mode
        & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or value.st_nlink != 1
        or invalid_size
    ):
        raise SecureFileIOError(
            "must be a trusted-owner, singly linked regular file "
            "that is immutable to peers and within its size bound"
        )


def read_stable_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    require_nonempty: bool = True,
) -> bytes:
    """Read one immutable pathname snapshot without blocking on special files."""

    if maximum_bytes <= 0:
        raise SecureFileIOError("maximum byte count must be positive")
    candidate = _canonical_path(path)
    if candidate == Path("/") or candidate.name in {"", ".", ".."}:
        raise SecureFileIOError("path is invalid")

    handles: list[_DirectoryHandle] = []
    descriptor = -1
    try:
        handles = _open_directory_chain(candidate.parent, create=False)
        parent_descriptor = handles[-1][0]
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(candidate.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        _validate_regular_file(
            opened,
            maximum_bytes=maximum_bytes,
            require_nonempty=require_nonempty,
        )
        opened_identity = _file_identity(opened)
        named = os.stat(
            candidate.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(named) != opened_identity:
            raise SecureFileIOError("file changed while it was opened")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SecureFileIOError("file exceeds its bounded size")
            chunks.append(chunk)
        payload = b"".join(chunks)

        after_opened = os.fstat(descriptor)
        after_named = os.stat(
            candidate.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            len(payload) != opened.st_size
            or _file_identity(after_opened) != opened_identity
            or _file_identity(after_named) != opened_identity
        ):
            raise SecureFileIOError("file changed while it was read")
        _revalidate_directory_chain(handles)
        return payload
    except SecureFileIOError:
        raise
    except (OSError, ValueError) as exc:
        raise SecureFileIOError(
            "cannot be opened as a stable nonblocking regular file"
        ) from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        _close_directory_handles(handles)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise SecureFileIOError("staging file write made no progress")
        remaining = remaining[written:]


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    overwrite: bool,
) -> None:
    """Publish complete private bytes relative to one retained parent FD."""

    destination = _canonical_path(path)
    if destination == Path("/") or destination.name in {"", ".", ".."}:
        raise SecureFileIOError("output path is invalid")

    handles: list[_DirectoryHandle] = []
    descriptor = -1
    temporary_name: str | None = None
    try:
        handles = _open_directory_chain(destination.parent, create=True)
        parent_descriptor = handles[-1][0]
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        for _ in range(64):
            candidate_name = (
                f".{destination.name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                descriptor = os.open(
                    candidate_name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate_name
            break
        if descriptor < 0 or temporary_name is None:
            raise SecureFileIOError(
                "a unique private staging file could not be allocated"
            )

        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        _validate_regular_file(staged)
        staged_identity = _file_identity(staged)
        staged_named = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(staged_named) != staged_identity:
            raise SecureFileIOError("staging file changed before publication")
        _revalidate_directory_chain(handles)

        if overwrite:
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_name = None
        else:
            try:
                os.link(
                    temporary_name,
                    destination.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise OutputExistsError("output already exists") from exc
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            temporary_name = None

        published = os.fstat(descriptor)
        _validate_regular_file(published)
        published_identity = _file_identity(published)
        published_named = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_identity(published_named) != published_identity:
            raise SecureFileIOError("output changed during publication")
        _revalidate_directory_chain(handles)
        os.fsync(parent_descriptor)

        final_opened = os.fstat(descriptor)
        final_named = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(final_opened) != published_identity
            or _file_identity(final_named) != published_identity
        ):
            raise SecureFileIOError("output changed while publication was synced")
        _revalidate_directory_chain(handles)
    except SecureFileIOError:
        raise
    except (OSError, ValueError) as exc:
        raise SecureFileIOError("output cannot be published safely") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_name is not None and handles:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=handles[-1][0])
        _close_directory_handles(handles)
