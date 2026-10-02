#!/usr/bin/env python3
"""Canonical per-tour publication lock shared by web and offline workers.

Every operation that can publish, replace, revoke, import, or accept assets for
one public tour slug must take this lock before any bundle-local lock.  Keep
this module dependency-free so the web image and standalone operator scripts
derive and open the exact same lock inode.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import math
import os
import stat
import time
from pathlib import Path
from typing import Iterator


def property_tour_publication_lock_timeout_seconds() -> float:
    raw = str(
        os.getenv(
            "PROPERTYQUARRY_RECONSTRUCTION_PUBLICATION_LOCK_TIMEOUT_SECONDS"
        )
        or ""
    ).strip()
    try:
        parsed = float(raw or "30")
    except (TypeError, ValueError):
        parsed = 30.0
    if not math.isfinite(parsed):
        parsed = 30.0
    return max(0.05, min(parsed, 300.0))


def property_tour_publication_lock_directory(public_dir: Path) -> Path:
    return public_dir.parent / ".propertyquarry-tour-publication-locks"


def property_tour_publication_lock_name(*, slug: str) -> str:
    digest = hashlib.sha256(str(slug or "").strip().encode("utf-8")).hexdigest()
    return f"{digest}.lock"


@contextlib.contextmanager
def property_tour_publication_lock(
    *,
    public_dir: Path,
    slug: str,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Acquire the canonical owned, no-follow, digest-named slug lock."""

    lock_dir = property_tour_publication_lock_directory(public_dir)
    created_lock_dir = False
    try:
        lock_dir.mkdir(mode=0o700)
        created_lock_dir = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimeError(
            "property_reconstruction_publication_lock_directory_unavailable"
        ) from exc
    if created_lock_dir:
        try:
            lock_dir.chmod(0o700)
        except OSError as exc:
            raise RuntimeError(
                "property_reconstruction_publication_lock_directory_unsafe"
            ) from exc

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        lock_dir_fd = os.open(lock_dir, directory_flags)
    except OSError as exc:
        raise RuntimeError(
            "property_reconstruction_publication_lock_directory_unsafe"
        ) from exc
    lock_fd: int | None = None
    acquired = False
    try:
        lock_dir_stat = os.fstat(lock_dir_fd)
        if (
            not stat.S_ISDIR(lock_dir_stat.st_mode)
            or lock_dir_stat.st_uid != os.geteuid()
            or stat.S_IMODE(lock_dir_stat.st_mode) != 0o700
        ):
            raise RuntimeError(
                "property_reconstruction_publication_lock_directory_unsafe"
            )

        lock_name = property_tour_publication_lock_name(slug=slug)
        lock_flags = (
            os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        created_lock_file = False
        try:
            lock_fd = os.open(
                lock_name,
                lock_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=lock_dir_fd,
            )
            created_lock_file = True
        except FileExistsError:
            try:
                lock_fd = os.open(lock_name, lock_flags, dir_fd=lock_dir_fd)
            except OSError as exc:
                raise RuntimeError(
                    "property_reconstruction_publication_lock_file_unsafe"
                ) from exc
        except OSError as exc:
            raise RuntimeError(
                "property_reconstruction_publication_lock_file_unavailable"
            ) from exc
        if created_lock_file:
            try:
                os.fchmod(lock_fd, 0o600)
            except OSError as exc:
                raise RuntimeError(
                    "property_reconstruction_publication_lock_file_unsafe"
                ) from exc
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.geteuid()
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
            or lock_stat.st_nlink != 1
        ):
            raise RuntimeError(
                "property_reconstruction_publication_lock_file_unsafe"
            )

        timeout = (
            property_tour_publication_lock_timeout_seconds()
            if timeout_seconds is None
            else max(0.0, min(float(timeout_seconds), 300.0))
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise RuntimeError(
                        "property_reconstruction_publication_lock_failed"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise RuntimeError(
                        "property_reconstruction_publication_lock_timeout"
                    ) from exc
                time.sleep(min(0.01, remaining))
        yield
    finally:
        if lock_fd is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.close(lock_dir_fd)
