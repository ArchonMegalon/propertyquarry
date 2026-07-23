#!/usr/bin/env python3
"""Verify and forward one caller-supplied, repository-bound GitHub token."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import resource
import select
import ssl
import stat
import sys
import time
from typing import Any, Callable, Mapping


API_HOST = "api.github.com"
API_VERSION = "2026-03-10"
REPOSITORY = "ArchonMegalon/propertyquarry"
REPOSITORY_ID = 1_257_593_732
REPOSITORY_OWNER_ID = 11_421_547
IMMUTABLE_SUBJECT_PREFIX = (
    f"repo:ArchonMegalon@{REPOSITORY_OWNER_ID}"
    f"/propertyquarry@{REPOSITORY_ID}"
)
TOKEN_FD = 8
GATE_FD = 7
STATUS_FD = 9
CALLER_UID = 1000
CALLER_GID = 1000
MAXIMUM_TOKEN_BYTES = 4096
MAXIMUM_RESPONSE_BYTES = 1024 * 1024
TOKEN_PATTERN = re.compile(rb"^github_pat_[A-Za-z0-9_]{20,246}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
PERMISSION_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*=(read|write)$")
PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


class CredentialRejected(Exception):
    """A deliberately detail-free credential admission failure."""


def reject() -> None:
    raise CredentialRejected


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
    except (CredentialRejected, OSError, ValueError):
        reject()


def require_fifo_fd(
    descriptor: int,
    *,
    access_mode: int | None,
    uid: int = CALLER_UID,
    gid: int = CALLER_GID,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError:
        reject()
    if (
        not stat.S_ISFIFO(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or metadata.st_nlink != 1
        or (access_mode is not None and flags & os.O_ACCMODE != access_mode)
    ):
        reject()
    return metadata


def read_exact_gate(descriptor: int) -> None:
    expected = b"verify\n"
    received = bytearray()
    try:
        while len(received) < len(expected):
            chunk = os.read(descriptor, len(expected) - len(received))
            if not chunk:
                reject()
            received.extend(chunk)
        if received != expected:
            reject()
    finally:
        for index in range(len(received)):
            received[index] = 0


def read_token_fd(descriptor: int, timeout_seconds: float = 15.0) -> bytearray:
    require_fifo_fd(descriptor, access_mode=os.O_RDONLY)
    deadline = time.monotonic() + timeout_seconds
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reject()
            readable, _, _ = select.select([descriptor], [], [], remaining)
            if not readable:
                reject()
            chunk = os.read(descriptor, min(1024, MAXIMUM_TOKEN_BYTES + 2 - len(raw)))
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


def strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result

    def invalid_number(_: str) -> Any:
        reject()

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_float=invalid_number,
            parse_constant=invalid_number,
        )
    except (CredentialRejected, UnicodeDecodeError, json.JSONDecodeError):
        reject()


def normalized_headers(
    values: list[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values:
        key = name.strip().lower()
        if (
            not key
            or key in result
            or "\r" in value
            or "\n" in value
        ):
            reject()
        result[key] = value.strip()
    return result


def github_request(
    token: bytearray, path: str
) -> tuple[Any, Mapping[str, str]]:
    if (
        not TOKEN_PATTERN.fullmatch(token)
        or not path.startswith("/")
        or "://" in path
        or "\r" in path
        or "\n" in path
    ):
        reject()
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(
        API_HOST,
        port=443,
        timeout=10,
        context=context,
    )
    authorization = bytearray(b"Bearer ")
    authorization.extend(token)
    try:
        connection.putrequest(
            "GET",
            path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", API_HOST)
        connection.putheader("Accept", "application/vnd.github+json")
        connection.putheader("Authorization", authorization)
        connection.putheader("User-Agent", "propertyquarry-release-credential-v2")
        connection.putheader("X-GitHub-Api-Version", API_VERSION)
        connection.endheaders()
        response = connection.getresponse()
        headers = normalized_headers(response.getheaders())
        length = headers.get("content-length")
        if length is not None:
            try:
                parsed_length = int(length, 10)
            except ValueError:
                reject()
            if parsed_length < 0 or parsed_length > MAXIMUM_RESPONSE_BYTES:
                reject()
        raw = response.read(MAXIMUM_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > MAXIMUM_RESPONSE_BYTES:
            reject()
        return strict_json(raw), headers
    except (
        CredentialRejected,
        OSError,
        ssl.SSLError,
        http.client.HTTPException,
        ValueError,
    ):
        reject()
    finally:
        for index in range(len(authorization)):
            authorization[index] = 0
        connection.close()


def require_permission_headers(
    headers: Mapping[str, str],
    expected: Mapping[str, str],
) -> None:
    if headers.get("x-oauth-scopes", ""):
        reject()
    raw = headers.get("x-accepted-github-permissions")
    if raw is None or not raw:
        reject()
    actual: dict[str, str] = {}
    for item in raw.split(","):
        normalized = item.strip().lower()
        if not PERMISSION_PATTERN.fullmatch(normalized):
            reject()
        key, value = normalized.split("=", 1)
        if key in actual:
            reject()
        actual[key] = value
    if actual != dict(expected):
        reject()


def exact_positive_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        reject()
    return value


Request = Callable[[bytearray, str], tuple[Any, Mapping[str, str]]]


def verify_github_credential(
    token: bytearray,
    request: Request = github_request,
) -> None:
    if not TOKEN_PATTERN.fullmatch(token):
        reject()
    repository_path = f"/repos/{REPOSITORY}"
    repository, headers = request(token, repository_path)
    require_permission_headers(headers, {"metadata": "read"})
    owner = repository.get("owner") if isinstance(repository, dict) else None
    if (
        not isinstance(repository, dict)
        or repository.get("full_name") != REPOSITORY
        or exact_positive_integer(repository.get("id")) != REPOSITORY_ID
        or not isinstance(owner, dict)
        or exact_positive_integer(owner.get("id")) != REPOSITORY_OWNER_ID
    ):
        reject()

    repositories_path = (
        "/user/repos?affiliation=owner%2Ccollaborator%2Corganization_member"
        "&per_page=100"
    )
    repositories, headers = request(token, repositories_path)
    require_permission_headers(headers, {"metadata": "read"})
    if 'rel="next"' in headers.get("link", "").lower():
        reject()
    if not isinstance(repositories, list) or len(repositories) != 1:
        reject()
    selected = repositories[0]
    selected_owner = selected.get("owner") if isinstance(selected, dict) else None
    if (
        not isinstance(selected, dict)
        or selected.get("full_name") != REPOSITORY
        or exact_positive_integer(selected.get("id")) != REPOSITORY_ID
        or not isinstance(selected_owner, dict)
        or exact_positive_integer(selected_owner.get("id"))
        != REPOSITORY_OWNER_ID
    ):
        reject()

    runners, headers = request(
        token,
        f"/repos/{REPOSITORY}/actions/runners?per_page=1",
    )
    require_permission_headers(headers, {"administration": "read"})
    if not isinstance(runners, dict):
        reject()
    total = runners.get("total_count")
    entries = runners.get("runners")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or not isinstance(entries, list)
        or len(entries) > 1
        or len(entries) > total
    ):
        reject()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or exact_positive_integer(entry.get("id")) < 1
            or not isinstance(entry.get("name"), str)
            or not 1 <= len(entry["name"]) <= 256
            or entry["name"].strip() != entry["name"]
        ):
            reject()

    oidc, headers = request(
        token,
        f"/repos/{REPOSITORY}/actions/oidc/customization/sub",
    )
    require_permission_headers(headers, {"actions": "read"})
    if (
        not isinstance(oidc, dict)
        or oidc.get("use_default") is not True
        or oidc.get("use_immutable_subject") is not True
        or oidc.get("sub_claim_prefix") != IMMUTABLE_SUBJECT_PREFIX
    ):
        reject()


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
    timeout_seconds: float = 30.0,
) -> None:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != Path(os.path.normpath(path)):
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
                os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
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
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        ):
            reject()
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        write_all(descriptor, token)
        write_all(descriptor, b"\n")
    finally:
        os.close(descriptor)


def credential_instance_digest(token: bytearray) -> str:
    result = "sha256:" + hashlib.sha256(token).hexdigest()
    if not DIGEST_PATTERN.fullmatch(result):
        reject()
    return result


def verify_and_publish(
    token: bytearray,
    status_fd: int,
    credential_fifo: str,
    *,
    verifier: Callable[[bytearray], None] = verify_github_credential,
    writer: Callable[[str, bytearray], None] = write_token_fifo,
) -> str:
    verifier(token)
    instance_digest = credential_instance_digest(token)
    write_all(
        status_fd,
        f"credential-instance-sha256={instance_digest}\n".encode("ascii"),
    )
    writer(credential_fifo, token)
    return instance_digest


def execute(
    token_fd: int,
    gate_fd: int,
    status_fd: int,
    credential_fifo: str,
) -> None:
    disable_process_dumps()
    if (
        token_fd != TOKEN_FD
        or gate_fd != GATE_FD
        or status_fd != STATUS_FD
        or len({token_fd, gate_fd, status_fd}) != 3
    ):
        reject()
    require_fifo_fd(gate_fd, access_mode=os.O_RDWR)
    require_fifo_fd(status_fd, access_mode=os.O_RDWR)
    require_fifo_fd(token_fd, access_mode=os.O_RDONLY)
    read_exact_gate(gate_fd)
    token = read_token_fd(token_fd)
    try:
        verify_and_publish(token, status_fd, credential_fifo)
    finally:
        for index in range(len(token)):
            token[index] = 0


def failure_status(descriptor: int) -> None:
    try:
        os.write(descriptor, b"credential-verification-rejected\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--token-fd", required=True, type=int)
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--status-fd", required=True, type=int)
    parser.add_argument("--credential-fifo", required=True)
    arguments = parser.parse_args()
    try:
        execute(
            arguments.token_fd,
            arguments.gate_fd,
            arguments.status_fd,
            arguments.credential_fifo,
        )
    except BaseException:
        failure_status(arguments.status_fd)
        return 50
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
