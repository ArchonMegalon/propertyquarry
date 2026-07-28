#!/usr/bin/python3.12
"""Create and identify the canonical release-venv root without a signal gap."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".propertyquarry_release_tools" / "release-venv"
BLOCKED_SIGNALS = frozenset({signal.SIGHUP, signal.SIGINT, signal.SIGTERM})


class CreationError(RuntimeError):
    """The canonical release environment root could not be created safely."""

    def __init__(
        self,
        message: str,
        *,
        created_identity: str | None = None,
    ) -> None:
        super().__init__(message)
        self.created_identity = created_identity


def _fail(
    message: str,
    *,
    created_identity: str | None = None,
) -> NoReturn:
    raise CreationError(message, created_identity=created_identity)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
    )


def _directory_binding(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _trusted_directory(metadata: os.stat_result, *, label: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        _fail(f"{label} is not a trusted directory")


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        _fail(f"created release verifier cannot be opened safely: {exc}")


def _create_and_identify(target: Path) -> str:
    parent = target.parent
    created_identity: str | None = None
    descriptor = -1
    parent_descriptor = -1
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        _fail(f"release verifier parent is unavailable: {exc}")
    _trusted_directory(parent_before, label="release verifier parent")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _fail(f"release verifier target cannot be inspected: {exc}")
    else:
        _fail("release verifier target already exists")

    previous_umask = os.umask(0o077)
    try:
        os.mkdir(target, 0o700)
    except OSError as exc:
        _fail(f"release verifier target cannot be created: {exc}")
    finally:
        os.umask(previous_umask)

    try:
        try:
            path_metadata = target.lstat()
        except OSError as exc:
            _fail(f"created release verifier cannot be identified: {exc}")
        created_identity = f"{path_metadata.st_dev}:{path_metadata.st_ino}"
        _trusted_directory(path_metadata, label="created release verifier")
        if stat.S_IMODE(path_metadata.st_mode) != 0o700:
            _fail("created release verifier mode is not 0700")

        descriptor = _open_directory(target)
        opened = os.fstat(descriptor)
        _trusted_directory(opened, label="created release verifier")
        if stat.S_IMODE(opened.st_mode) != 0o700:
            _fail("created release verifier mode is not 0700")
        try:
            path_metadata = target.lstat()
        except OSError as exc:
            _fail(f"created release verifier changed before inspection: {exc}")
        if _identity(path_metadata) != _identity(opened):
            _fail("created release verifier changed before inspection")
        os.fsync(descriptor)

        parent_descriptor = _open_directory(parent)
        parent_opened = os.fstat(parent_descriptor)
        if _directory_binding(parent_opened) != _directory_binding(parent_before):
            _fail("release verifier parent changed during creation")
        os.fsync(parent_descriptor)
    except CreationError as exc:
        if exc.created_identity is None:
            exc.created_identity = created_identity
        raise
    except OSError as exc:
        raise CreationError(
            f"created release verifier finalization failed: {exc}",
            created_identity=created_identity,
        ) from exc
    finally:
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if created_identity is None:
        _fail("created release verifier identity was not established")
    return created_identity


def _quarantine_created_target(target: Path, created_identity: str) -> Path:
    parent = target.parent
    parent_descriptor = -1
    quarantine: Path | None = None
    placeholder_identity: tuple[int, ...] | None = None
    moved = False
    try:
        parent_metadata = parent.lstat()
        _trusted_directory(parent_metadata, label="release verifier parent")
        parent_descriptor = _open_directory(parent)
        if _directory_binding(os.fstat(parent_descriptor)) != _directory_binding(
            parent_metadata
        ):
            _fail("release verifier parent changed before quarantine")

        current = target.lstat()
        _trusted_directory(current, label="created release verifier")
        if f"{current.st_dev}:{current.st_ino}" != created_identity:
            _fail("created release verifier changed before quarantine")

        quarantine = Path(
            tempfile.mkdtemp(
                prefix="release-venv.incomplete.",
                dir=parent,
            )
        )
        placeholder = quarantine.lstat()
        _trusted_directory(
            placeholder,
            label="release verifier quarantine placeholder",
        )
        if stat.S_IMODE(placeholder.st_mode) != 0o700:
            _fail("release verifier quarantine placeholder mode is not 0700")
        placeholder_identity = _identity(placeholder)

        os.rename(target, quarantine)
        moved = True
        quarantined = quarantine.lstat()
        if f"{quarantined.st_dev}:{quarantined.st_ino}" != created_identity:
            _fail("created release verifier changed while it was quarantined")
        os.fsync(parent_descriptor)
        return quarantine
    except CreationError as exc:
        suffix = (
            f"; incomplete release verifier retained at {quarantine}"
            if moved and quarantine is not None
            else ""
        )
        raise CreationError(
            f"created release verifier cannot be quarantined: {exc}{suffix}",
            created_identity=created_identity,
        ) from exc
    except OSError as exc:
        suffix = (
            f"; incomplete release verifier retained at {quarantine}"
            if moved and quarantine is not None
            else ""
        )
        raise CreationError(
            f"created release verifier cannot be quarantined: {exc}{suffix}",
            created_identity=created_identity,
        ) from exc
    finally:
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
        if (
            not moved
            and quarantine is not None
            and placeholder_identity is not None
        ):
            try:
                current_placeholder = quarantine.lstat()
                if _identity(current_placeholder) == placeholder_identity:
                    os.rmdir(quarantine)
            except OSError:
                pass


def _diagnose(message: str) -> None:
    try:
        print(f"error: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        sys.stderr = None


def _emit_created_identity(created_identity: str) -> bool:
    try:
        print(created_identity, flush=True)
    except (OSError, ValueError) as exc:
        sys.stdout = None
        try:
            quarantine = _quarantine_created_target(TARGET, created_identity)
        except CreationError as quarantine_exc:
            _diagnose(
                "release verifier identity could not be reported and its exact "
                f"environment could not be quarantined: {exc}; {quarantine_exc}"
            )
        else:
            _diagnose(
                "release verifier identity could not be reported; incomplete "
                f"environment retained at {quarantine}: {exc}"
            )
        return False
    return True


def main() -> int:
    if len(sys.argv) != 1:
        _fail("release verifier root creator accepts no arguments")
    if ROOT != Path("/docker/property"):
        _fail("release verifier root creator requires /docker/property")
    signal.pthread_sigmask(signal.SIG_BLOCK, BLOCKED_SIGNALS)
    created_identity = _create_and_identify(TARGET)
    return 0 if _emit_created_identity(created_identity) else 2


def _entrypoint() -> int:
    try:
        return main()
    except CreationError as exc:
        if exc.created_identity is not None:
            _emit_created_identity(exc.created_identity)
        _diagnose(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
