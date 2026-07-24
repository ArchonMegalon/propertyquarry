#!/usr/bin/env python3
"""Initialize the one dedicated governed tour volume exactly once."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


GOVERNED_ROOT = Path("/data/governed_public_property_tours")
ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/"
    "propertyquarry-prater-governed-volume-bootstrap-v1.py"
)
RESULT_SCHEMA = "propertyquarry.prater-governed-volume-bootstrap-result.v1"
ENTRYPOINT_UID = 0
ENTRYPOINT_GID = 0
ENTRYPOINT_MODE = 0o555
EXECUTION_UID = 0
EXECUTION_GID = 0
INITIAL_UID = 0
INITIAL_GID = 0
RUNTIME_UID = 10001
RUNTIME_GID = 10001
ROOT_MODE = 0o755


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _self_attest() -> None:
    descriptor = -1
    try:
        before = ENTRYPOINT_PATH.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_uid) != ENTRYPOINT_UID
            or int(before.st_gid) != ENTRYPOINT_GID
            or stat.S_IMODE(before.st_mode) != ENTRYPOINT_MODE
            or int(before.st_nlink) != 1
        ):
            raise RuntimeError("entrypoint_identity_invalid")
        descriptor = os.open(
            ENTRYPOINT_PATH,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        after = ENTRYPOINT_PATH.stat(follow_symlinks=False)
        identity = (
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
        for observed in (opened, after):
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
                raise RuntimeError("entrypoint_identity_changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def bootstrap() -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            GOVERNED_ROOT,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or int(before.st_uid) != INITIAL_UID
            or int(before.st_gid) != INITIAL_GID
            or stat.S_IMODE(before.st_mode) != ROOT_MODE
            or os.listdir(descriptor)
        ):
            raise RuntimeError("governed_volume_not_virgin")
        os.fchmod(descriptor, ROOT_MODE)
        if (
            int(before.st_uid) != RUNTIME_UID
            or int(before.st_gid) != RUNTIME_GID
        ):
            os.fchown(descriptor, RUNTIME_UID, RUNTIME_GID)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(after.st_mode)
            or int(after.st_dev) != int(before.st_dev)
            or int(after.st_ino) != int(before.st_ino)
            or int(after.st_uid) != RUNTIME_UID
            or int(after.st_gid) != RUNTIME_GID
            or stat.S_IMODE(after.st_mode) != ROOT_MODE
            or os.listdir(descriptor)
        ):
            raise RuntimeError("governed_volume_bootstrap_verification_failed")
        result = {
            "private_values_redacted": True,
            "root_device": int(after.st_dev),
            "root_empty": True,
            "root_gid": RUNTIME_GID,
            "root_inode": int(after.st_ino),
            "root_mode": ROOT_MODE,
            "root_uid": RUNTIME_UID,
            "schema": RESULT_SCHEMA,
            "status": "initialized",
            "version": 1,
        }
        return _canonical(result)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> int:
    if (
        len(sys.argv) != 1
        or Path(sys.argv[0]) != ENTRYPOINT_PATH
        or os.geteuid() != EXECUTION_UID
        or os.getegid() != EXECUTION_GID
    ):
        return 2
    try:
        _self_attest()
        output = bootstrap()
    except Exception:
        return 1
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
