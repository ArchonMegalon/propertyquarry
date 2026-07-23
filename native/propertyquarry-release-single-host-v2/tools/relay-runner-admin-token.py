#!/usr/bin/env python3
"""Gate and relay one runner-administration token without persisting it."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import re
import resource
import select
import stat
import time


TOKEN_FD = 8
GATE_FD = 7
STATUS_FD = 9
CALLER_UID = 1000
CALLER_GID = 1000
MAXIMUM_TOKEN_BYTES = 2048
TOKEN_PATTERN = re.compile(rb"^[A-Za-z0-9._-]{20,2048}$")
PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


class RelayRejected(Exception):
    """A deliberately detail-free token relay rejection."""


def reject() -> None:
    raise RelayRejected


def disable_process_dumps() -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            reject()
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            reject()
        if prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            reject()
    except (RelayRejected, OSError, ValueError):
        reject()


def require_fifo_fd(
    descriptor: int,
    *,
    access_mode: int | None,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError:
        reject()
    if (
        not stat.S_ISFIFO(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != CALLER_UID
        or metadata.st_gid != CALLER_GID
        or metadata.st_nlink != 1
        or (
            access_mode is not None
            and flags & os.O_ACCMODE != access_mode
        )
    ):
        reject()
    return metadata


def read_exact_gate(descriptor: int) -> None:
    expected = b"release\n"
    observed = bytearray()
    try:
        while len(observed) < len(expected):
            chunk = os.read(descriptor, len(expected) - len(observed))
            if not chunk:
                reject()
            observed.extend(chunk)
        if observed != expected:
            reject()
        readable, _, _ = select.select([descriptor], [], [], 0)
        if readable and os.read(descriptor, 1):
            reject()
    finally:
        for index in range(len(observed)):
            observed[index] = 0


def read_token_fd(
    descriptor: int,
    timeout_seconds: float = 60.0,
) -> bytearray:
    require_fifo_fd(descriptor, access_mode=os.O_RDONLY)
    deadline = time.monotonic() + timeout_seconds
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reject()
            readable, _, _ = select.select(
                [descriptor], [], [], remaining
            )
            if not readable:
                reject()
            chunk = os.read(
                descriptor,
                min(1024, MAXIMUM_TOKEN_BYTES + 2 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAXIMUM_TOKEN_BYTES + 1:
                reject()
        if raw.endswith(b"\n"):
            del raw[-1:]
        if not TOKEN_PATTERN.fullmatch(raw):
            reject()
        return raw
    except BaseException:
        for index in range(len(raw)):
            raw[index] = 0
        raise


def write_all(descriptor: int, raw: bytes | bytearray) -> None:
    view = memoryview(raw)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count < 1:
            reject()
        written += count


def write_token_fifo(
    path: str,
    token: bytearray,
    timeout_seconds: float = 60.0,
) -> None:
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or candidate != Path(os.path.normpath(path))
    ):
        reject()
    try:
        before = os.lstat(path)
    except OSError:
        reject()
    if (
        not stat.S_ISFIFO(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != CALLER_UID
        or before.st_gid != CALLER_GID
        or before.st_nlink != 1
    ):
        reject()
    deadline = time.monotonic() + timeout_seconds
    descriptor = -1
    while descriptor < 0:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_NONBLOCK
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
            )
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                reject()
            time.sleep(0.05)
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISFIFO(after.st_mode)
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_uid != CALLER_UID
            or after.st_gid != CALLER_GID
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            reject()
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(
            descriptor,
            fcntl.F_SETFL,
            flags & ~os.O_NONBLOCK,
        )
        write_all(descriptor, token)
        write_all(descriptor, b"\n")
    finally:
        os.close(descriptor)


def relay(
    token_fd: int,
    gate_fd: int,
    status_fd: int,
    relay_fifo: str,
) -> None:
    disable_process_dumps()
    if (
        token_fd != TOKEN_FD
        or gate_fd != GATE_FD
        or status_fd != STATUS_FD
        or len({token_fd, gate_fd, status_fd}) != 3
    ):
        reject()
    require_fifo_fd(token_fd, access_mode=os.O_RDONLY)
    require_fifo_fd(gate_fd, access_mode=os.O_RDWR)
    require_fifo_fd(status_fd, access_mode=os.O_RDWR)
    read_exact_gate(gate_fd)
    token = read_token_fd(token_fd)
    try:
        write_all(status_fd, b"runner-admin-token-ready\n")
        write_token_fifo(relay_fifo, token)
    finally:
        for index in range(len(token)):
            token[index] = 0


def failure_status(descriptor: int) -> None:
    try:
        os.write(descriptor, b"runner-admin-token-relay-rejected\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--token-fd", required=True, type=int)
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--status-fd", required=True, type=int)
    parser.add_argument("--relay-fifo", required=True)
    arguments = parser.parse_args()
    try:
        relay(
            arguments.token_fd,
            arguments.gate_fd,
            arguments.status_fd,
            arguments.relay_fifo,
        )
    except BaseException:
        failure_status(arguments.status_fd)
        return 50
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
