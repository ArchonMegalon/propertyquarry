#!/usr/bin/env python3
"""Fail-closed, local-only PropertyQuarry candidate/runtime binding gate.

This module deliberately does not perform a build, start a container, mutate Git,
or make a public release.  It verifies read-only evidence produced by those steps
and emits a new receipt only after two consistent observations of every mutable
boundary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
import posixpath
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence


BUILD_RECEIPT_SCHEMA = "propertyquarry.local_candidate_build.v1"
BINDING_RECEIPT_SCHEMA = "propertyquarry.local_candidate_runtime_binding.v3"
DOCKERFILE_PATH = "ea/Dockerfile.property-web"
RELEASE_MANIFEST_PATH = "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
DEFAULT_ALLOWED_METADATA_PATHS = (
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
    RELEASE_MANIFEST_PATH,
)
REQUIRED_IMAGE_LABELS = (
    "org.opencontainers.image.revision",
    "com.propertyquarry.metadata-envelope",
    "com.propertyquarry.release-manifest-sha256",
)
REQUIRED_CONTAINER_ENV = (
    "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
    "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
    "PROPERTYQUARRY_RELEASE_MANIFEST_SHA256",
)
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_CONSTRUCTION = "docker-save-uncompressed-layers-v1"
TRUSTED_GIT_BIN = "/usr/bin/git"
TRUSTED_DOCKER_BIN = "/usr/bin/docker"
TRUSTED_COMMAND_BINARIES = frozenset({TRUSTED_GIT_BIN, TRUSTED_DOCKER_BIN})
LOCAL_DOCKER_SOCKET = Path("/run/docker.sock")
AUTHORIZED_DATA_VOLUME_DESTINATION = "/var/lib/propertyquarry"
AUTHORIZED_DATA_VOLUME_DRIVER = "local"
_HTTP_HEADER_LIMIT_BYTES = 16_384
_PROCESS_GROUP_CLEANUP_SECONDS = 2.0
_MAX_COMMAND_TIMEOUT_S = 120.0
_MAX_HTTP_TIMEOUT_S = 120.0
_MAX_COMMAND_OUTPUT_BYTES = 16_777_216
_MAX_ARCHIVE_OUTPUT_BYTES = 1_073_741_824
_MAX_VERSION_OUTPUT_BYTES = 1_048_576
_MAX_BUILD_RECEIPT_BYTES = 16_777_216
_MAX_INPUT_FILE_BYTES = 67_108_864
_REQUIRED_MASKED_PATHS = frozenset(
    {
        "/proc/kcore",
        "/proc/keys",
        "/proc/timer_list",
        "/sys/firmware",
    }
)
_REQUIRED_READONLY_PATHS = frozenset(
    {
        "/proc/bus",
        "/proc/fs",
        "/proc/irq",
        "/proc/sys",
        "/proc/sysrq-trigger",
    }
)

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BARE_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_REPLICA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VOLUME_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REPO_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_PORT_KEY_RE = re.compile(r"([1-9][0-9]{0,4})/tcp\Z")
_RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z\Z"
)
_RFC3339_UTC_SECONDS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)


class BindingError(RuntimeError):
    """An intentionally redacted verifier failure.

    The stable code is the only text representation.  Command output, HTTP
    bodies, paths, environment values, and exception messages never cross this
    boundary.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandExecutor(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class VersionResponse:
    status_code: int
    final_url: str
    body: bytes
    peer_ip: str
    peer_port: int


class VersionFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> VersionResponse: ...


@dataclass(frozen=True)
class BindingConfig:
    repo_root: Path
    receipt_root: Path
    source_candidate_sha: str
    metadata_envelope_sha: str
    build_receipt_path: Path
    expected_build_receipt_sha256: str
    image_reference: str
    container_id: str
    expected_replica_id: str
    authorized_data_volume_name: str
    authorized_data_volume_source: str
    expected_host_config_sha256: str
    version_url: str
    receipt_path: Path
    command_timeout_s: float = 20.0
    http_timeout_s: float = 5.0
    max_command_output_bytes: int = 1_048_576
    max_archive_output_bytes: int = 134_217_728
    max_version_output_bytes: int = 65_536
    max_build_receipt_bytes: int = 262_144
    max_input_file_bytes: int = 16_777_216
    allowed_metadata_paths: tuple[str, ...] = DEFAULT_ALLOWED_METADATA_PATHS


@dataclass(frozen=True)
class VerificationResult:
    receipt: Mapping[str, Any]
    receipt_sha256: str


@dataclass(frozen=True)
class _ImageRuntimeContract:
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    shell: tuple[str, ...]
    user: str
    working_directory: str
    healthcheck: Mapping[str, Any]
    environment: Mapping[str, str]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bare_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
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


def _validate_trusted_binary(path: str) -> None:
    if path not in TRUSTED_COMMAND_BINARIES:
        raise BindingError("command_binary_untrusted")
    candidate = Path(path)
    current = Path(candidate.anchor)
    components = candidate.parts[1:]
    try:
        root_metadata = os.lstat(current)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != 0
            or root_metadata.st_mode & 0o022
        ):
            raise BindingError("command_binary_untrusted")
        for index, component in enumerate(components):
            current /= component
            metadata = os.lstat(current)
            final = index == len(components) - 1
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0:
                raise BindingError("command_binary_untrusted")
            if metadata.st_mode & 0o022:
                raise BindingError("command_binary_untrusted")
            if final:
                if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
                    raise BindingError("command_binary_untrusted")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise BindingError("command_binary_untrusted")
    except BindingError:
        raise
    except OSError:
        raise BindingError("command_binary_untrusted") from None


def _validate_local_docker_socket() -> None:
    path = LOCAL_DOCKER_SOCKET
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise BindingError("docker_socket_untrusted")
    current = Path(path.anchor)
    try:
        root_metadata = os.lstat(current)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != 0
            or root_metadata.st_mode & 0o022
        ):
            raise BindingError("docker_socket_untrusted")
        components = path.parts[1:]
        for index, component in enumerate(components):
            current /= component
            metadata = os.lstat(current)
            final = index == len(components) - 1
            if stat.S_ISLNK(metadata.st_mode):
                raise BindingError("docker_socket_untrusted")
            if final:
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid not in {0, os.geteuid()}
                    or metadata.st_mode & 0o002
                ):
                    raise BindingError("docker_socket_untrusted")
                continue
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
                raise BindingError("docker_socket_untrusted")
            writable_by_others = bool(metadata.st_mode & 0o022)
            trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if writable_by_others and not trusted_sticky:
                raise BindingError("docker_socket_untrusted")
    except BindingError:
        raise
    except OSError:
        raise BindingError("docker_socket_untrusted") from None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        raise BindingError("command_cleanup_failed") from None


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _PROCESS_GROUP_CLEANUP_SECONDS
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        raise BindingError("command_cleanup_failed") from None
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        raise BindingError("command_cleanup_failed") from None
    while _process_group_exists(process.pid):
        if time.monotonic() >= deadline:
            raise BindingError("command_cleanup_failed")
        time.sleep(0.01)


class BoundedSubprocessExecutor:
    """Runs a command without a shell or inherited environment.

    Both pipes are drained concurrently and the process is killed as soon as the
    combined output limit or deadline is exceeded.
    """

    _ENVIRONMENT = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "",
        "GIT_CONFIG": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CommandResult:
        if not argv or timeout_s <= 0 or max_output_bytes <= 0:
            raise BindingError("invalid_command_contract")
        _validate_trusted_binary(argv[0])
        try:
            process = subprocess.Popen(
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=self._ENVIRONMENT,
                shell=False,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
        except (OSError, ValueError):
            raise BindingError("command_launch_failed") from None

        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        buffers: dict[int, bytearray] = {
            process.stdout.fileno(): bytearray(),
            process.stderr.fileno(): bytearray(),
        }
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_s
        total = 0
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_process(process)
                    raise BindingError("command_timeout")
                events = selector.select(min(remaining, 0.25))
                if not events and process.poll() is not None:
                    events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, min(65_536, max_output_bytes - total + 1))
                    except OSError:
                        _kill_process(process)
                        raise BindingError("command_read_failed") from None
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_output_bytes:
                        _kill_process(process)
                        raise BindingError("command_output_limit")
                    buffers[key.fd].extend(chunk)
            try:
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _kill_process(process)
                raise BindingError("command_timeout") from None
            if _process_group_exists(process.pid):
                _kill_process(process)
                raise BindingError("command_residual_process_group")
            return CommandResult(
                returncode=returncode,
                stdout=bytes(buffers[process.stdout.fileno()]),
                stderr=bytes(buffers[process.stderr.fileno()]),
            )
        finally:
            try:
                if process.poll() is None or _process_group_exists(process.pid):
                    _kill_process(process)
            finally:
                selector.close()
                process.stdout.close()
                process.stderr.close()


class LocalVersionFetcher:
    def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> VersionResponse:
        address, port = _local_version_endpoint(url)
        deadline = time.monotonic() + timeout_s
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        try:
            _set_socket_deadline(connection, deadline)
            connection.connect((str(address), port))
            peer = connection.getpeername()
            peer_address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
            peer_port = int(peer[1])
            if peer_address != address or not peer_address.is_loopback or peer_port != port:
                raise BindingError("version_peer_mismatch")

            host_header = f"[{address}]" if address.version == 6 else str(address)
            request = (
                "GET /version HTTP/1.1\r\n"
                f"Host: {host_header}:{port}\r\n"
                "Accept: application/json\r\n"
                "User-Agent: pq-local-binding/1\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            offset = 0
            while offset < len(request):
                _set_socket_deadline(connection, deadline)
                written = connection.send(request[offset:])
                if written <= 0:
                    raise BindingError("version_fetch_failed")
                offset += written

            response = bytearray()
            header_end = -1
            while header_end < 0:
                if len(response) > _HTTP_HEADER_LIMIT_BYTES:
                    raise BindingError("invalid_version_http_response")
                _set_socket_deadline(connection, deadline)
                chunk = connection.recv(4096)
                if not chunk:
                    raise BindingError("invalid_version_http_response")
                response.extend(chunk)
                header_end = response.find(b"\r\n\r\n")
            if header_end > _HTTP_HEADER_LIMIT_BYTES:
                raise BindingError("invalid_version_http_response")

            header_block = bytes(response[:header_end])
            body = bytearray(response[header_end + 4 :])
            status_code, content_length = _parse_http_response_headers(header_block)
            if content_length > max_output_bytes or len(body) > max_output_bytes:
                raise BindingError("version_output_limit")
            if len(body) > content_length:
                raise BindingError("invalid_version_http_response")
            while len(body) < content_length:
                _set_socket_deadline(connection, deadline)
                chunk = connection.recv(min(65_536, content_length - len(body)))
                if not chunk:
                    raise BindingError("version_read_failed")
                body.extend(chunk)
            _set_socket_deadline(connection, deadline)
            return VersionResponse(
                status_code=status_code,
                final_url=url,
                body=bytes(body),
                peer_ip=str(peer_address),
                peer_port=peer_port,
            )
        except BindingError:
            raise
        except TimeoutError:
            raise BindingError("version_fetch_timeout") from None
        except (OSError, ValueError):
            raise BindingError("version_fetch_failed") from None
        finally:
            connection.close()


def _set_socket_deadline(connection: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BindingError("version_fetch_timeout")
    connection.settimeout(remaining)


def _parse_http_response_headers(data: bytes) -> tuple[int, int]:
    lines = data.split(b"\r\n")
    status = lines[0].split(b" ", 2) if lines else []
    if (
        len(status) < 2
        or status[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
        or len(status[1]) != 3
        or not status[1].isdigit()
    ):
        raise BindingError("invalid_version_http_response")
    content_lengths: list[int] = []
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise BindingError("invalid_version_http_response")
        name, value = line.split(b":", 1)
        try:
            normalized_name = name.decode("ascii", errors="strict").lower()
            normalized_value = value.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            raise BindingError("invalid_version_http_response") from None
        if normalized_name == "transfer-encoding":
            raise BindingError("invalid_version_http_response")
        if normalized_name == "content-length":
            if not normalized_value.isdigit():
                raise BindingError("invalid_version_http_response")
            content_lengths.append(int(normalized_value))
    if len(content_lengths) != 1:
        raise BindingError("invalid_version_http_response")
    return int(status[1]), content_lengths[0]


def _validate_receipt_root(root: Path) -> Path:
    if not root.is_absolute():
        raise BindingError("receipt_root_invalid")
    lexical_root = Path(os.path.abspath(root))
    if root != lexical_root:
        raise BindingError("receipt_root_invalid")
    if lexical_root == Path(lexical_root.anchor):
        raise BindingError("receipt_root_invalid")
    current = Path(lexical_root.anchor)
    try:
        components = lexical_root.parts[1:]
        for index, component in enumerate(components):
            current /= component
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BindingError("receipt_root_invalid")
            final = index == len(components) - 1
            if final:
                if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                    raise BindingError("receipt_root_invalid")
            else:
                if metadata.st_uid not in {0, os.geteuid()}:
                    raise BindingError("receipt_root_invalid")
                writable_by_others = bool(metadata.st_mode & 0o022)
                trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
                if writable_by_others and not trusted_sticky:
                    raise BindingError("receipt_root_invalid")
    except BindingError:
        raise
    except OSError:
        raise BindingError("receipt_root_invalid") from None
    return lexical_root


def _validate_receipt_child(path: Path, root: Path, error_code: str) -> None:
    lexical_path = Path(os.path.abspath(path))
    if not path.is_absolute() or path != lexical_path or lexical_path.parent != root:
        raise BindingError(error_code)
    if path.name in {"", ".", ".."} or "/" in path.name or "\x00" in path.name:
        raise BindingError(error_code)


def _validate_authorized_data_volume(config: BindingConfig) -> None:
    name = config.authorized_data_volume_name
    source = config.authorized_data_volume_source
    if not _VOLUME_NAME_RE.fullmatch(name):
        raise BindingError("invalid_authorized_data_volume")
    if (
        not source.startswith("/")
        or source.startswith("//")
        or posixpath.normpath(source) != source
        or any(ord(character) < 32 or ord(character) == 127 for character in source)
    ):
        raise BindingError("invalid_authorized_data_volume")
    parts = PurePosixPath(source).parts
    if len(parts) < 3 or parts[-1] != "_data" or parts[-2] != name:
        raise BindingError("invalid_authorized_data_volume")


def _validate_config(config: BindingConfig) -> None:
    if not _FULL_SHA_RE.fullmatch(config.source_candidate_sha):
        raise BindingError("invalid_source_candidate_sha")
    if not _FULL_SHA_RE.fullmatch(config.metadata_envelope_sha):
        raise BindingError("invalid_metadata_envelope_sha")
    if not _DIGEST_RE.fullmatch(config.expected_build_receipt_sha256):
        raise BindingError("invalid_expected_build_receipt_sha256")
    if not _CONTAINER_ID_RE.fullmatch(config.container_id):
        raise BindingError("invalid_container_id")
    if not _REPLICA_RE.fullmatch(config.expected_replica_id):
        raise BindingError("invalid_replica_id")
    if not _DIGEST_RE.fullmatch(config.image_reference):
        raise BindingError("invalid_image_reference")
    if not _DIGEST_RE.fullmatch(config.expected_host_config_sha256):
        raise BindingError("invalid_expected_host_config_sha256")
    _validate_authorized_data_volume(config)
    timeout_limits = (
        (config.command_timeout_s, _MAX_COMMAND_TIMEOUT_S),
        (config.http_timeout_s, _MAX_HTTP_TIMEOUT_S),
    )
    byte_limits = (
        (config.max_command_output_bytes, _MAX_COMMAND_OUTPUT_BYTES),
        (config.max_archive_output_bytes, _MAX_ARCHIVE_OUTPUT_BYTES),
        (config.max_version_output_bytes, _MAX_VERSION_OUTPUT_BYTES),
        (config.max_build_receipt_bytes, _MAX_BUILD_RECEIPT_BYTES),
        (config.max_input_file_bytes, _MAX_INPUT_FILE_BYTES),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or isinstance(value, float) and not math.isfinite(value)
        or value <= 0
        or value > maximum
        for value, maximum in timeout_limits
    ) or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
        for value, maximum in byte_limits
    ):
        raise BindingError("invalid_resource_limit")
    _validate_trusted_binary(TRUSTED_GIT_BIN)
    _validate_trusted_binary(TRUSTED_DOCKER_BIN)
    _validate_local_docker_socket()
    try:
        repo_root = config.repo_root.resolve(strict=True)
    except OSError:
        raise BindingError("invalid_repo_root") from None
    if not repo_root.is_dir():
        raise BindingError("invalid_repo_root")
    receipt_root = _validate_receipt_root(config.receipt_root)
    _validate_receipt_child(config.build_receipt_path, receipt_root, "build_receipt_outside_root")
    _validate_receipt_child(config.receipt_path, receipt_root, "receipt_outside_root")
    if config.build_receipt_path.name == config.receipt_path.name:
        raise BindingError("receipt_paths_collide")
    allowed = tuple(config.allowed_metadata_paths)
    if not allowed or len(set(allowed)) != len(allowed):
        raise BindingError("invalid_metadata_allowlist")
    for path in allowed:
        _validate_repo_relative_path(path, "invalid_metadata_allowlist")
    _local_version_endpoint(config.version_url)


def _local_version_endpoint(url: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise BindingError("invalid_version_url") from None
    if (
        parsed.scheme != "http"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/version"
        or parsed.query
        or parsed.fragment
        or port is None
        or port <= 0
    ):
        raise BindingError("invalid_version_url")
    if "%" in hostname:
        raise BindingError("version_url_not_local")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise BindingError("version_url_not_local") from None
    if not address.is_loopback:
        raise BindingError("version_url_not_local")
    return address, port


def _validate_repo_relative_path(path: str, error_code: str) -> None:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or str(candidate) != path
    ):
        raise BindingError(error_code)


def _read_stable_regular_file(path: Path, *, max_bytes: int, error_code: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BindingError(error_code) from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise BindingError(error_code)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BindingError(error_code)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise BindingError(error_code)
        return b"".join(chunks)
    except BindingError:
        raise
    except OSError:
        raise BindingError(error_code) from None
    finally:
        os.close(descriptor)


def _read_repo_file(
    repo_root: Path,
    relative_path: str,
    *,
    max_bytes: int,
) -> bytes:
    _validate_repo_relative_path(relative_path, "invalid_build_input_path")
    try:
        root = repo_root.resolve(strict=True)
        resolved = (root / relative_path).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise BindingError("build_input_outside_repo") from None
    return _read_stable_regular_file(
        resolved,
        max_bytes=max_bytes,
        error_code="build_input_unreadable",
    )


def _strict_json(data: bytes, *, error_code: str) -> Any:
    duplicate = False

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        decoded = data.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise BindingError(error_code) from None
    if duplicate:
        raise BindingError(error_code)
    return value


def _exact_keys(value: Any, keys: set[str], error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BindingError(error_code)
    return value


def _string(value: Any, error_code: str) -> str:
    if not isinstance(value, str):
        raise BindingError(error_code)
    return value


def _string_list(value: Any, error_code: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BindingError(error_code)
    return value


def _load_and_validate_build_receipt(
    config: BindingConfig,
) -> tuple[Mapping[str, Any], bytes]:
    raw = _read_stable_regular_file(
        config.build_receipt_path,
        max_bytes=config.max_build_receipt_bytes,
        error_code="build_receipt_unreadable",
    )
    if _sha256(raw) != config.expected_build_receipt_sha256:
        raise BindingError("build_receipt_hash_mismatch")
    value = _strict_json(raw, error_code="invalid_build_receipt_json")
    if _canonical_json_bytes(value) != raw:
        raise BindingError("build_receipt_not_canonical")
    receipt = _exact_keys(
        value,
        {
            "schema",
            "generated_at",
            "source_candidate_sha",
            "metadata_envelope_sha",
            "source_candidate",
            "metadata_envelope",
            "dockerfile",
            "release_manifest",
            "base_images",
            "local_build",
            "image_reference",
            "oci_manifest_digest",
            "image_config_id",
            "local_oci_manifest",
            "labels",
            "authority",
        },
        "invalid_build_receipt_schema",
    )
    if receipt["schema"] != BUILD_RECEIPT_SCHEMA:
        raise BindingError("invalid_build_receipt_schema")
    if receipt["source_candidate_sha"] != config.source_candidate_sha:
        raise BindingError("build_candidate_mismatch")
    if receipt["metadata_envelope_sha"] != config.metadata_envelope_sha:
        raise BindingError("build_envelope_mismatch")
    if receipt["image_reference"] != config.image_reference:
        raise BindingError("build_image_reference_mismatch")
    generated_at = _string(receipt["generated_at"], "invalid_build_receipt_timestamp")
    if not _RFC3339_UTC_SECONDS_RE.fullmatch(generated_at):
        raise BindingError("invalid_build_receipt_timestamp")

    source = _exact_keys(
        receipt["source_candidate"],
        {"tree_sha", "archive_sha256"},
        "invalid_build_source_evidence",
    )
    envelope = _exact_keys(
        receipt["metadata_envelope"],
        {"tree_sha", "archive_sha256", "changed_paths"},
        "invalid_build_envelope_evidence",
    )
    for evidence in (source, envelope):
        if not _FULL_SHA_RE.fullmatch(_string(evidence["tree_sha"], "invalid_build_tree_sha")):
            raise BindingError("invalid_build_tree_sha")
        if not _DIGEST_RE.fullmatch(
            _string(evidence["archive_sha256"], "invalid_build_archive_sha256")
        ):
            raise BindingError("invalid_build_archive_sha256")
    changed_paths = _string_list(
        envelope["changed_paths"], "invalid_build_envelope_evidence"
    )
    if changed_paths != sorted(set(changed_paths)):
        raise BindingError("invalid_build_envelope_evidence")
    for path in changed_paths:
        _validate_repo_relative_path(path, "invalid_build_envelope_evidence")
    if set(changed_paths).difference(config.allowed_metadata_paths):
        raise BindingError("invalid_build_envelope_evidence")

    dockerfile = _exact_keys(
        receipt["dockerfile"], {"path", "sha256"}, "invalid_build_dockerfile_evidence"
    )
    if dockerfile["path"] != DOCKERFILE_PATH or not _DIGEST_RE.fullmatch(
        _string(dockerfile["sha256"], "invalid_build_dockerfile_evidence")
    ):
        raise BindingError("invalid_build_dockerfile_evidence")

    release_manifest = _exact_keys(
        receipt["release_manifest"],
        {"path", "file_sha256", "manifest_sha256"},
        "invalid_build_manifest_evidence",
    )
    if release_manifest["path"] != RELEASE_MANIFEST_PATH:
        raise BindingError("invalid_build_manifest_evidence")
    if not _DIGEST_RE.fullmatch(
        _string(release_manifest["file_sha256"], "invalid_build_manifest_evidence")
    ) or not _BARE_SHA256_RE.fullmatch(
        _string(release_manifest["manifest_sha256"], "invalid_build_manifest_evidence")
    ):
        raise BindingError("invalid_build_manifest_evidence")

    manifest_sha = release_manifest["manifest_sha256"]
    labels = _exact_keys(receipt["labels"], set(REQUIRED_IMAGE_LABELS), "invalid_build_labels")
    expected_labels = {
        "org.opencontainers.image.revision": config.source_candidate_sha,
        "com.propertyquarry.metadata-envelope": config.metadata_envelope_sha,
        "com.propertyquarry.release-manifest-sha256": manifest_sha,
    }
    if labels != expected_labels:
        raise BindingError("build_label_mismatch")

    authority = _exact_keys(
        receipt["authority"],
        {
            "local_only",
            "performs_local_docker_build",
            "authoritative_for_release_effects",
            "public_launch_authority",
            "production_ready",
        },
        "invalid_build_authority",
    )
    if authority != {
        "local_only": True,
        "performs_local_docker_build": True,
        "authoritative_for_release_effects": False,
        "public_launch_authority": False,
        "production_ready": False,
    }:
        raise BindingError("invalid_build_authority")

    local_build = _exact_keys(
        receipt["local_build"],
        {
            "image_tag",
            "platform",
            "network_mode",
            "pull",
            "docker_daemon_id_sha256",
        },
        "invalid_local_build_evidence",
    )
    image_tag = _string(local_build["image_tag"], "invalid_local_build_evidence")
    if (
        not image_tag.startswith("propertyquarry-local-candidate:")
        or len(image_tag) > 159
        or any(character.isspace() or ord(character) < 33 for character in image_tag)
        or local_build["platform"] != "linux/amd64"
        or local_build["network_mode"] != "none"
        or local_build["pull"] is not False
        or not _DIGEST_RE.fullmatch(
            _string(
                local_build["docker_daemon_id_sha256"],
                "invalid_local_build_evidence",
            )
        )
    ):
        raise BindingError("invalid_local_build_evidence")

    bases = receipt["base_images"]
    if not isinstance(bases, list) or not bases:
        raise BindingError("invalid_base_image_evidence")
    seen_bases: set[str] = set()
    for value in bases:
        base = _exact_keys(
            value,
            {"reference", "image_config_id", "observed_repo_digest"},
            "invalid_base_image_evidence",
        )
        reference = _string(base["reference"], "invalid_base_image_evidence")
        image_config = _string(
            base["image_config_id"], "invalid_base_image_evidence"
        )
        observed_digest = _string(
            base["observed_repo_digest"], "invalid_base_image_evidence"
        )
        if (
            reference in seen_bases
            or "@" not in reference
            or not _DIGEST_RE.fullmatch(reference.rsplit("@", 1)[-1])
            or not _DIGEST_RE.fullmatch(image_config)
            or not _REPO_DIGEST_RE.fullmatch(observed_digest)
            or observed_digest.rsplit("@", 1)[-1] != reference.rsplit("@", 1)[-1]
        ):
            raise BindingError("invalid_base_image_evidence")
        seen_bases.add(reference)

    digest = _string(receipt["oci_manifest_digest"], "invalid_oci_manifest_digest")
    image_id = _string(receipt["image_config_id"], "invalid_image_config_id")
    if not _DIGEST_RE.fullmatch(digest):
        raise BindingError("invalid_oci_manifest_digest")
    if not _DIGEST_RE.fullmatch(image_id):
        raise BindingError("invalid_image_config_id")
    if digest == image_id:
        raise BindingError("oci_digest_equals_image_config_id")

    local_oci = _exact_keys(
        receipt["local_oci_manifest"],
        {
            "construction",
            "docker_archive_sha256",
            "media_type",
            "manifest_size",
            "config",
            "layers",
        },
        "invalid_local_oci_evidence",
    )
    if (
        local_oci["construction"] != OCI_CONSTRUCTION
        or local_oci["media_type"] != OCI_MANIFEST_MEDIA_TYPE
        or not _DIGEST_RE.fullmatch(
            _string(
                local_oci["docker_archive_sha256"],
                "invalid_local_oci_evidence",
            )
        )
    ):
        raise BindingError("invalid_local_oci_evidence")
    manifest_size = local_oci["manifest_size"]
    if isinstance(manifest_size, bool) or not isinstance(manifest_size, int) or manifest_size <= 0:
        raise BindingError("invalid_local_oci_evidence")
    local_config = _exact_keys(
        local_oci["config"],
        {"media_type", "digest", "size"},
        "invalid_local_oci_evidence",
    )
    config_size = local_config["size"]
    if (
        local_config["media_type"] != OCI_CONFIG_MEDIA_TYPE
        or local_config["digest"] != image_id
        or isinstance(config_size, bool)
        or not isinstance(config_size, int)
        or config_size <= 0
    ):
        raise BindingError("invalid_local_oci_evidence")
    local_layers = local_oci["layers"]
    if not isinstance(local_layers, list) or not local_layers:
        raise BindingError("invalid_local_oci_evidence")
    normalized_layers: list[Mapping[str, Any]] = []
    layer_digests: set[str] = set()
    for value in local_layers:
        layer = _exact_keys(
            value,
            {"media_type", "digest", "size"},
            "invalid_local_oci_evidence",
        )
        layer_digest = _string(layer["digest"], "invalid_local_oci_evidence")
        layer_size = layer["size"]
        if (
            layer["media_type"] != OCI_LAYER_MEDIA_TYPE
            or not _DIGEST_RE.fullmatch(layer_digest)
            or layer_digest in layer_digests
            or isinstance(layer_size, bool)
            or not isinstance(layer_size, int)
            or layer_size <= 0
        ):
            raise BindingError("invalid_local_oci_evidence")
        layer_digests.add(layer_digest)
        normalized_layers.append(layer)
    reconstructed_manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "digest": image_id,
            "size": config_size,
        },
        "layers": [
            {
                "mediaType": OCI_LAYER_MEDIA_TYPE,
                "digest": layer["digest"],
                "size": layer["size"],
            }
            for layer in normalized_layers
        ],
    }
    reconstructed_raw = _canonical_json_bytes(reconstructed_manifest)[:-1]
    if len(reconstructed_raw) != manifest_size or _sha256(reconstructed_raw) != digest:
        raise BindingError("local_oci_manifest_mismatch")

    dockerfile_raw = _read_repo_file(
        config.repo_root, DOCKERFILE_PATH, max_bytes=config.max_input_file_bytes
    )
    manifest_raw = _read_repo_file(
        config.repo_root, RELEASE_MANIFEST_PATH, max_bytes=config.max_input_file_bytes
    )
    if _sha256(dockerfile_raw) != dockerfile["sha256"]:
        raise BindingError("dockerfile_hash_mismatch")
    if _sha256(manifest_raw) != release_manifest["file_sha256"]:
        raise BindingError("release_manifest_file_hash_mismatch")
    return receipt, raw


class _Evidence:
    def __init__(self, config: BindingConfig, executor: CommandExecutor) -> None:
        self.config = config
        self.executor = executor

    def _run(
        self,
        argv: Sequence[str],
        *,
        error_code: str,
        max_output_bytes: int | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> CommandResult:
        limit = max_output_bytes or self.config.max_command_output_bytes
        try:
            result = self.executor.run(
                argv,
                timeout_s=self.config.command_timeout_s,
                max_output_bytes=limit,
            )
        except BindingError:
            raise
        except Exception:
            raise BindingError("command_execution_failed") from None
        if (
            not isinstance(result, CommandResult)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or not isinstance(result.returncode, int)
        ):
            raise BindingError("invalid_command_result")
        if len(result.stdout) + len(result.stderr) > limit:
            raise BindingError("command_output_limit")
        if result.returncode not in allowed_returncodes:
            raise BindingError(error_code)
        return result

    def git(
        self,
        *arguments: str,
        error_code: str,
        max_output_bytes: int | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> CommandResult:
        return self._run(
            (
                TRUSTED_GIT_BIN,
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.attributesFile=/dev/null",
                "-C",
                str(self.config.repo_root.resolve()),
                *arguments,
            ),
            error_code=error_code,
            max_output_bytes=max_output_bytes,
            allowed_returncodes=allowed_returncodes,
        )

    def docker(self, *arguments: str, error_code: str) -> bytes:
        return self._run(
            (
                TRUSTED_DOCKER_BIN,
                "--host",
                f"unix://{LOCAL_DOCKER_SOCKET}",
                *arguments,
            ),
            error_code=error_code,
        ).stdout


def _one_line_sha(data: bytes, error_code: str) -> str:
    try:
        value = data.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        raise BindingError(error_code) from None
    if not _FULL_SHA_RE.fullmatch(value):
        raise BindingError(error_code)
    return value


def _git_attributes_path(evidence: _Evidence) -> Path:
    raw = evidence.git(
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "info/attributes",
        error_code="git_attributes_path_failed",
    ).stdout
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise BindingError("git_attributes_path_invalid") from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise BindingError("git_attributes_path_invalid")
    path = Path(value[:-1])
    if (
        not path.is_absolute()
        or path != Path(os.path.abspath(path))
        or len(os.fsencode(path)) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in str(path))
    ):
        raise BindingError("git_attributes_path_invalid")
    return path


def _require_no_local_git_attributes(evidence: _Evidence) -> None:
    path = _git_attributes_path(evidence)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise BindingError("git_local_attributes_check_failed") from None
    raise BindingError("git_local_attributes_forbidden")


def _changed_paths(data: bytes) -> tuple[str, ...]:
    if data and not data.endswith(b"\0"):
        raise BindingError("invalid_git_path_output")
    paths: set[str] = set()
    for token in data.split(b"\0"):
        if not token:
            continue
        if b"\n" in token or b"\r" in token:
            raise BindingError("invalid_git_path_output")
        try:
            path = token.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BindingError("invalid_git_path_output") from None
        _validate_repo_relative_path(path, "invalid_git_path_output")
        paths.add(path)
    return tuple(sorted(paths))


def _tree_changed_paths(
    evidence: _Evidence,
    candidate_tree: str,
    envelope_tree: str,
) -> tuple[str, ...]:
    return _changed_paths(
        evidence.git(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            candidate_tree,
            envelope_tree,
            "--",
            error_code="metadata_path_check_failed",
        ).stdout
    )


def _verify_git_binding(
    config: BindingConfig,
    evidence: _Evidence,
    build_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    status = evidence.git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        error_code="git_status_failed",
    ).stdout
    if status:
        raise BindingError("dirty_build_context")

    candidate = config.source_candidate_sha
    envelope = config.metadata_envelope_sha
    evidence.git("cat-file", "-e", candidate + "^{commit}", error_code="candidate_commit_missing")
    evidence.git("cat-file", "-e", envelope + "^{commit}", error_code="envelope_commit_missing")
    ancestor = evidence.git(
        "merge-base",
        "--is-ancestor",
        candidate,
        envelope,
        error_code="git_ancestry_check_failed",
        allowed_returncodes=frozenset({0, 1}),
    )
    if ancestor.returncode != 0:
        raise BindingError("metadata_envelope_not_descendant")

    head = _one_line_sha(
        evidence.git("rev-parse", "HEAD", error_code="git_head_failed").stdout,
        "invalid_git_head",
    )
    if head != envelope:
        raise BindingError("metadata_envelope_not_checked_out")

    candidate_tree = _one_line_sha(
        evidence.git(
            "rev-parse", candidate + "^{tree}", error_code="candidate_tree_failed"
        ).stdout,
        "invalid_candidate_tree",
    )
    envelope_tree = _one_line_sha(
        evidence.git(
            "rev-parse", envelope + "^{tree}", error_code="envelope_tree_failed"
        ).stdout,
        "invalid_envelope_tree",
    )
    changed = _tree_changed_paths(evidence, candidate_tree, envelope_tree)
    forbidden = set(changed).difference(config.allowed_metadata_paths)
    if forbidden:
        raise BindingError("metadata_envelope_contains_source_changes")
    if list(changed) != build_receipt["metadata_envelope"]["changed_paths"]:
        raise BindingError("metadata_envelope_path_evidence_mismatch")

    _require_no_local_git_attributes(evidence)
    candidate_archive = evidence.git(
        "archive",
        "--format=tar",
        candidate,
        error_code="candidate_archive_failed",
        max_output_bytes=config.max_archive_output_bytes,
    ).stdout
    envelope_archive = evidence.git(
        "archive",
        "--format=tar",
        envelope,
        error_code="envelope_archive_failed",
        max_output_bytes=config.max_archive_output_bytes,
    ).stdout
    _require_no_local_git_attributes(evidence)

    source_receipt = build_receipt["source_candidate"]
    envelope_receipt = build_receipt["metadata_envelope"]
    if candidate_tree != source_receipt["tree_sha"]:
        raise BindingError("candidate_tree_mismatch")
    if envelope_tree != envelope_receipt["tree_sha"]:
        raise BindingError("envelope_tree_mismatch")
    if _sha256(candidate_archive) != source_receipt["archive_sha256"]:
        raise BindingError("candidate_archive_hash_mismatch")
    if _sha256(envelope_archive) != envelope_receipt["archive_sha256"]:
        raise BindingError("envelope_archive_hash_mismatch")

    return {
        "head_sha": head,
        "source_candidate_tree_sha": candidate_tree,
        "metadata_envelope_tree_sha": envelope_tree,
        "metadata_paths": list(changed),
        "source_candidate_archive_sha256": _sha256(candidate_archive),
        "metadata_envelope_archive_sha256": _sha256(envelope_archive),
    }


def _docker_inspect_object(data: bytes, error_code: str) -> Mapping[str, Any]:
    value = _strict_json(data, error_code=error_code)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BindingError(error_code)
    return value[0]


def _mapping(value: Any, error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BindingError(error_code)
    return value


def _bool(value: Any, expected: bool, error_code: str) -> None:
    if not isinstance(value, bool) or value is not expected:
        raise BindingError(error_code)


def _integer(value: Any, expected: int, error_code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise BindingError(error_code)


def _validate_image_inspect(
    config: BindingConfig,
    build_receipt: Mapping[str, Any],
    image: Mapping[str, Any],
) -> tuple[Mapping[str, Any], _ImageRuntimeContract]:
    image_id = build_receipt["image_config_id"]
    manifest_digest = build_receipt["oci_manifest_digest"]
    if image.get("Id") != image_id:
        raise BindingError("docker_image_config_id_mismatch")
    image_config = _mapping(image.get("Config"), "invalid_docker_image_config")
    labels = _mapping(image_config.get("Labels"), "invalid_docker_image_labels")
    for key, expected in build_receipt["labels"].items():
        if labels.get(key) != expected:
            raise BindingError("docker_image_label_mismatch")

    repo_tags = _string_list(image.get("RepoTags"), "invalid_docker_repo_tags")
    repo_digests = _string_list(image.get("RepoDigests"), "invalid_docker_repo_digests")
    if config.image_reference != image_id:
        raise BindingError("docker_image_reference_not_immutable_id")
    if any(not _REPO_DIGEST_RE.fullmatch(value) for value in repo_digests):
        raise BindingError("invalid_docker_repo_digests")
    matching_repo_digests = [
        value for value in repo_digests if value.rsplit("@", 1)[-1] == manifest_digest
    ]
    if repo_digests and len(matching_repo_digests) != 1:
        raise BindingError("docker_repo_digest_conflicts_with_local_oci")

    rootfs = _exact_keys(
        image.get("RootFS"), {"Type", "Layers"}, "invalid_docker_image_rootfs"
    )
    rootfs_layers = _string_list(rootfs["Layers"], "invalid_docker_image_rootfs")
    expected_layers = [
        layer["digest"] for layer in build_receipt["local_oci_manifest"]["layers"]
    ]
    if rootfs["Type"] != "layers" or rootfs_layers != expected_layers:
        raise BindingError("docker_image_rootfs_mismatch")

    runtime_contract = _image_runtime_contract(image_config)
    runtime_contract_evidence = _runtime_contract_evidence(runtime_contract)

    selected = {
        "image_config_id": image_id,
        "labels": {key: labels[key] for key in REQUIRED_IMAGE_LABELS},
        "repo_tags": sorted(repo_tags),
        "repo_digests": sorted(repo_digests),
        "observed_repo_digest": (
            matching_repo_digests[0] if matching_repo_digests else None
        ),
        "oci_manifest_digest": manifest_digest,
        "rootfs_diff_ids_sha256": _sha256(
            _canonical_json_bytes(rootfs_layers)[:-1]
        ),
        "runtime_contract_sha256": _fingerprint(runtime_contract_evidence),
    }
    return selected, runtime_contract


def _container_env(value: Any) -> Mapping[str, str]:
    entries = _string_list(value, "invalid_container_environment")
    result: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise BindingError("invalid_container_environment")
        key, item = entry.split("=", 1)
        if not key or key in result:
            raise BindingError("invalid_container_environment")
        result[key] = item
    return result


def _optional_command(value: Any, error_code: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_string_list(value, error_code))


def _image_runtime_contract(image_config: Mapping[str, Any]) -> _ImageRuntimeContract:
    entrypoint = _optional_command(image_config.get("Entrypoint"), "invalid_image_runtime_contract")
    command = _optional_command(image_config.get("Cmd"), "invalid_image_runtime_contract")
    shell = _optional_command(image_config.get("Shell"), "invalid_image_runtime_contract")
    if not entrypoint and not command:
        raise BindingError("invalid_image_runtime_contract")
    healthcheck = _mapping(image_config.get("Healthcheck"), "invalid_image_runtime_contract")
    health_test = _string_list(healthcheck.get("Test"), "invalid_image_runtime_contract")
    if len(health_test) < 2 or health_test[0] not in {"CMD", "CMD-SHELL"}:
        raise BindingError("invalid_image_runtime_contract")
    return _ImageRuntimeContract(
        entrypoint=entrypoint,
        command=command,
        shell=shell,
        user=_string(image_config.get("User"), "invalid_image_runtime_contract"),
        working_directory=_string(
            image_config.get("WorkingDir"), "invalid_image_runtime_contract"
        ),
        healthcheck=healthcheck,
        environment=_container_env(image_config.get("Env")),
    )


def _runtime_contract_evidence(contract: _ImageRuntimeContract) -> Mapping[str, Any]:
    return {
        "entrypoint": list(contract.entrypoint),
        "command": list(contract.command),
        "shell": list(contract.shell),
        "user": contract.user,
        "working_directory": contract.working_directory,
        "healthcheck": contract.healthcheck,
        "environment_sha256": _fingerprint(dict(contract.environment)),
    }


def _mount_replaces_app(destination: str) -> bool:
    if not destination.startswith("/") or destination.startswith("//") or "\x00" in destination:
        return True
    normalized = posixpath.normpath(destination)
    if normalized != destination:
        return True
    return normalized == "/" or normalized == "/app" or normalized.startswith("/app/")


def _authorized_data_mount_evidence(
    config: BindingConfig,
    mount: Mapping[str, Any],
) -> Mapping[str, Any]:
    required_keys = {
        "Type",
        "Name",
        "Source",
        "Destination",
        "Driver",
        "Mode",
        "RW",
        "Propagation",
    }
    if set(mount) != required_keys:
        raise BindingError("invalid_container_mounts")
    identity = {
        "type": _string(mount.get("Type"), "invalid_container_mounts"),
        "name": _string(mount.get("Name"), "invalid_container_mounts"),
        "source": _string(mount.get("Source"), "invalid_container_mounts"),
        "destination": _string(mount.get("Destination"), "invalid_container_mounts"),
        "driver": _string(mount.get("Driver"), "invalid_container_mounts"),
        "mode": _string(mount.get("Mode"), "invalid_container_mounts"),
        "propagation": _string(mount.get("Propagation"), "invalid_container_mounts"),
    }
    read_write = mount.get("RW")
    if not isinstance(read_write, bool):
        raise BindingError("invalid_container_mounts")
    identity["read_only"] = not read_write
    expected = {
        "type": "volume",
        "name": config.authorized_data_volume_name,
        "source": config.authorized_data_volume_source,
        "destination": AUTHORIZED_DATA_VOLUME_DESTINATION,
        "driver": AUTHORIZED_DATA_VOLUME_DRIVER,
        "mode": "rw",
        "propagation": "",
        "read_only": False,
    }
    if identity != expected:
        raise BindingError("authorized_data_volume_identity_mismatch")
    return {
        "type": identity["type"],
        "name": identity["name"],
        "destination": identity["destination"],
        "driver": identity["driver"],
        "read_only": identity["read_only"],
        "source_sha256": _sha256(identity["source"].encode("utf-8")),
        "identity_sha256": _fingerprint(identity),
    }


def _port_bindings(value: Any, error_code: str) -> Mapping[str, tuple[tuple[str, int], ...]]:
    mapping = _mapping(value, error_code)
    result: dict[str, tuple[tuple[str, int], ...]] = {}
    for port_key, bindings_value in mapping.items():
        if not isinstance(port_key, str):
            raise BindingError(error_code)
        match = _PORT_KEY_RE.fullmatch(port_key)
        if match is None or int(match.group(1)) > 65_535:
            raise BindingError(error_code)
        if bindings_value is None:
            result[port_key] = ()
            continue
        if not isinstance(bindings_value, list):
            raise BindingError(error_code)
        bindings: list[tuple[str, int]] = []
        for binding_value in bindings_value:
            binding = _mapping(binding_value, error_code)
            host_ip = _string(binding.get("HostIp"), error_code)
            host_port_raw = _string(binding.get("HostPort"), error_code)
            try:
                host_address = ipaddress.ip_address(host_ip)
                host_port = int(host_port_raw)
            except ValueError:
                raise BindingError(error_code) from None
            if host_port <= 0 or host_port > 65_535:
                raise BindingError(error_code)
            bindings.append((str(host_address), host_port))
        result[port_key] = tuple(bindings)
    return result


def _closed_host_security(host_config: Mapping[str, Any]) -> Mapping[str, Any]:
    boolean_contract = {
        "Privileged": False,
        "ReadonlyRootfs": True,
        "AutoRemove": False,
        "OomKillDisable": False,
        "Init": True,
        "PublishAllPorts": False,
    }
    string_contract = {
        "PidMode": "",
        "IpcMode": "private",
        "UTSMode": "",
        "UsernsMode": "",
        "CgroupnsMode": "private",
        "Runtime": "runc",
        "Isolation": "",
        "VolumeDriver": "",
        "CgroupParent": "",
        "Cgroup": "",
        "ContainerIDFile": "",
    }
    list_contract: Mapping[str, tuple[str, ...]] = {
        "CapAdd": (),
        "CapDrop": ("ALL",),
        "SecurityOpt": ("no-new-privileges:true",),
        "Binds": (),
        "Links": (),
        "ExtraHosts": (),
        "Dns": (),
        "DnsOptions": (),
        "DnsSearch": (),
        "VolumesFrom": (),
        "GroupAdd": (),
        "DeviceCgroupRules": (),
    }
    selected: dict[str, Any] = {}
    for key, expected in boolean_contract.items():
        _bool(host_config.get(key), expected, "container_host_security_mismatch")
        selected[key] = expected
    for key, expected in string_contract.items():
        value = _string(host_config.get(key), "container_host_security_mismatch")
        if value != expected:
            raise BindingError("container_host_security_mismatch")
        selected[key] = value
    for key, expected in list_contract.items():
        raw_values = host_config.get(key)
        values = (
            []
            if raw_values is None
            else _string_list(raw_values, "container_host_security_mismatch")
        )
        if len(values) != len(set(values)) or tuple(sorted(values)) != tuple(sorted(expected)):
            raise BindingError("container_host_security_mismatch")
        selected[key] = sorted(values)
    for key in ("Devices", "DeviceRequests", "Mounts"):
        value = host_config.get(key)
        if value is not None and (not isinstance(value, list) or value):
            raise BindingError("container_host_security_mismatch")
        selected[key] = []
    for key in ("Tmpfs", "Sysctls", "StorageOpt"):
        value = host_config.get(key)
        if value is not None and (not isinstance(value, dict) or value):
            raise BindingError("container_host_security_mismatch")
        selected[key] = {}
    for key, required in (
        ("MaskedPaths", _REQUIRED_MASKED_PATHS),
        ("ReadonlyPaths", _REQUIRED_READONLY_PATHS),
    ):
        values = _string_list(host_config.get(key), "container_host_security_mismatch")
        if len(values) != len(set(values)) or not required.issubset(values):
            raise BindingError("container_host_security_mismatch")
        selected[key] = sorted(values)
    observed_sha256 = _fingerprint(host_config)
    selected["canonical_sha256"] = observed_sha256
    return selected


def _validate_version_port_binding(
    config: BindingConfig,
    container: Mapping[str, Any],
) -> Mapping[str, Any]:
    address, host_port = _local_version_endpoint(config.version_url)
    host_config = _mapping(container.get("HostConfig"), "invalid_container_host_config")
    network_mode = _string(host_config.get("NetworkMode"), "invalid_container_network_mode")
    if (
        not network_mode
        or network_mode in {"host", "none"}
        or network_mode.startswith("container:")
    ):
        raise BindingError("container_network_mode_unbound")
    configured = _port_bindings(
        host_config.get("PortBindings"), "invalid_container_port_bindings"
    )
    network_settings = _mapping(
        container.get("NetworkSettings"), "invalid_container_network_settings"
    )
    observed = _port_bindings(
        network_settings.get("Ports"), "invalid_container_port_bindings"
    )
    expected_binding = (str(address), host_port)
    if (
        len(configured) != 1
        or len(observed) != 1
        or set(configured) != set(observed)
    ):
        raise BindingError("version_container_port_ambiguous")
    container_port_key = next(iter(configured))
    if (
        configured[container_port_key] != (expected_binding,)
        or observed[container_port_key] != (expected_binding,)
    ):
        raise BindingError("version_container_port_mismatch")

    container_config = _mapping(
        container.get("Config"), "invalid_container_config"
    )
    exposed_ports = _mapping(
        container_config.get("ExposedPorts"), "invalid_container_port_bindings"
    )
    for port_key, metadata in exposed_ports.items():
        if not isinstance(port_key, str):
            raise BindingError("invalid_container_port_bindings")
        match = _PORT_KEY_RE.fullmatch(port_key)
        if (
            match is None
            or int(match.group(1)) > 65_535
            or not isinstance(metadata, dict)
            or metadata
        ):
            raise BindingError("invalid_container_port_bindings")
    if set(exposed_ports) != {container_port_key}:
        raise BindingError("version_container_port_ambiguous")

    networks = _mapping(network_settings.get("Networks"), "invalid_container_networks")
    if len(networks) != 1:
        raise BindingError("container_network_ambiguous")
    network_name, network_value = next(iter(networks.items()))
    if not isinstance(network_name, str) or not network_name:
        raise BindingError("invalid_container_networks")
    expected_network_name = "bridge" if network_mode == "default" else network_mode
    if network_name != expected_network_name:
        raise BindingError("container_network_ambiguous")
    network = _mapping(network_value, "invalid_container_networks")
    network_id = _string(network.get("NetworkID"), "invalid_container_networks")
    endpoint_id = _string(network.get("EndpointID"), "invalid_container_networks")
    if not network_id or not endpoint_id:
        raise BindingError("invalid_container_networks")
    container_addresses: list[str] = []
    for key in ("IPAddress", "GlobalIPv6Address"):
        raw = _string(network.get(key), "invalid_container_networks")
        if raw:
            try:
                parsed = ipaddress.ip_address(raw)
            except ValueError:
                raise BindingError("invalid_container_networks") from None
            if parsed.is_loopback or parsed.is_unspecified:
                raise BindingError("invalid_container_networks")
            container_addresses.append(str(parsed))
    if len(container_addresses) != 1:
        raise BindingError("container_network_ambiguous")
    return {
        "host_ip": str(address),
        "host_port": host_port,
        "container_port": int(container_port_key.split("/", 1)[0]),
        "protocol": "tcp",
        "network_name": network_name,
        "network_id": network_id,
        "endpoint_id": endpoint_id,
        "container_ip": container_addresses[0],
    }


def _validate_container_inspect(
    config: BindingConfig,
    build_receipt: Mapping[str, Any],
    container: Mapping[str, Any],
    image_runtime: _ImageRuntimeContract,
) -> Mapping[str, Any]:
    if container.get("Id") != config.container_id:
        raise BindingError("container_id_mismatch")
    if container.get("Image") != build_receipt["image_config_id"]:
        raise BindingError("container_image_config_id_mismatch")

    state = _mapping(container.get("State"), "invalid_container_state")
    if state.get("Status") != "running":
        raise BindingError("container_not_running")
    _bool(state.get("Running"), True, "container_not_running")
    _bool(state.get("Paused"), False, "container_not_running")
    _bool(state.get("Restarting"), False, "container_restarting")
    _bool(state.get("OOMKilled"), False, "container_oom_killed")
    _bool(state.get("Dead"), False, "container_dead")
    _integer(state.get("ExitCode"), 0, "container_exit_code_invalid")
    if state.get("Error") != "":
        raise BindingError("container_state_error")
    health = _mapping(state.get("Health"), "container_health_missing")
    if health.get("Status") != "healthy":
        raise BindingError("container_unhealthy")
    _integer(health.get("FailingStreak"), 0, "container_health_failing_streak")
    process_id = state.get("Pid")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 1:
        raise BindingError("invalid_container_process_identity")
    started_at = _string(state.get("StartedAt"), "invalid_container_process_identity")
    if not _RFC3339_UTC_RE.fullmatch(started_at):
        raise BindingError("invalid_container_process_identity")
    restart_count = container.get("RestartCount")
    if (
        isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
    ):
        raise BindingError("invalid_container_process_identity")

    host_config = _mapping(container.get("HostConfig"), "invalid_container_host_config")
    host_security = _closed_host_security(host_config)
    if container.get("AppArmorProfile") != "docker-default" or container.get(
        "Platform"
    ) != "linux":
        raise BindingError("container_host_security_mismatch")

    container_config = _mapping(container.get("Config"), "invalid_container_config")
    if container_config.get("Image") != config.image_reference:
        raise BindingError("container_image_reference_mismatch")
    if container_config.get("Hostname") != config.expected_replica_id:
        raise BindingError("container_replica_mismatch")
    if _optional_command(
        container_config.get("Entrypoint"), "invalid_container_runtime_contract"
    ) != image_runtime.entrypoint or _optional_command(
        container_config.get("Cmd"), "invalid_container_runtime_contract"
    ) != image_runtime.command:
        raise BindingError("container_command_override")
    if _optional_command(
        container_config.get("Shell"), "invalid_container_runtime_contract"
    ) != image_runtime.shell:
        raise BindingError("container_shell_override")
    if container_config.get("User") != image_runtime.user or container_config.get(
        "WorkingDir"
    ) != image_runtime.working_directory:
        raise BindingError("container_runtime_contract_mismatch")
    container_healthcheck = _mapping(
        container_config.get("Healthcheck"), "invalid_container_runtime_contract"
    )
    if container_healthcheck != image_runtime.healthcheck:
        raise BindingError("container_healthcheck_override")
    effective_command = (*image_runtime.entrypoint, *image_runtime.command)
    if not effective_command:
        raise BindingError("invalid_image_runtime_contract")
    if container.get("Path") != effective_command[0] or tuple(
        _string_list(container.get("Args"), "invalid_container_runtime_contract")
    ) != effective_command[1:]:
        raise BindingError("container_process_override")
    environment = _container_env(container_config.get("Env"))
    manifest_sha = build_receipt["release_manifest"]["manifest_sha256"]
    expected_environment = {
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": config.source_candidate_sha,
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": build_receipt["oci_manifest_digest"],
        "PROPERTYQUARRY_RELEASE_MANIFEST_SHA256": manifest_sha,
    }
    complete_expected_environment = dict(image_runtime.environment)
    complete_expected_environment.update(expected_environment)
    if environment != complete_expected_environment:
        raise BindingError("container_runtime_environment_mismatch")

    mounts_value = container.get("Mounts")
    if not isinstance(mounts_value, list):
        raise BindingError("invalid_container_mounts")
    inspected_mounts: list[Mapping[str, Any]] = []
    for value in mounts_value:
        mount = _mapping(value, "invalid_container_mounts")
        destination = _string(mount.get("Destination"), "invalid_container_mounts")
        if _mount_replaces_app(destination):
            raise BindingError("app_replacement_mount")
        inspected_mounts.append(mount)
    if len(inspected_mounts) != 1:
        raise BindingError("unauthorized_container_mounts")
    selected_mounts = [_authorized_data_mount_evidence(config, inspected_mounts[0])]

    version_endpoint = _validate_version_port_binding(config, container)
    if host_security["canonical_sha256"] != config.expected_host_config_sha256:
        raise BindingError("host_config_digest_mismatch")
    runtime_environment_sha256 = _fingerprint(dict(environment))

    return {
        "container_id": config.container_id,
        "image_config_id": build_receipt["image_config_id"],
        "image_reference": config.image_reference,
        "replica_id": config.expected_replica_id,
        "state": {
            "status": "running",
            "healthy": True,
            "failing_streak": 0,
            "pid": process_id,
            "started_at": started_at,
            "restart_count": restart_count,
        },
        "host_security_sha256": _fingerprint(host_security),
        "host_config_sha256": host_security["canonical_sha256"],
        "runtime_environment_sha256": runtime_environment_sha256,
        "runtime_contract_sha256": _fingerprint(
            {
                **_runtime_contract_evidence(image_runtime),
                "environment_sha256": runtime_environment_sha256,
                "effective_path": effective_command[0],
                "effective_args": list(effective_command[1:]),
            }
        ),
        "version_endpoint": version_endpoint,
        "mounts": selected_mounts,
    }


def _inspect_docker(
    config: BindingConfig,
    evidence: _Evidence,
    build_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    image = _docker_inspect_object(
        evidence.docker(
            "image", "inspect", config.image_reference, error_code="docker_image_inspect_failed"
        ),
        "invalid_docker_image_inspect",
    )
    container = _docker_inspect_object(
        evidence.docker(
            "container", "inspect", config.container_id, error_code="container_inspect_failed"
        ),
        "invalid_container_inspect",
    )
    image_evidence, image_runtime = _validate_image_inspect(config, build_receipt, image)
    return {
        "image": image_evidence,
        "container": _validate_container_inspect(
            config, build_receipt, container, image_runtime
        ),
    }


def _fetch_and_validate_version(
    config: BindingConfig,
    fetcher: VersionFetcher,
    build_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        response = fetcher.fetch(
            config.version_url,
            timeout_s=config.http_timeout_s,
            max_output_bytes=config.max_version_output_bytes,
        )
    except BindingError:
        raise
    except Exception:
        raise BindingError("version_fetch_failed") from None
    if not isinstance(response, VersionResponse):
        raise BindingError("invalid_version_response")
    if (
        not isinstance(response.status_code, int)
        or not isinstance(response.final_url, str)
        or not isinstance(response.body, bytes)
        or not isinstance(response.peer_ip, str)
        or isinstance(response.peer_port, bool)
        or not isinstance(response.peer_port, int)
    ):
        raise BindingError("invalid_version_response")
    expected_address, expected_port = _local_version_endpoint(config.version_url)
    try:
        observed_peer = ipaddress.ip_address(response.peer_ip)
    except ValueError:
        raise BindingError("version_peer_mismatch") from None
    if (
        observed_peer != expected_address
        or not observed_peer.is_loopback
        or response.peer_port != expected_port
    ):
        raise BindingError("version_peer_mismatch")
    if len(response.body) > config.max_version_output_bytes:
        raise BindingError("version_output_limit")
    if response.status_code != 200:
        raise BindingError("version_http_status")
    if response.final_url != config.version_url:
        raise BindingError("version_redirected")
    version = _strict_json(response.body, error_code="invalid_version_json")
    if not isinstance(version, dict):
        raise BindingError("invalid_version_json")

    expected = {
        "release_commit_sha": config.source_candidate_sha,
        "release_manifest_sha256": build_receipt["release_manifest"]["manifest_sha256"],
        "release_image_digest": build_receipt["oci_manifest_digest"],
        "replica_id": config.expected_replica_id,
    }
    for key, value in expected.items():
        if version.get(key) != value:
            raise BindingError("version_identity_mismatch")
    if version.get("release_manifest_status") != "complete":
        raise BindingError("version_manifest_incomplete")
    for key in ("release_manifest_errors", "release_manifest_mismatch_fields"):
        if key in version and version[key] not in (None, "", [], {}):
            raise BindingError("version_manifest_incomplete")
    return {
        **expected,
        "release_manifest_status": "complete",
    }


def _open_receipt_root(root: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError:
        raise BindingError("receipt_root_invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BindingError("receipt_root_invalid")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_write_new_private(root: Path, path: Path, data: bytes) -> None:
    root_descriptor = _open_receipt_root(root)
    target_name = path.name

    descriptor = -1
    temporary_name: str | None = None
    completed = False
    file_identity: tuple[int, int] | None = None
    try:
        try:
            os.stat(target_name, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BindingError("receipt_already_exists")
        temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=root_descriptor)
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        file_identity = (opened.st_dev, opened.st_ino)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise BindingError("receipt_write_failed")
            offset += written
        os.fsync(descriptor)
        temporary_stat = os.fstat(descriptor)
        if file_identity != (temporary_stat.st_dev, temporary_stat.st_ino):
            raise BindingError("receipt_identity_changed")
        if (
            not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_uid != os.geteuid()
            or stat.S_IMODE(temporary_stat.st_mode) != 0o600
        ):
            raise BindingError("receipt_mode_invalid")
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise BindingError("receipt_already_exists") from None
        target_stat = os.stat(
            target_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (target_stat.st_dev, target_stat.st_ino) != file_identity:
            raise BindingError("receipt_identity_changed")
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_uid != os.geteuid()
            or stat.S_IMODE(target_stat.st_mode) != 0o600
        ):
            raise BindingError("receipt_mode_invalid")
        os.fsync(root_descriptor)
        os.unlink(temporary_name, dir_fd=root_descriptor)
        temporary_name = None
        os.fsync(root_descriptor)
        completed = True
    except BindingError:
        raise
    except OSError:
        raise BindingError("receipt_write_failed") from None
    finally:
        cleanup_error = False
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_error = True
        if not completed and file_identity is not None:
            try:
                target_stat = os.stat(
                    target_name, dir_fd=root_descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_error = True
            else:
                if (target_stat.st_dev, target_stat.st_ino) == file_identity:
                    try:
                        os.unlink(target_name, dir_fd=root_descriptor)
                        os.fsync(root_descriptor)
                    except OSError:
                        cleanup_error = True
                else:
                    cleanup_error = True
        os.close(root_descriptor)
        if cleanup_error:
            raise BindingError("receipt_cleanup_failed")


def _fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json_bytes(value))


def verify_binding(
    config: BindingConfig,
    *,
    executor: CommandExecutor | None = None,
    version_fetcher: VersionFetcher | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> VerificationResult:
    """Verify the complete local binding and atomically emit a private receipt."""

    _validate_config(config)
    executor = executor or BoundedSubprocessExecutor()
    version_fetcher = version_fetcher or LocalVersionFetcher()
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    evidence = _Evidence(config, executor)

    build_receipt, build_raw_before = _load_and_validate_build_receipt(config)
    git_before = _verify_git_binding(config, evidence, build_receipt)
    docker_before = _inspect_docker(config, evidence, build_receipt)
    version_before = _fetch_and_validate_version(config, version_fetcher, build_receipt)

    try:
        version_after = _fetch_and_validate_version(config, version_fetcher, build_receipt)
        docker_after = _inspect_docker(config, evidence, build_receipt)
        git_after = _verify_git_binding(config, evidence, build_receipt)
        build_receipt_after, build_raw_after = _load_and_validate_build_receipt(config)
    except BindingError:
        raise BindingError("toctou_reinspection_failed") from None

    if (
        build_raw_before != build_raw_after
        or build_receipt != build_receipt_after
        or _fingerprint(git_before) != _fingerprint(git_after)
        or _fingerprint(docker_before) != _fingerprint(docker_after)
        or _fingerprint(version_before) != _fingerprint(version_after)
    ):
        raise BindingError("toctou_evidence_changed")

    observed_at = now()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise BindingError("invalid_observation_clock")
    observed_at_utc = observed_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    receipt: Mapping[str, Any] = {
        "schema": BINDING_RECEIPT_SCHEMA,
        "status": "pass",
        "gate_passed": True,
        "observed_at_utc": observed_at_utc,
        "local_docker_authority": True,
        "production_authority": False,
        "public_launch_authority": False,
        "authority": {
            "local_docker_authority": True,
            "production_authority": False,
            "public_launch_authority": False,
        },
        "production_ready": False,
        "performs_release_effects": False,
        "source_candidate_sha": config.source_candidate_sha,
        "metadata_envelope_sha": config.metadata_envelope_sha,
        "build_receipt_sha256": config.expected_build_receipt_sha256,
        "source_candidate_tree_sha": git_before["source_candidate_tree_sha"],
        "metadata_envelope_tree_sha": git_before["metadata_envelope_tree_sha"],
        "source_candidate_archive_sha256": git_before["source_candidate_archive_sha256"],
        "metadata_envelope_archive_sha256": git_before["metadata_envelope_archive_sha256"],
        "release_manifest_sha256": build_receipt["release_manifest"]["manifest_sha256"],
        "oci_manifest_digest": build_receipt["oci_manifest_digest"],
        "observed_repo_digest": docker_before["image"]["observed_repo_digest"],
        "image_config_id": build_receipt["image_config_id"],
        "container_id": config.container_id,
        "replica_id": config.expected_replica_id,
        "verified_metadata_paths": git_before["metadata_paths"],
        "verified_mounts": docker_before["container"]["mounts"],
        "version_endpoint": docker_before["container"]["version_endpoint"],
        "runtime_contract_sha256": docker_before["container"]["runtime_contract_sha256"],
        "runtime_environment_sha256": docker_before["container"][
            "runtime_environment_sha256"
        ],
        "host_security_sha256": docker_before["container"]["host_security_sha256"],
        "host_config_sha256": docker_before["container"]["host_config_sha256"],
        "checks": {
            "clean_git_context": True,
            "candidate_is_ancestor_of_metadata_envelope": True,
            "metadata_allowlist_only": True,
            "build_input_hashes_match": True,
            "oci_digest_distinct_from_image_config_id": True,
            "oci_labels_match": True,
            "registry_independent_local_oci_matches": True,
            "container_image_state_health_match": True,
            "container_process_identity_stable": True,
            "container_host_security_closed": True,
            "no_app_replacement_mounts": True,
            "authorized_mount_contract_exact": True,
            "authorized_data_volume_identity_matches": True,
            "host_config_digest_matches": True,
            "version_identity_matches": True,
            "pre_post_reinspection_matches": True,
        },
    }
    raw_receipt = _canonical_json_bytes(receipt)
    _atomic_write_new_private(config.receipt_root, config.receipt_path, raw_receipt)
    return VerificationResult(receipt=receipt, receipt_sha256=_sha256(raw_receipt))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a local Docker PropertyQuarry candidate/runtime binding. "
            "A passing result is never production or public-launch authority."
        )
    )
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--source-candidate-sha", required=True)
    parser.add_argument("--metadata-envelope-sha", required=True)
    parser.add_argument("--build-receipt", required=True, type=Path)
    parser.add_argument("--build-receipt-sha256", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--replica-id", required=True)
    parser.add_argument("--authorized-data-volume-name", required=True)
    parser.add_argument("--authorized-data-volume-source", required=True)
    parser.add_argument("--host-config-sha256", required=True)
    parser.add_argument("--version-url", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    config = BindingConfig(
        repo_root=arguments.repo_root,
        receipt_root=arguments.receipt_root,
        source_candidate_sha=arguments.source_candidate_sha,
        metadata_envelope_sha=arguments.metadata_envelope_sha,
        build_receipt_path=arguments.build_receipt,
        expected_build_receipt_sha256=arguments.build_receipt_sha256,
        image_reference=arguments.image_reference,
        container_id=arguments.container_id,
        expected_replica_id=arguments.replica_id,
        authorized_data_volume_name=arguments.authorized_data_volume_name,
        authorized_data_volume_source=arguments.authorized_data_volume_source,
        expected_host_config_sha256=arguments.host_config_sha256,
        version_url=arguments.version_url,
        receipt_path=arguments.receipt,
    )
    try:
        result = verify_binding(config)
    except BindingError as error:
        print("propertyquarry-local-binding:" + error.code, file=sys.stderr)
        return 2
    except Exception:
        print("propertyquarry-local-binding:internal_error", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "pass",
                "receipt_sha256": result.receipt_sha256,
                "local_docker_authority": True,
                "production_authority": False,
                "public_launch_authority": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
