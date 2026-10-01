#!/usr/bin/env python3
"""Create the immutable governed Prater closeout marker."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path


GOVERNED_ROOT = Path("/data/governed_public_property_tours")
ENTRYPOINT_PATH = Path(
    "/usr/local/libexec/propertyquarry-prater-ai-panorama-closeout-v1.py"
)
REQUEST_PATH = Path(
    "/run/propertyquarry-release-control/ai-panorama-install/"
    "prater-ai-panorama-closeout-request.v1.json"
)
MARKER_NAME = (
    ".prater-messe-maisonette-ai-360-053ad185e1c44b2e.revoked.v1.json"
)
MARKER_SCHEMA = "propertyquarry.governed-public-tour-revocation.v1"
MARKER_AUTHORITY = "propertyquarry-release-control"
MARKER_SLUG = "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
MARKER_TOUR_SHA256 = (
    "c3795ca2956c18e3e8b1749611660052dac794a08dec7f47db212b51049cf849"
)
MARKER_KEYS = frozenset(
    {
        "authority",
        "revocation_id",
        "revoked_at",
        "schema",
        "slug",
        "status",
        "tour_sha256",
        "version",
    }
)
REQUEST_MODE = 0o400
MARKER_MODE = 0o444
GOVERNED_ROOT_MODE = 0o755
ENTRYPOINT_UID = 0
ENTRYPOINT_GID = 0
ENTRYPOINT_MODE = 0o555
EXECUTION_UID = 0
EXECUTION_GID = 0
REQUEST_UID = 0
REQUEST_GID = 0
ROOT_UID = 10001
ROOT_GID = 10001
MARKER_UID = 0
MARKER_GID = 0
MAX_BYTES = 4096
_REVOCATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_REVOKED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)


def _checkpoint(_name: str) -> None:
    """Crash-injection seam; production execution intentionally does nothing."""


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


def _strict_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_marker_key")
        result[key] = value
    return result


def _validated_marker_bytes(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("marker_size_invalid")
    payload = json.loads(
        raw.decode("ascii"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("marker_constant_invalid")
        ),
    )
    revoked_at = payload.get("revoked_at") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or set(payload) != MARKER_KEYS
        or payload.get("schema") != MARKER_SCHEMA
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("authority") != MARKER_AUTHORITY
        or payload.get("status") != "revoked"
        or payload.get("slug") != MARKER_SLUG
        or payload.get("tour_sha256") != MARKER_TOUR_SHA256
        or type(payload.get("revocation_id")) is not str
        or _REVOCATION_ID_RE.fullmatch(payload["revocation_id"]) is None
        or type(revoked_at) is not str
        or _REVOKED_AT_RE.fullmatch(revoked_at) is None
    ):
        raise ValueError("marker_payload_invalid")
    datetime.fromisoformat(f"{revoked_at[:-1]}+00:00")
    canonical = _canonical(payload)
    if raw != canonical:
        raise ValueError("marker_noncanonical")
    return canonical


def _same_file(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        int(left.st_dev),
        int(left.st_ino),
        int(left.st_mode),
        int(left.st_uid),
        int(left.st_gid),
        int(left.st_nlink),
        int(left.st_size),
        int(left.st_mtime_ns),
        int(left.st_ctime_ns),
    ) == (
        int(right.st_dev),
        int(right.st_ino),
        int(right.st_mode),
        int(right.st_uid),
        int(right.st_gid),
        int(right.st_nlink),
        int(right.st_size),
        int(right.st_mtime_ns),
        int(right.st_ctime_ns),
    )


def _read_existing_marker(
    root_descriptor: int,
    expected: bytes,
    *,
    required_nlink: int = 1,
) -> os.stat_result:
    descriptor = -1
    try:
        descriptor = os.open(
            MARKER_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or int(before.st_uid) != MARKER_UID
            or int(before.st_gid) != MARKER_GID
            or stat.S_IMODE(before.st_mode) != MARKER_MODE
            or int(before.st_nlink) != required_nlink
            or int(before.st_size) != len(expected)
        ):
            raise RuntimeError("existing_closeout_marker_invalid")
        chunks: list[bytes] = []
        remaining = len(expected)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                raise RuntimeError("existing_closeout_marker_changed")
            chunks.append(chunk)
            remaining -= len(chunk)
            _checkpoint("marker-stable-reread")
        if os.read(descriptor, 1):
            raise RuntimeError("existing_closeout_marker_changed")
        after = os.fstat(descriptor)
        if (
            not _same_file(before, after)
            or b"".join(chunks) != expected
        ):
            raise RuntimeError("existing_closeout_marker_changed")
        return after
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _complete_marker(
    root_descriptor: int,
    *,
    expected: bytes,
) -> os.stat_result:
    """Create or resume the fixed marker as an immediate privacy kill switch."""

    descriptor = -1
    try:
        try:
            descriptor = os.open(
                MARKER_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=root_descriptor,
            )
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            descriptor = os.open(
                MARKER_NAME,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
        root = os.fstat(root_descriptor)
        before = os.fstat(descriptor)
        path_before = os.stat(
            MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        mode = stat.S_IMODE(before.st_mode)
        creator_mode_subset = mode & ~0o600 == 0
        if (
            not _same_file(before, path_before)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_dev) != int(root.st_dev)
            or int(before.st_uid) != MARKER_UID
            or int(before.st_gid) != MARKER_GID
            or int(before.st_nlink) != 1
            or not (
                creator_mode_subset
                or (mode == MARKER_MODE and int(before.st_size) == len(expected))
            )
            or int(before.st_size) < 0
            or int(before.st_size) > len(expected)
        ):
            raise RuntimeError("closeout_marker_invalid")
        size = int(before.st_size)
        completed_read_only = (
            mode == MARKER_MODE and size == len(expected)
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        prefix = bytearray()
        while len(prefix) < size:
            chunk = os.read(descriptor, min(size - len(prefix), 4096))
            if not chunk:
                raise RuntimeError("closeout_marker_changed")
            prefix.extend(chunk)
        if bytes(prefix) != expected[:size]:
            raise RuntimeError("closeout_marker_invalid")
        after_prefix = os.fstat(descriptor)
        path_after_prefix = os.stat(
            MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_file(
                after_prefix,
                path_after_prefix,
            )
            or int(after_prefix.st_size) != size
        ):
            raise RuntimeError("closeout_marker_changed")
        if completed_read_only:
            os.fsync(descriptor)
            os.fsync(root_descriptor)
            return after_prefix
        read_identity = after_prefix
        os.close(descriptor)
        descriptor = os.open(
            MARKER_NAME,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        reopened = os.fstat(descriptor)
        path_reopened = os.stat(
            MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_file(read_identity, reopened)
            or not _same_file(reopened, path_reopened)
        ):
            raise RuntimeError("closeout_marker_changed")
        if stat.S_IMODE(reopened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            reopened = os.fstat(descriptor)
            path_reopened = os.stat(
                MARKER_NAME,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_file(reopened, path_reopened)
                or stat.S_IMODE(reopened.st_mode) != 0o600
            ):
                raise RuntimeError("closeout_marker_changed")

        # The fixed final leaf is durable before content work.  From this
        # point the public route observes an invalid in-progress marker and
        # fails closed even if this process is lost.
        os.fsync(descriptor)
        os.fsync(root_descriptor)
        _checkpoint("marker-kill-switch-durable")
        if size < len(expected):
            os.lseek(descriptor, size, os.SEEK_SET)
            remaining = memoryview(expected)[size:]
            while remaining:
                written = os.write(descriptor, remaining[:64])
                if written <= 0:
                    raise RuntimeError("closeout_marker_short_write")
                remaining = remaining[written:]
                _checkpoint("marker-write-progress")
        _checkpoint("marker-written")
        os.fsync(descriptor)
        _checkpoint("marker-bytes-fsynced")
        os.fchmod(descriptor, MARKER_MODE)
        _checkpoint("marker-readonly")
        os.fsync(descriptor)
        _checkpoint("marker-final-fsynced")
        after = os.fstat(descriptor)
        path_after = os.stat(
            MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_file(after, path_after)
            or not stat.S_ISREG(after.st_mode)
            or int(after.st_dev) != int(root.st_dev)
            or int(after.st_uid) != MARKER_UID
            or int(after.st_gid) != MARKER_GID
            or stat.S_IMODE(after.st_mode) != MARKER_MODE
            or int(after.st_nlink) != 1
            or int(after.st_size) != len(expected)
        ):
            raise RuntimeError("closeout_marker_verification_failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    # Stable exact reread occurs after the writable descriptor is closed.
    final_stat = _read_existing_marker(root_descriptor, expected)
    os.fsync(root_descriptor)
    _checkpoint("marker-stable")
    return final_stat


def _governed_root_inventory(
    root_descriptor: int,
    *,
    expected_names: set[str],
) -> os.stat_result:
    names = set(os.listdir(root_descriptor))
    if names != expected_names:
        raise RuntimeError("governed_root_inventory_invalid")
    return _governed_target_identity(root_descriptor)


def _governed_target_identity(
    root_descriptor: int,
) -> os.stat_result:
    target = os.stat(
        MARKER_SLUG,
        dir_fd=root_descriptor,
        follow_symlinks=False,
    )
    root = os.fstat(root_descriptor)
    # Closeout is a one-way privacy boundary, not install validation.  Bind
    # only the no-follow directory-entry identity and never open, follow, or
    # read a possibly partial or attacker-shaped protected target.
    if int(target.st_dev) != int(root.st_dev):
        raise RuntimeError("governed_target_invalid")
    return target


def _read_request() -> bytes:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = os.open(
            REQUEST_PATH.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor = os.open(
            REQUEST_PATH.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or int(before.st_uid) != REQUEST_UID
            or int(before.st_gid) != REQUEST_GID
            or stat.S_IMODE(before.st_mode) != REQUEST_MODE
            or int(before.st_nlink) != 1
            or int(before.st_size) <= 0
            or int(before.st_size) > MAX_BYTES
        ):
            raise RuntimeError("closeout_request_invalid")
        remaining = int(before.st_size)
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                raise RuntimeError("closeout_request_changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError("closeout_request_changed")
        after = os.fstat(descriptor)
        if not _same_file(before, after):
            raise RuntimeError("closeout_request_changed")
        return _validated_marker_bytes(b"".join(chunks))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def closeout() -> bytes:
    raw = _read_request()
    root_descriptor = -1
    try:
        root_descriptor = os.open(
            GOVERNED_ROOT,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_details = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_details.st_mode)
            or int(root_details.st_uid) != ROOT_UID
            or int(root_details.st_gid) != ROOT_GID
            or stat.S_IMODE(root_details.st_mode) != GOVERNED_ROOT_MODE
        ):
            raise RuntimeError("governed_root_invalid")
        # Establish only that the protected entry exists on this governed
        # volume.  The fixed marker is completed before the wider inventory
        # proof so unrelated entries can never keep the protected tour live.
        target_before = _governed_target_identity(root_descriptor)
        marker = _complete_marker(
            root_descriptor,
            expected=raw,
        )
        target_after = _governed_root_inventory(
            root_descriptor,
            expected_names={MARKER_SLUG, MARKER_NAME},
        )
        if not _same_file(target_before, target_after):
            raise RuntimeError("governed_target_changed")
        path_marker = os.stat(
            MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not _same_file(marker, path_marker):
            raise RuntimeError("closeout_marker_changed")
        return raw
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)


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
        output = closeout()
    except Exception:
        return 1
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
