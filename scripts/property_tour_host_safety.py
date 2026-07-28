#!/usr/bin/env python3
"""Shared host-safety guards for PropertyQuarry tour import/render scripts."""

from __future__ import annotations

import os
import signal
import shutil
import stat
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence

import fcntl


class TourHostSafetyError(RuntimeError):
    """A stable fail-closed reason suitable for operator receipts."""


def bounded_env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        parsed = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(parsed, int(maximum)))


def tour_manifest_max_bytes() -> int:
    return bounded_env_int(
        "PROPERTYQUARRY_TOUR_MANIFEST_MAX_BYTES",
        default=2 * 1024 * 1024,
        minimum=1_024,
        maximum=16 * 1024 * 1024,
    )


def tour_asset_max_bytes() -> int:
    return bounded_env_int(
        "PROPERTYQUARRY_TOUR_ASSET_MAX_BYTES",
        default=512 * 1024 * 1024,
        minimum=1_024,
        maximum=2 * 1024 * 1024 * 1024,
    )


def minimum_free_bytes(*, env_name: str = "") -> int:
    specific_name = str(env_name or "").strip()
    raw_specific = str(os.getenv(specific_name) or "").strip() if specific_name else ""
    raw_general = str(os.getenv("PROPERTYQUARRY_TOUR_MIN_FREE_BYTES") or "").strip()
    raw = raw_specific or raw_general
    try:
        parsed = int(raw) if raw else 10 * 1024 * 1024 * 1024
    except (TypeError, ValueError):
        parsed = 10 * 1024 * 1024 * 1024
    return max(0, min(parsed, 10 * 1024 * 1024 * 1024 * 1024))


def require_free_disk(
    path: Path,
    *,
    reason_prefix: str,
    expected_write_bytes: int = 0,
    env_name: str = "",
) -> dict[str, int]:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
    except OSError as exc:
        raise TourHostSafetyError(f"{reason_prefix}_disk_unavailable") from exc
    minimum = minimum_free_bytes(env_name=env_name)
    reserve = max(int(expected_write_bytes), 0)
    free = int(usage.free)
    if free < minimum + reserve:
        raise TourHostSafetyError(f"{reason_prefix}_low_disk")
    return {
        "free_bytes": free,
        "minimum_free_bytes": minimum,
        "expected_write_bytes": reserve,
    }


def require_bounded_file(
    path: Path,
    *,
    reason_prefix: str,
    maximum_bytes: int | None = None,
    allow_empty: bool = False,
) -> int:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TourHostSafetyError(f"{reason_prefix}_missing") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise TourHostSafetyError(f"{reason_prefix}_invalid_type")
    size = int(details.st_size)
    limit = int(maximum_bytes if maximum_bytes is not None else tour_asset_max_bytes())
    if size < 0 or (not allow_empty and size == 0):
        raise TourHostSafetyError(f"{reason_prefix}_empty")
    if size > limit:
        raise TourHostSafetyError(f"{reason_prefix}_too_large")
    return size


def require_bounded_tree(
    root: Path,
    *,
    reason_prefix: str,
    maximum_files: int | None = None,
    maximum_total_bytes: int | None = None,
    maximum_file_bytes: int | None = None,
    maximum_depth: int = 32,
) -> dict[str, int]:
    file_limit = maximum_files or bounded_env_int(
        "PROPERTYQUARRY_TOUR_EXPORT_MAX_FILES",
        default=20_000,
        minimum=1,
        maximum=100_000,
    )
    total_limit = maximum_total_bytes or bounded_env_int(
        "PROPERTYQUARRY_TOUR_EXPORT_MAX_EXPANDED_BYTES",
        default=2 * 1024 * 1024 * 1024,
        minimum=1_024,
        maximum=8 * 1024 * 1024 * 1024,
    )
    per_file_limit = maximum_file_bytes or tour_asset_max_bytes()
    try:
        root_details = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise TourHostSafetyError(f"{reason_prefix}_missing") from exc
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(root_details.st_mode):
        raise TourHostSafetyError(f"{reason_prefix}_invalid_type")

    files = 0
    directories = 0
    total_bytes = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > maximum_depth:
            raise TourHostSafetyError(f"{reason_prefix}_depth_limit")
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise TourHostSafetyError(f"{reason_prefix}_unreadable") from exc
        for entry in entries:
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise TourHostSafetyError(f"{reason_prefix}_unreadable") from exc
            if stat.S_ISLNK(details.st_mode):
                raise TourHostSafetyError(f"{reason_prefix}_symlink_forbidden")
            if stat.S_ISDIR(details.st_mode):
                directories += 1
                stack.append((Path(entry.path), depth + 1))
                continue
            if not stat.S_ISREG(details.st_mode):
                raise TourHostSafetyError(f"{reason_prefix}_special_file_forbidden")
            files += 1
            size = int(details.st_size)
            total_bytes += size
            if files > file_limit:
                raise TourHostSafetyError(f"{reason_prefix}_file_count_limit")
            if size > per_file_limit:
                raise TourHostSafetyError(f"{reason_prefix}_file_too_large")
            if total_bytes > total_limit:
                raise TourHostSafetyError(f"{reason_prefix}_expanded_size_limit")
    return {
        "file_count": files,
        "directory_count": directories,
        "total_bytes": total_bytes,
        "file_limit": int(file_limit),
        "total_bytes_limit": int(total_limit),
        "per_file_bytes_limit": int(per_file_limit),
    }


def safe_extract_tour_zip(
    zip_path: Path,
    target_dir: Path,
    *,
    reason_prefix: str,
) -> Path:
    archive_limit = bounded_env_int(
        "PROPERTYQUARRY_TOUR_ARCHIVE_MAX_BYTES",
        default=1024 * 1024 * 1024,
        minimum=1_024,
        maximum=4 * 1024 * 1024 * 1024,
    )
    maximum_files = bounded_env_int(
        "PROPERTYQUARRY_TOUR_EXPORT_MAX_FILES",
        default=20_000,
        minimum=1,
        maximum=100_000,
    )
    expanded_limit = bounded_env_int(
        "PROPERTYQUARRY_TOUR_EXPORT_MAX_EXPANDED_BYTES",
        default=2 * 1024 * 1024 * 1024,
        minimum=1_024,
        maximum=8 * 1024 * 1024 * 1024,
    )
    member_limit = tour_asset_max_bytes()
    ratio_limit = bounded_env_int(
        "PROPERTYQUARRY_TOUR_ARCHIVE_MAX_COMPRESSION_RATIO",
        default=200,
        minimum=2,
        maximum=1_000,
    )
    require_bounded_file(
        zip_path,
        reason_prefix=f"{reason_prefix}_archive",
        maximum_bytes=archive_limit,
    )
    require_free_disk(
        target_dir,
        reason_prefix=reason_prefix,
        expected_write_bytes=expanded_limit,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TourHostSafetyError(f"{reason_prefix}_archive_invalid") from exc
    with archive:
        members = archive.infolist()
        if len(members) > maximum_files:
            raise TourHostSafetyError(f"{reason_prefix}_file_count_limit")
        seen: set[str] = set()
        declared_total = 0
        validated: list[tuple[zipfile.ZipInfo, Path]] = []
        for member in members:
            raw_name = str(member.filename or "")
            if (
                not raw_name
                or len(raw_name) > 1_024
                or "\\" in raw_name
                or raw_name.startswith("/")
                or "\x00" in raw_name
                or (len(raw_name) >= 2 and raw_name[1] == ":")
            ):
                raise TourHostSafetyError(f"{reason_prefix}_unsafe_path")
            path = PurePosixPath(raw_name)
            parts = tuple(part for part in path.parts if part != "/")
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise TourHostSafetyError(f"{reason_prefix}_unsafe_path")
            normalized_name = "/".join(parts)
            if normalized_name in seen:
                raise TourHostSafetyError(f"{reason_prefix}_duplicate_path")
            seen.add(normalized_name)
            unix_mode = (int(member.external_attr) >> 16) & 0xFFFF
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise TourHostSafetyError(f"{reason_prefix}_symlink_forbidden")
            if int(member.flag_bits) & 0x1:
                raise TourHostSafetyError(f"{reason_prefix}_encrypted_member_forbidden")
            file_size = int(member.file_size)
            compressed_size = int(member.compress_size)
            if file_size < 0 or compressed_size < 0 or file_size > member_limit:
                raise TourHostSafetyError(f"{reason_prefix}_member_too_large")
            if file_size and compressed_size == 0:
                raise TourHostSafetyError(f"{reason_prefix}_compression_ratio_limit")
            if compressed_size and file_size / compressed_size > ratio_limit:
                raise TourHostSafetyError(f"{reason_prefix}_compression_ratio_limit")
            declared_total += file_size
            if declared_total > expanded_limit:
                raise TourHostSafetyError(f"{reason_prefix}_expanded_size_limit")
            destination = (target_dir / normalized_name).resolve()
            if destination != target_root and target_root not in destination.parents:
                raise TourHostSafetyError(f"{reason_prefix}_unsafe_path")
            validated.append((member, destination))

        actual_total = 0
        for member, destination in validated:
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            member_written = 0
            try:
                with archive.open(member, "r") as source, destination.open("xb") as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        member_written += len(chunk)
                        actual_total += len(chunk)
                        if member_written > member_limit:
                            raise TourHostSafetyError(f"{reason_prefix}_member_too_large")
                        if actual_total > expanded_limit:
                            raise TourHostSafetyError(f"{reason_prefix}_expanded_size_limit")
                        target.write(chunk)
            except FileExistsError as exc:
                raise TourHostSafetyError(f"{reason_prefix}_duplicate_path") from exc
            if member_written != int(member.file_size):
                raise TourHostSafetyError(f"{reason_prefix}_member_size_mismatch")

    require_bounded_tree(
        target_dir,
        reason_prefix=reason_prefix,
        maximum_files=maximum_files,
        maximum_total_bytes=expanded_limit,
        maximum_file_bytes=member_limit,
    )
    children = [path for path in target_dir.iterdir() if path.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
        return children[0].resolve()
    return target_root


@contextmanager
def bounded_lane_lock(lane: str) -> Iterator[None]:
    safe_lane = "".join(character for character in str(lane or "") if character.isalnum() or character in {"-", "_"})
    if not safe_lane:
        raise TourHostSafetyError("tour_lane_lock_invalid")
    lock_root = Path(
        os.getenv("PROPERTYQUARRY_TOUR_LOCK_DIR")
        or "/tmp/property-tour-locks"
    ).expanduser()
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_root.chmod(0o700)
    lock_path = lock_root / f"{safe_lane}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TourHostSafetyError(f"{safe_lane}_concurrency_limit_reached") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run_bounded_subprocess(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> BoundedProcessResult:
    process = subprocess.Popen(
        [str(item) for item in command],
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    output_lock = threading.Lock()
    total_output = 0
    output_exceeded = threading.Event()

    def _terminate_group(sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (OSError, ProcessLookupError):
            pass

    def _drain(name: str, stream: object) -> None:
        nonlocal total_output
        reader = stream
        while True:
            chunk = reader.read(64 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                break
            with output_lock:
                total_output += len(chunk)
                remaining = max(maximum_output_bytes - len(buffers[name]), 0)
                if remaining:
                    buffers[name].extend(chunk[:remaining])
                if total_output > maximum_output_bytes:
                    output_exceeded.set()
                    _terminate_group(signal.SIGTERM)

    threads = [
        threading.Thread(target=_drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=max(int(timeout_seconds), 1))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_group(signal.SIGKILL)
            process.wait(timeout=5)
    finally:
        for thread in threads:
            thread.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if timed_out:
        raise TourHostSafetyError("tour_import_subprocess_timeout")
    if output_exceeded.is_set():
        raise TourHostSafetyError("tour_import_subprocess_output_limit")
    return BoundedProcessResult(
        returncode=int(process.returncode or 0),
        stdout=bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
    )
