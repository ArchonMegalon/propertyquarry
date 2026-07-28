#!/usr/bin/env python3
"""Produce a fail-closed PropertyQuarry candidate build receipt.

The Docker build context is an authenticated ``git archive`` byte string held
in memory.  The live worktree, untracked files, ignored files, and
release-control surfaces are not build inputs or authorities.  The default
mode remains local-only; an explicit second authorization may publish to a
literal loopback OCI registry and verify the result by immutable digest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import io
import json
import math
import os
import posixpath
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from scripts import verify_generated_release_artifacts_clean as manifest_model


BUILD_RECEIPT_SCHEMA = "propertyquarry.local_candidate_build.v1"
PUBLISHED_BUILD_RECEIPT_SCHEMA = "propertyquarry.registry_candidate_build.v2"
REGISTRY_PUBLICATION_SCHEMA = "propertyquarry.loopback_registry_publication.v1"
WEB_DOCKERFILE_PATH = "ea/Dockerfile.property-web"
RENDER_DOCKERFILE_PATH = "ea/Dockerfile.property-render"
# Backward-compatible public name for the default web candidate profile.
DOCKERFILE_PATH = WEB_DOCKERFILE_PATH
IMAGE_KIND_DOCKERFILE_PATHS = {
    "web": WEB_DOCKERFILE_PATH,
    "render": RENDER_DOCKERFILE_PATH,
}
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
TRUSTED_GIT_BIN = "/usr/bin/git"
TRUSTED_DOCKER_BIN = "/usr/bin/docker"
TRUSTED_COMMAND_BINARIES = frozenset({TRUSTED_GIT_BIN, TRUSTED_DOCKER_BIN})
LOCAL_DOCKER_SOCKET = Path("/run/docker.sock")
LOCAL_IMAGE_TAG_PREFIX = "propertyquarry-local-candidate:"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_CONSTRUCTION = "docker-save-uncompressed-layers-v1"
_PROCESS_GROUP_CLEANUP_SECONDS = 2.0

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_TAG_RE = re.compile(
    r"propertyquarry-local-candidate:[a-z0-9][a-z0-9_.-]{0,127}\Z"
)
_REPO_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_DOCKER_CONFIG_NAME_RE = re.compile(r"\.pq-build-docker-[A-Za-z0-9_-]+\Z")
_BUILDX_CONFIG_NAME_RE = re.compile(r"\.pq-buildx-[A-Za-z0-9_-]+\Z")
_FROM_RE = re.compile(
    r"FROM[ \t]+(?P<reference>[^ \t]+@sha256:[0-9a-f]{64})"
    r"(?:[ \t]+AS[ \t]+(?P<alias>[A-Za-z0-9_.-]+))?[ \t]*\Z",
    flags=re.IGNORECASE,
)
_FROM_SCRATCH_RE = re.compile(
    r"FROM[ \t]+scratch"
    r"(?:[ \t]+AS[ \t]+(?P<alias>[A-Za-z0-9_.-]+))?[ \t]*\Z",
    flags=re.IGNORECASE,
)
_COPY_FROM_MARKER_RE = re.compile(r"(?:^|[ \t])--FROM(?:=|[ \t])", re.IGNORECASE)
_COPY_FROM_FLAG_RE = re.compile(
    r"(?:^|[ \t])--FROM(?:=|[ \t]+)(?P<source>[^ \t]+)(?=[ \t])",
    re.IGNORECASE,
)
_REGISTRY_COMPONENT_PATTERN = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_LOOPBACK_REGISTRY_REPOSITORY_RE = re.compile(
    rf"127\.0\.0\.1:(?P<port>[1-9][0-9]{{0,4}})/"
    rf"(?P<name>{_REGISTRY_COMPONENT_PATTERN}"
    rf"(?:/{_REGISTRY_COMPONENT_PATTERN})*)\Z"
)
_REGISTRY_CANDIDATE_TAG_RE = re.compile(
    r"candidate-[0-9a-f]{40}-[0-9a-f]{64}\Z"
)
_RFC3339_UTC_SECONDS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_DOCKERFILE_PARSER_DIRECTIVE_RE = re.compile(
    r"#[ \t]*[A-Za-z][A-Za-z0-9_-]*[ \t]*=",
    re.IGNORECASE,
)
_DOCKERFILE_INSTRUCTION_RE = re.compile(
    r"(?P<instruction>[A-Za-z]+)(?:[ \t]+(?P<body>.*))?\Z"
)
_MAX_DOCKER_ARCHIVE_MEMBERS = 100_000
_MAX_DOCKER_ARCHIVE_PATH_BYTES = 4_096
_MAX_DOCKER_ARCHIVE_MANIFEST_BYTES = 1_048_576
_MAX_DOCKER_ARCHIVE_CONFIG_BYTES = 16_777_216
_MAX_BUILDX_CONFIG_MEMBERS = 10_000
_MAX_BUILDX_CONFIG_FILE_BYTES = 16_777_216
_MAX_BUILDX_CONFIG_AGGREGATE_BYTES = 67_108_864
_REGISTRY_MANIFEST_MEDIA_TYPES = frozenset(
    {
        OCI_MANIFEST_MEDIA_TYPE,
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_REGISTRY_ACCEPT = ", ".join(sorted(_REGISTRY_MANIFEST_MEDIA_TYPES))


class BuildError(RuntimeError):
    """A stable, redacted failure suitable for automation."""

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
        input_data: bytes | None,
        timeout_s: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandResult: ...


class RegistryObserver(Protocol):
    def manifest_digest(
        self,
        repository: str,
        reference: str,
        *,
        timeout_s: float,
    ) -> str | None: ...


@dataclass(frozen=True)
class BuildConfig:
    repo_root: Path
    receipt_root: Path
    source_candidate_sha: str
    metadata_envelope_sha: str
    local_image_tag: str
    receipt_path: Path
    execute_local_build: bool
    image_kind: str = "web"
    execute_registry_publication: bool = False
    registry_repository: str | None = None
    git_timeout_s: float = 30.0
    docker_inspect_timeout_s: float = 30.0
    docker_build_timeout_s: float = 1_800.0
    docker_save_timeout_s: float = 300.0
    docker_push_timeout_s: float = 600.0
    docker_pull_timeout_s: float = 600.0
    registry_observe_timeout_s: float = 30.0
    max_command_output_bytes: int = 4_194_304
    max_git_archive_bytes: int = 536_870_912
    max_image_archive_bytes: int = 1_073_741_824
    max_input_file_bytes: int = 16_777_216
    allowed_metadata_paths: tuple[str, ...] = DEFAULT_ALLOWED_METADATA_PATHS


@dataclass(frozen=True)
class BuildResult:
    receipt: Mapping[str, Any]
    receipt_sha256: str


@dataclass(frozen=True)
class _GitSnapshot:
    candidate_tree: str
    envelope_tree: str
    changed_paths: tuple[str, ...]
    candidate_archive: bytes
    envelope_archive: bytes
    dockerfile: bytes
    release_manifest: bytes
    release_manifest_sha256: str


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json_document(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise BuildError("canonical_json_invalid") from None


def _canonical_json_bytes(value: Any) -> bytes:
    return _canonical_json_document(value) + b"\n"


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
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise BuildError(error_code) from None
    if duplicate:
        raise BuildError(error_code)
    return value


def _exact_mapping(value: Any, keys: set[str], error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BuildError(error_code)
    return value


def _string(value: Any, error_code: str) -> str:
    if not isinstance(value, str):
        raise BuildError(error_code)
    return value


def _string_list(value: Any, error_code: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BuildError(error_code)
    return value


def _validate_repo_relative_path(path: str, error_code: str) -> None:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or str(candidate) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise BuildError(error_code)


def _trusted_binary_identity(path: str) -> tuple[int, ...]:
    if path not in TRUSTED_COMMAND_BINARIES:
        raise BuildError("command_binary_untrusted")
    candidate = Path(path)
    current = Path(candidate.anchor)
    try:
        root = os.lstat(current)
        if (
            not stat.S_ISDIR(root.st_mode)
            or stat.S_ISLNK(root.st_mode)
            or root.st_uid != 0
            or root.st_mode & 0o022
        ):
            raise BuildError("command_binary_untrusted")
        for index, component in enumerate(candidate.parts[1:]):
            current /= component
            metadata = os.lstat(current)
            final = index == len(candidate.parts[1:]) - 1
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0:
                raise BuildError("command_binary_untrusted")
            if metadata.st_mode & 0o022:
                raise BuildError("command_binary_untrusted")
            if final:
                if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
                    raise BuildError("command_binary_untrusted")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise BuildError("command_binary_untrusted")
        final_metadata = os.lstat(candidate)
        return (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_uid,
            final_metadata.st_gid,
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
            final_metadata.st_ctime_ns,
        )
    except BuildError:
        raise
    except OSError:
        raise BuildError("command_binary_untrusted") from None


def _local_docker_socket_identity() -> tuple[int, ...]:
    path = LOCAL_DOCKER_SOCKET
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise BuildError("docker_socket_untrusted")
    current = Path(path.anchor)
    try:
        root = os.lstat(current)
        if (
            not stat.S_ISDIR(root.st_mode)
            or stat.S_ISLNK(root.st_mode)
            or root.st_uid != 0
            or root.st_mode & 0o022
        ):
            raise BuildError("docker_socket_untrusted")
        final_metadata: os.stat_result | None = None
        for index, component in enumerate(path.parts[1:]):
            current /= component
            metadata = os.lstat(current)
            final = index == len(path.parts[1:]) - 1
            if stat.S_ISLNK(metadata.st_mode):
                raise BuildError("docker_socket_untrusted")
            if final:
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid not in {0, os.geteuid()}
                    or metadata.st_mode & 0o002
                ):
                    raise BuildError("docker_socket_untrusted")
                final_metadata = metadata
                continue
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
                raise BuildError("docker_socket_untrusted")
            writable = bool(metadata.st_mode & 0o022)
            trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if writable and not trusted_sticky:
                raise BuildError("docker_socket_untrusted")
        if final_metadata is None:
            raise BuildError("docker_socket_untrusted")
        return (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_uid,
            final_metadata.st_gid,
        )
    except BuildError:
        raise
    except OSError:
        raise BuildError("docker_socket_untrusted") from None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        raise BuildError("command_cleanup_failed") from None


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _PROCESS_GROUP_CLEANUP_SECONDS
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        raise BuildError("command_cleanup_failed") from None
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError("command_cleanup_failed") from None
    while _process_group_exists(process.pid):
        if time.monotonic() >= deadline:
            raise BuildError("command_cleanup_failed")
        time.sleep(0.01)


class BoundedSubprocessExecutor:
    """Run trusted binaries with bounded input, output, time, and descendants."""

    _ENVIRONMENT = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "",
        "HOME": "/nonexistent",
        "DOCKER_BUILDKIT": "1",
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

    def __init__(self, *, buildx_config: Path | None = None) -> None:
        self._environment = dict(self._ENVIRONMENT)
        if buildx_config is not None:
            if (
                not buildx_config.is_absolute()
                or buildx_config != Path(os.path.abspath(buildx_config))
                or not _BUILDX_CONFIG_NAME_RE.fullmatch(buildx_config.name)
            ):
                raise BuildError("buildx_config_invalid")
            self._environment["BUILDX_CONFIG"] = str(buildx_config)

    def run(
        self,
        argv: Sequence[str],
        *,
        input_data: bytes | None,
        timeout_s: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandResult:
        if (
            not argv
            or timeout_s <= 0
            or max_stdout_bytes <= 0
            or max_stderr_bytes <= 0
            or (input_data is not None and not isinstance(input_data, bytes))
        ):
            raise BuildError("invalid_command_contract")
        _trusted_binary_identity(argv[0])
        try:
            process = subprocess.Popen(
                tuple(argv),
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=self._environment,
                shell=False,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
        except (OSError, ValueError):
            raise BuildError("command_launch_failed") from None

        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        input_offset = 0
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        deadline = time.monotonic() + timeout_s
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_process(process)
                    raise BuildError("command_timeout")
                events = selector.select(min(remaining, 0.25))
                if not events and process.poll() is not None:
                    events = [
                        (key, selectors.EVENT_READ)
                        for key in selector.get_map().values()
                        if key.fileobj is not process.stdin
                    ]
                for key, mask in events:
                    if process.stdin is not None and key.fileobj is process.stdin:
                        payload = input_data or b""
                        if input_offset >= len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        try:
                            written = os.write(
                                process.stdin.fileno(), payload[input_offset : input_offset + 65_536]
                            )
                        except BrokenPipeError:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        except OSError:
                            _kill_process(process)
                            raise BuildError("command_write_failed") from None
                        input_offset += written
                        continue
                    if not mask & selectors.EVENT_READ:
                        continue
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        _kill_process(process)
                        raise BuildError("command_read_failed") from None
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = stdout if key.fileobj is process.stdout else stderr
                    limit = max_stdout_bytes if key.fileobj is process.stdout else max_stderr_bytes
                    target.extend(chunk)
                    if len(target) > limit:
                        _kill_process(process)
                        raise BuildError("command_output_limit")
            try:
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _kill_process(process)
                raise BuildError("command_timeout") from None
            if _process_group_exists(process.pid):
                _kill_process(process)
                raise BuildError("command_residual_process_group")
            return CommandResult(returncode, bytes(stdout), bytes(stderr))
        finally:
            try:
                if process.poll() is None or _process_group_exists(process.pid):
                    _kill_process(process)
            finally:
                selector.close()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                process.stdout.close()
                process.stderr.close()


def _registry_coordinates(repository: str) -> tuple[int, str]:
    match = _LOOPBACK_REGISTRY_REPOSITORY_RE.fullmatch(repository)
    if match is None or len(repository) > 255:
        raise BuildError("registry_repository_invalid")
    port = int(match.group("port"))
    if not 1 <= port <= 65_535:
        raise BuildError("registry_repository_invalid")
    return port, match.group("name")


class LoopbackRegistryObserver:
    """Read immutable manifest identity directly from a literal loopback registry."""

    def manifest_digest(
        self,
        repository: str,
        reference: str,
        *,
        timeout_s: float,
    ) -> str | None:
        port, name = _registry_coordinates(repository)
        if (
            not _REGISTRY_CANDIDATE_TAG_RE.fullmatch(reference)
            and not _DIGEST_RE.fullmatch(reference)
        ):
            raise BuildError("registry_reference_invalid")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(
                "HEAD",
                f"/v2/{name}/manifests/{reference}",
                headers={"Accept": _REGISTRY_ACCEPT},
            )
            response = connection.getresponse()
            if response.read(1) != b"":
                raise BuildError("registry_response_invalid")
            api_versions = response.headers.get_all("Docker-Distribution-Api-Version")
            if api_versions != ["registry/2.0"]:
                raise BuildError("registry_response_invalid")
            if response.status == 404:
                return None
            if response.status != 200:
                raise BuildError("registry_observation_failed")
            content_types = response.headers.get_all("Content-Type")
            if (
                content_types is None
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip()
                not in _REGISTRY_MANIFEST_MEDIA_TYPES
            ):
                raise BuildError("registry_response_invalid")
            digests = response.headers.get_all("Docker-Content-Digest")
            if digests is None or len(digests) != 1 or not _DIGEST_RE.fullmatch(digests[0]):
                raise BuildError("registry_response_invalid")
            if _DIGEST_RE.fullmatch(reference) and digests[0] != reference:
                raise BuildError("registry_digest_mismatch")
            return digests[0]
        except BuildError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise BuildError("registry_observation_failed") from None
        finally:
            if response is not None:
                response.close()
            connection.close()


def _validate_receipt_root(root: Path) -> Path:
    if not root.is_absolute() or root != Path(os.path.abspath(root)) or root == Path(root.anchor):
        raise BuildError("receipt_root_invalid")
    current = Path(root.anchor)
    try:
        for index, component in enumerate(root.parts[1:]):
            current /= component
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BuildError("receipt_root_invalid")
            final = index == len(root.parts[1:]) - 1
            if final:
                if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                    raise BuildError("receipt_root_invalid")
            else:
                if metadata.st_uid not in {0, os.geteuid()}:
                    raise BuildError("receipt_root_invalid")
                writable = bool(metadata.st_mode & 0o022)
                trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
                if writable and not trusted_sticky:
                    raise BuildError("receipt_root_invalid")
    except BuildError:
        raise
    except OSError:
        raise BuildError("receipt_root_invalid") from None
    return root


def _validate_receipt_child(path: Path, root: Path) -> None:
    if (
        not path.is_absolute()
        or path != Path(os.path.abspath(path))
        or path.parent != root
        or path.name in {"", ".", ".."}
        or "/" in path.name
        or "\x00" in path.name
    ):
        raise BuildError("receipt_outside_root")


def _require_receipt_absent(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise BuildError("receipt_preflight_failed") from None
    raise BuildError("receipt_already_exists")


def _validate_config(config: BuildConfig) -> None:
    if config.execute_local_build is not True:
        raise BuildError("local_build_not_explicitly_authorized")
    if not _FULL_SHA_RE.fullmatch(config.source_candidate_sha):
        raise BuildError("invalid_source_candidate_sha")
    if not _FULL_SHA_RE.fullmatch(config.metadata_envelope_sha):
        raise BuildError("invalid_metadata_envelope_sha")
    if not _IMAGE_TAG_RE.fullmatch(config.local_image_tag):
        raise BuildError("invalid_local_image_tag")
    _dockerfile_path(config)
    if config.local_image_tag in {
        LOCAL_IMAGE_TAG_PREFIX + "latest",
        LOCAL_IMAGE_TAG_PREFIX + "main",
        LOCAL_IMAGE_TAG_PREFIX + "production",
    }:
        raise BuildError("invalid_local_image_tag")
    if config.execute_registry_publication is True:
        if not isinstance(config.registry_repository, str):
            raise BuildError("registry_repository_required")
        _registry_coordinates(config.registry_repository)
    elif config.execute_registry_publication is False:
        if config.registry_repository is not None:
            raise BuildError("registry_publication_not_explicitly_authorized")
    else:
        raise BuildError("registry_publication_not_explicitly_authorized")
    timeout_limits = (
        (config.git_timeout_s, 120.0),
        (config.docker_inspect_timeout_s, 120.0),
        (config.docker_build_timeout_s, 7_200.0),
        (config.docker_save_timeout_s, 1_200.0),
        (config.docker_push_timeout_s, 1_200.0),
        (config.docker_pull_timeout_s, 1_200.0),
        (config.registry_observe_timeout_s, 120.0),
    )
    byte_limits = (
        (config.max_command_output_bytes, 16_777_216),
        (config.max_git_archive_bytes, 1_073_741_824),
        (config.max_image_archive_bytes, 2_147_483_648),
        (config.max_input_file_bytes, 67_108_864),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
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
        raise BuildError("invalid_resource_limit")
    if not config.repo_root.is_absolute() or config.repo_root != Path(
        os.path.abspath(config.repo_root)
    ):
        raise BuildError("invalid_repo_root")
    try:
        repository = config.repo_root.resolve(strict=True)
    except OSError:
        raise BuildError("invalid_repo_root") from None
    if repository != config.repo_root or not repository.is_dir():
        raise BuildError("invalid_repo_root")
    root = _validate_receipt_root(config.receipt_root)
    _validate_receipt_child(config.receipt_path, root)
    allowed = tuple(config.allowed_metadata_paths)
    if not allowed or len(allowed) != len(set(allowed)):
        raise BuildError("invalid_metadata_allowlist")
    for path in allowed:
        _validate_repo_relative_path(path, "invalid_metadata_allowlist")
    _trusted_binary_identity(TRUSTED_GIT_BIN)
    _trusted_binary_identity(TRUSTED_DOCKER_BIN)
    _local_docker_socket_identity()


def _dockerfile_path(config: BuildConfig) -> str:
    if not isinstance(config.image_kind, str):
        raise BuildError("invalid_image_kind")
    try:
        return IMAGE_KIND_DOCKERFILE_PATHS[config.image_kind]
    except KeyError:
        raise BuildError("invalid_image_kind") from None


class _Evidence:
    def __init__(
        self,
        config: BuildConfig,
        executor: CommandExecutor,
        docker_config: Path,
        buildx_config: Path,
    ) -> None:
        self.config = config
        self.executor = executor
        self.docker_config = docker_config
        self.buildx_config = buildx_config

    def _run(
        self,
        argv: Sequence[str],
        *,
        input_data: bytes | None,
        timeout_s: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        error_code: str,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> CommandResult:
        try:
            result = self.executor.run(
                argv,
                input_data=input_data,
                timeout_s=timeout_s,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
        except BuildError:
            raise
        except Exception:
            raise BuildError("command_execution_failed") from None
        if (
            not isinstance(result, CommandResult)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
        ):
            raise BuildError("invalid_command_result")
        if len(result.stdout) > max_stdout_bytes or len(result.stderr) > max_stderr_bytes:
            raise BuildError("command_output_limit")
        if result.returncode not in allowed_returncodes:
            raise BuildError(error_code)
        return result

    def git(
        self,
        *arguments: str,
        error_code: str,
        max_stdout_bytes: int | None = None,
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
                str(self.config.repo_root),
                *arguments,
            ),
            input_data=None,
            timeout_s=self.config.git_timeout_s,
            max_stdout_bytes=max_stdout_bytes or self.config.max_command_output_bytes,
            max_stderr_bytes=self.config.max_command_output_bytes,
            error_code=error_code,
            allowed_returncodes=allowed_returncodes,
        )

    def docker(
        self,
        *arguments: str,
        error_code: str,
        input_data: bytes | None = None,
        timeout_s: float | None = None,
        max_stdout_bytes: int | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        success_observer: Callable[[CommandResult], None] | None = None,
    ) -> CommandResult:
        _audit_docker_config(self.docker_config)
        _audit_buildx_config(self.buildx_config)
        result = self._run(
            (
                TRUSTED_DOCKER_BIN,
                "--config",
                str(self.docker_config),
                "--host",
                f"unix://{LOCAL_DOCKER_SOCKET}",
                *arguments,
            ),
            input_data=input_data,
            timeout_s=timeout_s or self.config.docker_inspect_timeout_s,
            max_stdout_bytes=max_stdout_bytes or self.config.max_command_output_bytes,
            max_stderr_bytes=self.config.max_command_output_bytes,
            error_code=error_code,
            allowed_returncodes=allowed_returncodes,
        )
        if success_observer is not None:
            success_observer(result)
        _audit_docker_config(self.docker_config)
        _audit_buildx_config(self.buildx_config)
        return result


def _one_line_sha(data: bytes, error_code: str) -> str:
    try:
        value = data.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise BuildError(error_code) from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise BuildError(error_code)
    value = value[:-1]
    if not _FULL_SHA_RE.fullmatch(value):
        raise BuildError(error_code)
    return value


def _one_line_digest(data: bytes, error_code: str) -> str:
    try:
        value = data.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise BuildError(error_code) from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise BuildError(error_code)
    value = value[:-1]
    if not _DIGEST_RE.fullmatch(value):
        raise BuildError(error_code)
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
        raise BuildError("git_attributes_path_invalid") from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise BuildError("git_attributes_path_invalid")
    path = Path(value[:-1])
    if (
        not path.is_absolute()
        or path != Path(os.path.abspath(path))
        or len(os.fsencode(path)) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in str(path))
    ):
        raise BuildError("git_attributes_path_invalid")
    return path


def _require_no_local_git_attributes(evidence: _Evidence) -> None:
    path = _git_attributes_path(evidence)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise BuildError("git_local_attributes_check_failed") from None
    raise BuildError("git_local_attributes_forbidden")


def _changed_paths(data: bytes) -> tuple[str, ...]:
    if data and not data.endswith(b"\0"):
        raise BuildError("invalid_git_path_output")
    paths: set[str] = set()
    for token in data.split(b"\0"):
        if not token:
            continue
        try:
            path = token.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BuildError("invalid_git_path_output") from None
        _validate_repo_relative_path(path, "invalid_git_path_output")
        paths.add(path)
    return tuple(sorted(paths))


def _safe_tar_path(path: str, error_code: str) -> str:
    normalized = path.removesuffix("/")
    _validate_repo_relative_path(normalized, error_code)
    return normalized


def _safe_symlink_target(member_path: str, target: str) -> None:
    if not target or "\\" in target or target.startswith("/") or "\x00" in target:
        raise BuildError("git_archive_unsafe_entry")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(member_path), target))
    if joined in {"", ".", ".."} or joined.startswith("../") or joined.startswith("/"):
        raise BuildError("git_archive_unsafe_entry")


def _archive_required_files(
    archive: bytes,
    *,
    max_file_bytes: int,
    dockerfile_path: str = DOCKERFILE_PATH,
) -> tuple[bytes, bytes]:
    required_paths = {dockerfile_path, RELEASE_MANIFEST_PATH}
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
            for member in tree:
                path = _safe_tar_path(member.name, "git_archive_unsafe_entry")
                if path in seen:
                    raise BuildError("git_archive_duplicate_entry")
                seen.add(path)
                if member.isdir():
                    continue
                if member.issym():
                    _safe_symlink_target(path, member.linkname)
                    continue
                if not member.isfile() or member.islnk():
                    raise BuildError("git_archive_unsafe_entry")
                if member.size < 0 or member.size > max_file_bytes:
                    if path in required_paths:
                        raise BuildError("build_input_too_large")
                    continue
                if path not in required_paths:
                    continue
                stream = tree.extractfile(member)
                if stream is None:
                    raise BuildError("git_archive_required_file_invalid")
                payload = stream.read(max_file_bytes + 1)
                if len(payload) != member.size or len(payload) > max_file_bytes:
                    raise BuildError("git_archive_required_file_invalid")
                files[path] = payload
    except BuildError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError):
        raise BuildError("git_archive_invalid") from None
    if set(files) != required_paths:
        raise BuildError("git_archive_required_file_missing")
    return files[dockerfile_path], files[RELEASE_MANIFEST_PATH]


def _release_manifest_digest(raw: bytes, candidate_sha: str) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
        values, issues = manifest_model._parse_release_manifest(text)
        issues.extend(manifest_model._release_manifest_shape_issues(values))
        if issues:
            raise ValueError
        if values.get("release_commit_sha") != candidate_sha:
            raise ValueError
        for key, expected in manifest_model.RELEASE_MANIFEST_STATIC_VALUES.items():
            if values.get(key) != expected:
                raise ValueError
        if values.get("release_label") != f"propertyquarry-source-browser-candidate-{candidate_sha[:12]}":
            raise ValueError
        if values.get("release_deployment_id") != f"propertyquarry-governed-deploy-{candidate_sha[:12]}":
            raise ValueError
        return manifest_model.release_manifest_sha256(values)
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise BuildError("release_manifest_invalid") from None


def _observe_git(config: BuildConfig, evidence: _Evidence) -> _GitSnapshot:
    candidate = config.source_candidate_sha
    envelope = config.metadata_envelope_sha
    dockerfile_path = _dockerfile_path(config)
    evidence.git("cat-file", "-e", candidate + "^{commit}", error_code="candidate_commit_missing")
    evidence.git("cat-file", "-e", envelope + "^{commit}", error_code="envelope_commit_missing")
    ancestry = evidence.git(
        "merge-base",
        "--is-ancestor",
        candidate,
        envelope,
        error_code="git_ancestry_check_failed",
        allowed_returncodes=frozenset({0, 1}),
    )
    if ancestry.returncode != 0:
        raise BuildError("metadata_envelope_not_descendant")
    candidate_tree = _one_line_sha(
        evidence.git("rev-parse", candidate + "^{tree}", error_code="candidate_tree_failed").stdout,
        "invalid_candidate_tree",
    )
    envelope_tree = _one_line_sha(
        evidence.git("rev-parse", envelope + "^{tree}", error_code="envelope_tree_failed").stdout,
        "invalid_envelope_tree",
    )
    changed = _changed_paths(
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
    if set(changed).difference(config.allowed_metadata_paths):
        raise BuildError("metadata_envelope_contains_source_changes")
    _require_no_local_git_attributes(evidence)
    candidate_archive = evidence.git(
        "archive",
        "--format=tar",
        candidate,
        error_code="candidate_archive_failed",
        max_stdout_bytes=config.max_git_archive_bytes,
    ).stdout
    envelope_archive = evidence.git(
        "archive",
        "--format=tar",
        envelope,
        error_code="envelope_archive_failed",
        max_stdout_bytes=config.max_git_archive_bytes,
    ).stdout
    _require_no_local_git_attributes(evidence)
    candidate_dockerfile, _candidate_manifest = _archive_required_files(
        candidate_archive,
        max_file_bytes=config.max_input_file_bytes,
        dockerfile_path=dockerfile_path,
    )
    dockerfile, release_manifest = _archive_required_files(
        envelope_archive,
        max_file_bytes=config.max_input_file_bytes,
        dockerfile_path=dockerfile_path,
    )
    if candidate_dockerfile != dockerfile:
        raise BuildError("dockerfile_changed_in_metadata_envelope")
    manifest_sha256 = _release_manifest_digest(release_manifest, candidate)
    return _GitSnapshot(
        candidate_tree=candidate_tree,
        envelope_tree=envelope_tree,
        changed_paths=changed,
        candidate_archive=candidate_archive,
        envelope_archive=envelope_archive,
        dockerfile=dockerfile,
        release_manifest=release_manifest,
        release_manifest_sha256=manifest_sha256,
    )


def _dockerfile_base_references(dockerfile: bytes) -> tuple[str, ...]:
    try:
        text = dockerfile.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise BuildError("dockerfile_invalid") from None
    references: list[str] = []
    declared_stage_aliases: set[str] = set()
    logical = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Docker parser directives are interpreted before Dockerfile escape
        # processing.  Reject every directive-shaped leading comment rather
        # than attempting to emulate the daemon's evolving directive set.
        if _DOCKERFILE_PARSER_DIRECTIVE_RE.match(line):
            raise BuildError("dockerfile_local_only_contract_invalid")
        if not line or line.startswith("#"):
            continue
        logical += line
        if logical.endswith("\\"):
            logical = logical[:-1] + " "
            continue
        parsed = _DOCKERFILE_INSTRUCTION_RE.fullmatch(logical)
        if parsed is None:
            raise BuildError("dockerfile_invalid")
        instruction = parsed.group("instruction").upper()
        body = parsed.group("body") or ""
        if instruction == "FROM":
            match = _FROM_RE.fullmatch(logical)
            scratch_match = (
                _FROM_SCRATCH_RE.fullmatch(logical) if match is None else None
            )
            if match is None and scratch_match is None:
                raise BuildError("dockerfile_base_not_digest_pinned")
            if match is not None:
                references.append(match.group("reference"))
            alias = (
                match.group("alias")
                if match is not None
                else scratch_match.group("alias")
            )
            if alias is not None:
                normalized_alias = alias.lower()
                if normalized_alias in declared_stage_aliases:
                    raise BuildError("dockerfile_local_only_contract_invalid")
                declared_stage_aliases.add(normalized_alias)
        copy_from = list(_COPY_FROM_FLAG_RE.finditer(body))
        if instruction == "COPY" and _COPY_FROM_MARKER_RE.search(body):
            if (
                len(copy_from) != 1
                or copy_from[0].group("source").lower() not in declared_stage_aliases
            ):
                raise BuildError("dockerfile_local_only_contract_invalid")
        upper = body.upper()
        if (
            instruction == "ADD"
            or instruction == "RUN" and re.search(r"(?:^|[ \t])--MOUNT(?:=|[ \t])", upper)
            or instruction == "RUN" and re.search(r"(?:^|[ \t])--DEVICE(?:=|[ \t])", upper)
            or instruction == "RUN" and re.search(r"(?:^|[ \t])--SECURITY(?:=|[ \t])", upper)
            or instruction == "RUN"
            and re.search(r"(?:^|[ \t])--NETWORK[ \t]*=[ \t]*(?!NONE(?:[ \t]|$))", upper)
        ):
            raise BuildError("dockerfile_local_only_contract_invalid")
        logical = ""
    if logical or not references:
        raise BuildError("dockerfile_invalid")
    return tuple(dict.fromkeys(references))


def _docker_inspect_object(data: bytes, error_code: str) -> Mapping[str, Any]:
    value = _strict_json(data, error_code=error_code)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BuildError(error_code)
    return value[0]


def _base_image_evidence(reference: str, image: Mapping[str, Any]) -> Mapping[str, Any]:
    expected_digest = reference.rsplit("@", 1)[-1]
    image_id = _string(image.get("Id"), "base_image_inspect_invalid")
    if not _DIGEST_RE.fullmatch(image_id):
        raise BuildError("base_image_inspect_invalid")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise BuildError("base_image_platform_mismatch")
    repo_digests = _string_list(image.get("RepoDigests"), "base_image_inspect_invalid")
    config = image.get("Config")
    if not isinstance(config, dict):
        raise BuildError("base_image_inspect_invalid")
    on_build = config.get("OnBuild")
    if on_build is not None and on_build != []:
        raise BuildError("base_image_onbuild_forbidden")
    matching = [
        item
        for item in repo_digests
        if _REPO_DIGEST_RE.fullmatch(item) and item.rsplit("@", 1)[-1] == expected_digest
    ]
    if len(matching) != 1:
        raise BuildError("base_image_digest_unavailable_locally")
    rootfs = _exact_mapping(
        image.get("RootFS"), {"Type", "Layers"}, "base_image_rootfs_invalid"
    )
    layers = _string_list(rootfs["Layers"], "base_image_rootfs_invalid")
    if (
        rootfs["Type"] != "layers"
        or not layers
        or any(not _DIGEST_RE.fullmatch(item) for item in layers)
    ):
        raise BuildError("base_image_rootfs_invalid")
    return {
        "reference": reference,
        "image_config_id": image_id,
        "observed_repo_digest": matching[0],
        "platform": "linux/amd64",
        "rootfs_diff_ids": layers,
    }


def _reinspect_base_image(
    expected: Mapping[str, Any],
    image: Mapping[str, Any],
) -> None:
    image_id = _string(image.get("Id"), "base_image_reinspect_invalid")
    if image_id != expected.get("image_config_id"):
        raise BuildError("base_image_changed_during_build")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise BuildError("base_image_changed_during_build")
    config = image.get("Config")
    if not isinstance(config, dict):
        raise BuildError("base_image_reinspect_invalid")
    on_build = config.get("OnBuild")
    if on_build is not None and on_build != []:
        raise BuildError("base_image_changed_during_build")
    rootfs = _exact_mapping(
        image.get("RootFS"), {"Type", "Layers"}, "base_image_reinspect_invalid"
    )
    layers = _string_list(rootfs["Layers"], "base_image_reinspect_invalid")
    if (
        rootfs["Type"] != "layers"
        or layers != expected.get("rootfs_diff_ids")
        or any(not _DIGEST_RE.fullmatch(item) for item in layers)
    ):
        raise BuildError("base_image_changed_during_build")
    repo_digests = _string_list(
        image.get("RepoDigests"), "base_image_reinspect_invalid"
    )
    if any(not _REPO_DIGEST_RE.fullmatch(item) for item in repo_digests):
        raise BuildError("base_image_reinspect_invalid")


def _image_evidence(
    image: Mapping[str, Any],
    *,
    tag: str,
    labels: Mapping[str, str],
) -> Mapping[str, Any]:
    image_id = _string(image.get("Id"), "built_image_inspect_invalid")
    if not _DIGEST_RE.fullmatch(image_id):
        raise BuildError("built_image_inspect_invalid")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise BuildError("built_image_platform_mismatch")
    repo_tags = _string_list(image.get("RepoTags"), "built_image_inspect_invalid")
    if repo_tags.count(tag) != 1:
        raise BuildError("built_image_tag_mismatch")
    config = image.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise BuildError("built_image_inspect_invalid")
    observed_labels = config["Labels"]
    if any(observed_labels.get(key) != expected for key, expected in labels.items()):
        raise BuildError("built_image_label_mismatch")
    rootfs = _exact_mapping(
        image.get("RootFS"), {"Type", "Layers"}, "built_image_rootfs_invalid"
    )
    layers = _string_list(rootfs["Layers"], "built_image_rootfs_invalid")
    if rootfs["Type"] != "layers" or not layers or any(not _DIGEST_RE.fullmatch(item) for item in layers):
        raise BuildError("built_image_rootfs_invalid")
    if len(layers) != len(set(layers)):
        # Repeated empty or identical filesystem changes are legal in theory but
        # ambiguous in this receipt construction and therefore fail closed.
        raise BuildError("built_image_rootfs_invalid")
    repo_digests = _string_list(image.get("RepoDigests"), "built_image_inspect_invalid")
    if any(not _REPO_DIGEST_RE.fullmatch(item) for item in repo_digests):
        raise BuildError("built_image_inspect_invalid")
    return {
        "image_config_id": image_id,
        "rootfs_diff_ids": layers,
        "labels": {key: observed_labels[key] for key in REQUIRED_IMAGE_LABELS},
        "repo_tags": sorted(repo_tags),
        "repo_digests": sorted(repo_digests),
    }


def _registry_candidate_tag(source_candidate_sha: str, image_id: str) -> str:
    tag = f"candidate-{source_candidate_sha}-{image_id.removeprefix('sha256:')}"
    if not _REGISTRY_CANDIDATE_TAG_RE.fullmatch(tag):
        raise BuildError("registry_reference_invalid")
    return tag


def _observe_registry_manifest(
    observer: RegistryObserver,
    *,
    repository: str,
    reference: str,
    timeout_s: float,
    allow_missing: bool,
) -> str | None:
    try:
        digest = observer.manifest_digest(
            repository,
            reference,
            timeout_s=timeout_s,
        )
    except BuildError:
        raise
    except Exception:
        raise BuildError("registry_observation_failed") from None
    if digest is None:
        if allow_missing:
            return None
        raise BuildError("registry_manifest_missing")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise BuildError("registry_response_invalid")
    return digest


def _published_image_evidence(
    image: Mapping[str, Any],
    *,
    immutable_reference: str,
    expected_image_id: str,
    expected_diff_ids: Sequence[str],
    expected_labels: Mapping[str, str],
) -> Mapping[str, Any]:
    image_id = _string(image.get("Id"), "registry_image_inspect_invalid")
    if image_id != expected_image_id:
        raise BuildError("registry_image_config_mismatch")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise BuildError("registry_image_platform_mismatch")
    config = image.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise BuildError("registry_image_inspect_invalid")
    observed_labels = config["Labels"]
    if any(observed_labels.get(key) != expected for key, expected in expected_labels.items()):
        raise BuildError("registry_image_label_mismatch")
    rootfs = _exact_mapping(
        image.get("RootFS"), {"Type", "Layers"}, "registry_image_rootfs_invalid"
    )
    layers = _string_list(rootfs["Layers"], "registry_image_rootfs_invalid")
    if rootfs["Type"] != "layers" or layers != list(expected_diff_ids):
        raise BuildError("registry_image_rootfs_mismatch")
    repo_tags = _string_list(image.get("RepoTags"), "registry_image_inspect_invalid")
    repo_digests = _string_list(image.get("RepoDigests"), "registry_image_inspect_invalid")
    if (
        any(not _REPO_DIGEST_RE.fullmatch(item) for item in repo_digests)
        or repo_digests.count(immutable_reference) != 1
    ):
        raise BuildError("registry_repo_digest_mismatch")
    return {
        "image_config_id": image_id,
        "rootfs_diff_ids": layers,
        "labels": {key: observed_labels[key] for key in REQUIRED_IMAGE_LABELS},
        "repo_tags": sorted(repo_tags),
        "repo_digests": sorted(repo_digests),
    }


def _publish_to_loopback_registry(
    config: BuildConfig,
    evidence: _Evidence,
    observer: RegistryObserver,
    *,
    image_id: str,
    diff_ids: Sequence[str],
    labels: Mapping[str, str],
    owned_local_tags: list[str],
) -> Mapping[str, Any]:
    repository = config.registry_repository
    assert isinstance(repository, str)
    tag_name = _registry_candidate_tag(config.source_candidate_sha, image_id)
    candidate_reference = f"{repository}:{tag_name}"
    manifest_digest = _observe_registry_manifest(
        observer,
        repository=repository,
        reference=tag_name,
        timeout_s=config.registry_observe_timeout_s,
        allow_missing=True,
    )
    push_performed = manifest_digest is None
    if push_performed:

        def capture_local_publication_tag(_result: CommandResult) -> None:
            owned_local_tags.append(candidate_reference)

        evidence.docker(
            "image",
            "tag",
            image_id,
            candidate_reference,
            error_code="registry_local_tag_failed",
            success_observer=capture_local_publication_tag,
        )
        evidence.docker(
            "image",
            "push",
            "--quiet",
            candidate_reference,
            error_code="registry_push_failed",
            timeout_s=config.docker_push_timeout_s,
        )
        manifest_digest = _observe_registry_manifest(
            observer,
            repository=repository,
            reference=tag_name,
            timeout_s=config.registry_observe_timeout_s,
            allow_missing=False,
        )
    assert manifest_digest is not None
    immutable_reference = f"{repository}@{manifest_digest}"
    if not _REPO_DIGEST_RE.fullmatch(immutable_reference):
        raise BuildError("registry_reference_invalid")
    evidence.docker(
        "image",
        "pull",
        "--platform",
        "linux/amd64",
        "--quiet",
        immutable_reference,
        error_code="registry_digest_pull_failed",
        timeout_s=config.docker_pull_timeout_s,
    )
    first_object = _docker_inspect_object(
        evidence.docker(
            "image",
            "inspect",
            immutable_reference,
            error_code="registry_image_inspect_failed",
        ).stdout,
        "registry_image_inspect_invalid",
    )
    first = _published_image_evidence(
        first_object,
        immutable_reference=immutable_reference,
        expected_image_id=image_id,
        expected_diff_ids=diff_ids,
        expected_labels=labels,
    )
    tag_digest_after_pull = _observe_registry_manifest(
        observer,
        repository=repository,
        reference=tag_name,
        timeout_s=config.registry_observe_timeout_s,
        allow_missing=False,
    )
    if tag_digest_after_pull != manifest_digest:
        raise BuildError("registry_tag_changed_during_publication")
    digest_observation = _observe_registry_manifest(
        observer,
        repository=repository,
        reference=manifest_digest,
        timeout_s=config.registry_observe_timeout_s,
        allow_missing=False,
    )
    if digest_observation != manifest_digest:
        raise BuildError("registry_digest_mismatch")
    second_object = _docker_inspect_object(
        evidence.docker(
            "image",
            "inspect",
            immutable_reference,
            error_code="registry_image_reinspect_failed",
        ).stdout,
        "registry_image_inspect_invalid",
    )
    second = _published_image_evidence(
        second_object,
        immutable_reference=immutable_reference,
        expected_image_id=image_id,
        expected_diff_ids=diff_ids,
        expected_labels=labels,
    )
    if second != first:
        raise BuildError("registry_image_changed_during_publication")
    return {
        "schema": REGISTRY_PUBLICATION_SCHEMA,
        "transport": "loopback_http",
        "repository": repository,
        "candidate_tag": candidate_reference,
        "tag_preexisting": not push_performed,
        "push_performed": push_performed,
        "manifest_digest": manifest_digest,
        "immutable_reference": immutable_reference,
        "pulled_image": first,
        "platform": "linux/amd64",
    }


def _safe_archive_member_path(path: str) -> str:
    if path.endswith("/"):
        path = path[:-1]
    _validate_repo_relative_path(path, "docker_archive_unsafe_entry")
    return path


def _docker_archive_members(archive: bytes) -> Mapping[str, tuple[int, int]]:
    regular: dict[str, tuple[int, int]] = {}
    seen: set[str] = set()
    member_count = 0
    aggregate_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
            for member in tree:
                member_count += 1
                if member_count > _MAX_DOCKER_ARCHIVE_MEMBERS:
                    raise BuildError("docker_archive_member_limit")
                path = _safe_archive_member_path(member.name)
                if len(path.encode("utf-8")) > _MAX_DOCKER_ARCHIVE_PATH_BYTES:
                    raise BuildError("docker_archive_path_limit")
                if path in seen:
                    raise BuildError("docker_archive_duplicate_entry")
                seen.add(path)
                sparse = getattr(member, "sparse", None)
                if member.issparse() or sparse or any(
                    "sparse" in key.lower() for key in member.pax_headers
                ):
                    raise BuildError("docker_archive_sparse_entry")
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise BuildError("docker_archive_unsafe_entry")
                if member.size < 0:
                    raise BuildError("docker_archive_invalid")
                aggregate_size += member.size
                if aggregate_size > len(archive):
                    raise BuildError("docker_archive_aggregate_limit")
                start = member.offset_data
                end = start + member.size
                if start < 0 or end < start or end > len(archive):
                    raise BuildError("docker_archive_invalid")
                regular[path] = (start, member.size)
    except BuildError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError, OverflowError):
        raise BuildError("docker_archive_invalid") from None
    return regular


def _archive_member_bytes(
    archive: bytes,
    members: Mapping[str, tuple[int, int]],
    path: str,
    *,
    maximum: int,
    error_code: str,
) -> bytes:
    location = members.get(path)
    if location is None or location[1] > maximum:
        raise BuildError(error_code)
    start, size = location
    return bytes(memoryview(archive)[start : start + size])


def _docker_archive_oci_evidence(
    archive: bytes,
    *,
    expected_image_id: str,
    expected_diff_ids: Sequence[str],
    expected_labels: Mapping[str, str],
) -> tuple[str, Mapping[str, Any]]:
    members = _docker_archive_members(archive)
    manifest_raw = _archive_member_bytes(
        archive,
        members,
        "manifest.json",
        maximum=_MAX_DOCKER_ARCHIVE_MANIFEST_BYTES,
        error_code="docker_archive_manifest_missing",
    )
    manifest_list = _strict_json(manifest_raw, error_code="docker_archive_manifest_invalid")
    if not isinstance(manifest_list, list) or len(manifest_list) != 1:
        raise BuildError("docker_archive_manifest_invalid")
    record_value = manifest_list[0]
    if not isinstance(record_value, dict):
        raise BuildError("docker_archive_manifest_invalid")
    legacy_keys = {"Config", "RepoTags", "Layers"}
    modern_keys = legacy_keys | {"LayerSources"}
    if frozenset(record_value) not in {
        frozenset(legacy_keys),
        frozenset(modern_keys),
    }:
        raise BuildError("docker_archive_manifest_invalid")
    record = record_value
    config_path = _string(record["Config"], "docker_archive_manifest_invalid")
    _safe_archive_member_path(config_path)
    image_hex = expected_image_id.removeprefix("sha256:")
    if config_path not in {
        image_hex + ".json",
        "blobs/sha256/" + image_hex,
    }:
        raise BuildError("docker_archive_config_mismatch")
    layer_paths = _string_list(record["Layers"], "docker_archive_manifest_invalid")
    if not layer_paths or len(layer_paths) != len(set(layer_paths)):
        raise BuildError("docker_archive_manifest_invalid")
    if record["RepoTags"] is not None:
        _string_list(record["RepoTags"], "docker_archive_manifest_invalid")
    config_raw = _archive_member_bytes(
        archive,
        members,
        config_path,
        maximum=_MAX_DOCKER_ARCHIVE_CONFIG_BYTES,
        error_code="docker_archive_config_mismatch",
    )
    if _sha256(config_raw) != expected_image_id:
        raise BuildError("docker_archive_config_mismatch")
    config_document = _strict_json(config_raw, error_code="docker_archive_config_invalid")
    if not isinstance(config_document, dict):
        raise BuildError("docker_archive_config_invalid")
    if config_document.get("architecture") != "amd64" or config_document.get("os") != "linux":
        raise BuildError("docker_archive_config_invalid")
    rootfs = _exact_mapping(
        config_document.get("rootfs"), {"type", "diff_ids"}, "docker_archive_config_invalid"
    )
    config_diff_ids = _string_list(rootfs["diff_ids"], "docker_archive_config_invalid")
    if rootfs["type"] != "layers" or config_diff_ids != list(expected_diff_ids):
        raise BuildError("docker_archive_rootfs_mismatch")
    config_config = config_document.get("config")
    if not isinstance(config_config, dict) or not isinstance(config_config.get("Labels"), dict):
        raise BuildError("docker_archive_config_invalid")
    if any(config_config["Labels"].get(key) != value for key, value in expected_labels.items()):
        raise BuildError("docker_archive_label_mismatch")
    if len(layer_paths) != len(config_diff_ids):
        raise BuildError("docker_archive_layer_count_mismatch")

    layer_sources = record.get("LayerSources")
    if layer_sources is not None:
        if not isinstance(layer_sources, dict) or set(layer_sources) != set(config_diff_ids):
            raise BuildError("docker_archive_layer_sources_invalid")

    layers: list[dict[str, Any]] = []
    for path, diff_id in zip(layer_paths, config_diff_ids, strict=True):
        _safe_archive_member_path(path)
        if layer_sources is not None and path != (
            "blobs/sha256/" + diff_id.removeprefix("sha256:")
        ):
            raise BuildError("docker_archive_layer_sources_invalid")
        location = members.get(path)
        if location is None:
            raise BuildError("docker_archive_layer_digest_mismatch")
        start, layer_size = location
        layer_view = memoryview(archive)[start : start + layer_size]
        if "sha256:" + hashlib.sha256(layer_view).hexdigest() != diff_id:
            raise BuildError("docker_archive_layer_digest_mismatch")
        if layer_sources is not None:
            descriptor = _exact_mapping(
                layer_sources.get(diff_id),
                {"mediaType", "size", "digest"},
                "docker_archive_layer_sources_invalid",
            )
            if (
                descriptor["mediaType"] != OCI_LAYER_MEDIA_TYPE
                or descriptor["digest"] != diff_id
                or isinstance(descriptor["size"], bool)
                or not isinstance(descriptor["size"], int)
                or descriptor["size"] != layer_size
            ):
                raise BuildError("docker_archive_layer_sources_invalid")
        layers.append(
            {
                "media_type": OCI_LAYER_MEDIA_TYPE,
                "digest": diff_id,
                "size": layer_size,
            }
        )
    descriptor = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "digest": expected_image_id,
            "size": len(config_raw),
        },
        "layers": [
            {
                "mediaType": item["media_type"],
                "digest": item["digest"],
                "size": item["size"],
            }
            for item in layers
        ],
    }
    manifest_bytes = _canonical_json_document(descriptor)
    digest = _sha256(manifest_bytes)
    return digest, {
        "construction": OCI_CONSTRUCTION,
        "docker_archive_sha256": _sha256(archive),
        "media_type": OCI_MANIFEST_MEDIA_TYPE,
        "manifest_size": len(manifest_bytes),
        "config": {
            "media_type": OCI_CONFIG_MEDIA_TYPE,
            "digest": expected_image_id,
            "size": len(config_raw),
        },
        "layers": layers,
    }


def _parse_daemon_id(data: bytes) -> str:
    value = _strict_json(data, error_code="docker_daemon_identity_invalid")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise BuildError("docker_daemon_identity_invalid")
    return value


def _audit_buildx_config(path: Path) -> None:
    member_count = 0
    aggregate_bytes = 0
    try:
        root = os.lstat(path)
        if (
            not _BUILDX_CONFIG_NAME_RE.fullmatch(path.name)
            or stat.S_ISLNK(root.st_mode)
            or not stat.S_ISDIR(root.st_mode)
            or stat.S_IMODE(root.st_mode) != 0o700
            or root.st_uid != os.geteuid()
        ):
            raise BuildError("buildx_config_mutated")
        for current_path, directory_names, file_names in os.walk(
            path, topdown=True, followlinks=False
        ):
            current = Path(current_path)
            current_metadata = os.lstat(current)
            if (
                stat.S_ISLNK(current_metadata.st_mode)
                or not stat.S_ISDIR(current_metadata.st_mode)
                or stat.S_IMODE(current_metadata.st_mode) != 0o700
                or current_metadata.st_uid != os.geteuid()
            ):
                raise BuildError("buildx_config_mutated")
            for name in (*directory_names, *file_names):
                member_count += 1
                candidate = current / name
                if (
                    member_count > _MAX_BUILDX_CONFIG_MEMBERS
                    or len(os.fsencode(candidate)) > _MAX_DOCKER_ARCHIVE_PATH_BYTES
                    or name in {"", ".", ".."}
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                ):
                    raise BuildError("buildx_config_mutated")
                metadata = os.lstat(candidate)
                if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise BuildError("buildx_config_mutated")
                if stat.S_ISDIR(metadata.st_mode):
                    if stat.S_IMODE(metadata.st_mode) != 0o700:
                        raise BuildError("buildx_config_mutated")
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o644}
                    or metadata.st_nlink != 1
                    or metadata.st_size < 0
                    or metadata.st_size > _MAX_BUILDX_CONFIG_FILE_BYTES
                ):
                    raise BuildError("buildx_config_mutated")
                aggregate_bytes += metadata.st_size
                if aggregate_bytes > _MAX_BUILDX_CONFIG_AGGREGATE_BYTES:
                    raise BuildError("buildx_config_mutated")
    except BuildError:
        raise
    except OSError:
        raise BuildError("buildx_config_mutated") from None


def _create_buildx_config(root: Path) -> Path:
    path: Path | None = None
    try:
        path = Path(tempfile.mkdtemp(prefix=".pq-buildx-", dir=root))
        path.chmod(0o700)
        _audit_buildx_config(path)
        return path
    except BuildError:
        raise
    except OSError:
        if path is not None:
            try:
                os.rmdir(path)
            except OSError:
                raise BuildError("buildx_config_partial_cleanup_failed") from None
        raise BuildError("buildx_config_create_failed") from None


def _remove_buildx_config(path: Path) -> None:
    _audit_buildx_config(path)
    try:
        for current_path, directory_names, file_names in os.walk(
            path, topdown=False, followlinks=False
        ):
            current = Path(current_path)
            for name in file_names:
                os.unlink(current / name)
            for name in directory_names:
                os.rmdir(current / name)
        os.rmdir(path)
    except OSError:
        raise BuildError("buildx_config_cleanup_failed") from None


def _create_docker_config(root: Path) -> Path:
    path: Path | None = None
    root_identity: tuple[int, int] | None = None
    try:
        path = Path(tempfile.mkdtemp(prefix=".pq-build-docker-", dir=root))
        created = os.lstat(path)
        root_identity = (created.st_dev, created.st_ino)
        path.chmod(0o700)
        descriptor = os.open(
            path / "config.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            payload = b"{}\n"
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise BuildError("docker_config_create_failed")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _audit_docker_config(path)
        return path
    except BaseException as error:
        if path is not None and root_identity is not None:
            try:
                current = os.lstat(path)
                if (current.st_dev, current.st_ino) != root_identity or not stat.S_ISDIR(
                    current.st_mode
                ):
                    raise BuildError("docker_config_partial_cleanup_failed")
                names = os.listdir(path)
                if set(names).difference({"config.json"}):
                    raise BuildError("docker_config_partial_cleanup_failed")
                if "config.json" in names:
                    leaf = os.lstat(path / "config.json")
                    if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode):
                        raise BuildError("docker_config_partial_cleanup_failed")
                    os.unlink(path / "config.json")
                os.rmdir(path)
            except BuildError:
                raise
            except OSError:
                raise BuildError("docker_config_partial_cleanup_failed") from None
        if isinstance(error, BuildError):
            raise error
        if isinstance(error, OSError):
            raise BuildError("docker_config_create_failed") from None
        raise


def _audit_docker_config(path: Path) -> None:
    try:
        top = os.lstat(path)
        names = os.listdir(path)
        config = os.lstat(path / "config.json")
        raw = (path / "config.json").read_bytes()
    except OSError:
        raise BuildError("docker_config_mutated") from None
    if (
        not _DOCKER_CONFIG_NAME_RE.fullmatch(path.name)
        or stat.S_ISLNK(top.st_mode)
        or not stat.S_ISDIR(top.st_mode)
        or stat.S_IMODE(top.st_mode) != 0o700
        or top.st_uid != os.geteuid()
        or names != ["config.json"]
        or stat.S_ISLNK(config.st_mode)
        or not stat.S_ISREG(config.st_mode)
        or stat.S_IMODE(config.st_mode) != 0o600
        or config.st_uid != os.geteuid()
        or config.st_nlink != 1
        or raw != b"{}\n"
    ):
        raise BuildError("docker_config_mutated")


def _remove_docker_config(path: Path) -> None:
    _audit_docker_config(path)
    try:
        os.unlink(path / "config.json")
        os.rmdir(path)
    except OSError:
        raise BuildError("docker_config_cleanup_failed") from None


def _discard_owned_docker_config(path: Path) -> None:
    """Remove only the still-private config directory created by this process."""

    try:
        top = os.lstat(path)
        names = os.listdir(path)
        if (
            not _DOCKER_CONFIG_NAME_RE.fullmatch(path.name)
            or stat.S_ISLNK(top.st_mode)
            or not stat.S_ISDIR(top.st_mode)
            or stat.S_IMODE(top.st_mode) != 0o700
            or top.st_uid != os.geteuid()
            or set(names).difference({"config.json"})
        ):
            raise BuildError("docker_config_cleanup_failed")
        if "config.json" in names:
            leaf = os.lstat(path / "config.json")
            if (
                stat.S_ISLNK(leaf.st_mode)
                or not stat.S_ISREG(leaf.st_mode)
                or leaf.st_uid != os.geteuid()
                or leaf.st_nlink != 1
            ):
                raise BuildError("docker_config_cleanup_failed")
            os.unlink(path / "config.json")
        os.rmdir(path)
    except BuildError:
        raise
    except OSError:
        raise BuildError("docker_config_cleanup_failed") from None


def _cleanup_built_tag(
    evidence: _Evidence,
    *,
    tag: str,
    expected_image_id: str,
) -> None:
    try:
        observed = evidence.docker(
            "image",
            "inspect",
            tag,
            error_code="local_build_cleanup_failed",
            allowed_returncodes=frozenset({0, 1}),
        )
        if observed.returncode == 1:
            return
        image = _docker_inspect_object(observed.stdout, "local_build_cleanup_failed")
        if image.get("Id") != expected_image_id:
            raise BuildError("local_build_cleanup_identity_mismatch")
        evidence.docker(
            "image",
            "rm",
            "--no-prune",
            tag,
            error_code="local_build_cleanup_failed",
        )
        after = evidence.docker(
            "image",
            "inspect",
            tag,
            error_code="local_build_cleanup_failed",
            allowed_returncodes=frozenset({0, 1}),
        )
        if after.returncode != 1:
            raise BuildError("local_build_cleanup_failed")
    except BuildError:
        raise
    except Exception:
        raise BuildError("local_build_cleanup_failed") from None


def _cleanup_owned_tags(
    evidence: _Evidence,
    *,
    tags: Sequence[str],
    expected_image_id: str,
) -> None:
    unique_tags = tuple(dict.fromkeys(tags))
    for tag in reversed(unique_tags):
        _cleanup_built_tag(
            evidence,
            tag=tag,
            expected_image_id=expected_image_id,
        )


def _cleanup_built_tag_with_fresh_config(
    config: BuildConfig,
    executor: CommandExecutor,
    *,
    expected_image_id: str,
    tags: Sequence[str] | None = None,
) -> None:
    docker_config = _create_docker_config(config.receipt_root)
    try:
        buildx_config = _create_buildx_config(config.receipt_root)
    except BaseException:
        _remove_docker_config(docker_config)
        raise
    cleanup_error: BaseException | None = None
    try:
        _cleanup_owned_tags(
            _Evidence(config, executor, docker_config, buildx_config),
            tags=(config.local_image_tag,) if tags is None else tags,
            expected_image_id=expected_image_id,
        )
    except BaseException as error:
        cleanup_error = error
    try:
        _remove_docker_config(docker_config)
    except BaseException as error:
        cleanup_error = cleanup_error or error
    try:
        _remove_buildx_config(buildx_config)
    except BaseException as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        if isinstance(cleanup_error, BuildError):
            raise cleanup_error
        raise BuildError("local_build_cleanup_failed") from None


def _atomic_write_receipt(path: Path, root: Path, data: bytes) -> None:
    root_fd = -1
    temp_fd = -1
    temp_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    temp_identity: tuple[int, int] | None = None
    completed = False
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_meta = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_meta.st_mode)
            or stat.S_IMODE(root_meta.st_mode) != 0o700
            or root_meta.st_uid != os.geteuid()
        ):
            raise BuildError("receipt_root_invalid")
        try:
            os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BuildError("receipt_already_exists")
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        opened = os.fstat(temp_fd)
        temp_identity = (opened.st_dev, opened.st_ino)
        offset = 0
        while offset < len(data):
            written = os.write(temp_fd, data[offset:])
            if written <= 0:
                raise BuildError("receipt_write_failed")
            offset += written
        os.fsync(temp_fd)
        os.link(
            temp_name,
            path.name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
            follow_symlinks=False,
        )
        os.unlink(temp_name, dir_fd=root_fd)
        temp_name = ""
        os.fsync(root_fd)
        final = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
        if (
            temp_identity != (final.st_dev, final.st_ino)
            or not stat.S_ISREG(final.st_mode)
            or stat.S_IMODE(final.st_mode) != 0o600
            or final.st_nlink != 1
            or final.st_size != len(data)
        ):
            raise BuildError("receipt_write_failed")
        completed = True
    except BuildError:
        raise
    except FileExistsError:
        raise BuildError("receipt_already_exists") from None
    except OSError:
        raise BuildError("receipt_write_failed") from None
    finally:
        cleanup_error = False
        if temp_fd >= 0:
            os.close(temp_fd)
        if root_fd >= 0:
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_error = True
            if not completed and temp_identity is not None:
                try:
                    final = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_error = True
                else:
                    if temp_identity == (final.st_dev, final.st_ino):
                        try:
                            os.unlink(path.name, dir_fd=root_fd)
                            os.fsync(root_fd)
                        except OSError:
                            cleanup_error = True
                    else:
                        cleanup_error = True
            os.close(root_fd)
        if cleanup_error:
            raise BuildError("receipt_cleanup_failed")


def produce_build_receipt(
    config: BuildConfig,
    *,
    executor: CommandExecutor | None = None,
    registry_observer: RegistryObserver | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> BuildResult:
    """Perform one explicitly authorized build and publish its receipt."""

    _validate_config(config)
    dockerfile_path = _dockerfile_path(config)
    _require_receipt_absent(config.receipt_path)
    if config.execute_registry_publication and registry_observer is None:
        registry_observer = LoopbackRegistryObserver()
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    git_binary_before = _trusted_binary_identity(TRUSTED_GIT_BIN)
    docker_binary_before = _trusted_binary_identity(TRUSTED_DOCKER_BIN)
    socket_before = _local_docker_socket_identity()
    docker_config = _create_docker_config(config.receipt_root)
    try:
        buildx_config = _create_buildx_config(config.receipt_root)
    except BaseException:
        _remove_docker_config(docker_config)
        raise
    executor = executor or BoundedSubprocessExecutor(buildx_config=buildx_config)
    receipt: Mapping[str, Any] | None = None
    built_image_id: str | None = None
    owned_local_tags: list[str] = []
    build_started = False
    operation_succeeded = False
    evidence = _Evidence(config, executor, docker_config, buildx_config)
    try:
        git_first = _observe_git(config, evidence)
        base_references = _dockerfile_base_references(git_first.dockerfile)
        daemon_first = _parse_daemon_id(
            evidence.docker(
                "system",
                "info",
                "--format",
                "{{json .ID}}",
                error_code="docker_daemon_identity_failed",
            ).stdout
        )
        absent = evidence.docker(
            "image",
            "inspect",
            config.local_image_tag,
            error_code="local_image_tag_preflight_failed",
            allowed_returncodes=frozenset({0, 1}),
        )
        if absent.returncode == 0:
            raise BuildError("local_image_tag_already_exists")

        bases_first: list[Mapping[str, Any]] = []
        for reference in base_references:
            inspected = _docker_inspect_object(
                evidence.docker(
                    "image", "inspect", reference, error_code="base_image_inspect_failed"
                ).stdout,
                "base_image_inspect_invalid",
            )
            bases_first.append(_base_image_evidence(reference, inspected))

        labels = {
            "org.opencontainers.image.revision": config.source_candidate_sha,
            "com.propertyquarry.metadata-envelope": config.metadata_envelope_sha,
            "com.propertyquarry.release-manifest-sha256": git_first.release_manifest_sha256,
        }
        build_arguments: list[str] = [
            "image",
            "build",
            "--network",
            "none",
            "--pull=false",
            "--platform",
            "linux/amd64",
            "--quiet",
            "--file",
            dockerfile_path,
            "--tag",
            config.local_image_tag,
        ]
        for key in REQUIRED_IMAGE_LABELS:
            build_arguments.extend(("--label", f"{key}={labels[key]}"))
        build_arguments.append("-")

        def capture_build_identity(result: CommandResult) -> None:
            nonlocal built_image_id
            built_image_id = _one_line_digest(
                result.stdout, "local_docker_build_id_invalid"
            )
            owned_local_tags.append(config.local_image_tag)

        build_started = True
        evidence.docker(
            *build_arguments,
            error_code="local_docker_build_failed",
            input_data=git_first.envelope_archive,
            timeout_s=config.docker_build_timeout_s,
            success_observer=capture_build_identity,
        )
        assert built_image_id is not None

        image_first_object = _docker_inspect_object(
            evidence.docker(
                "image", "inspect", config.local_image_tag, error_code="built_image_inspect_failed"
            ).stdout,
            "built_image_inspect_invalid",
        )
        image_first = _image_evidence(
            image_first_object, tag=config.local_image_tag, labels=labels
        )
        image_id = _string(image_first["image_config_id"], "built_image_inspect_invalid")
        if image_id != built_image_id:
            raise BuildError("local_docker_build_id_mismatch")
        archive = evidence.docker(
            "image",
            "save",
            image_id,
            error_code="built_image_save_failed",
            timeout_s=config.docker_save_timeout_s,
            max_stdout_bytes=config.max_image_archive_bytes,
        ).stdout
        oci_digest, local_oci = _docker_archive_oci_evidence(
            archive,
            expected_image_id=image_id,
            expected_diff_ids=image_first["rootfs_diff_ids"],
            expected_labels=labels,
        )

        image_second_object = _docker_inspect_object(
            evidence.docker(
                "image", "inspect", image_id, error_code="built_image_reinspect_failed"
            ).stdout,
            "built_image_inspect_invalid",
        )
        image_second = _image_evidence(
            image_second_object, tag=config.local_image_tag, labels=labels
        )
        if image_second != image_first:
            raise BuildError("built_image_changed_during_receipt")
        for base_first in bases_first:
            image_config_id = _string(
                base_first.get("image_config_id"), "base_image_reinspect_invalid"
            )
            inspected = _docker_inspect_object(
                evidence.docker(
                    "image",
                    "inspect",
                    image_config_id,
                    error_code="base_image_reinspect_failed",
                ).stdout,
                "base_image_reinspect_invalid",
            )
            _reinspect_base_image(base_first, inspected)
        daemon_second = _parse_daemon_id(
            evidence.docker(
                "system",
                "info",
                "--format",
                "{{json .ID}}",
                error_code="docker_daemon_identity_failed",
            ).stdout
        )
        if daemon_second != daemon_first:
            raise BuildError("docker_daemon_changed_during_build")
        git_second = _observe_git(config, evidence)
        if git_second != git_first:
            raise BuildError("git_inputs_changed_during_build")
        if _trusted_binary_identity(TRUSTED_GIT_BIN) != git_binary_before:
            raise BuildError("command_binary_changed")
        if _trusted_binary_identity(TRUSTED_DOCKER_BIN) != docker_binary_before:
            raise BuildError("command_binary_changed")
        if _local_docker_socket_identity() != socket_before:
            raise BuildError("docker_socket_changed")

        registry_publication: Mapping[str, Any] | None = None
        if config.execute_registry_publication:
            assert registry_observer is not None
            registry_publication = _publish_to_loopback_registry(
                config,
                evidence,
                registry_observer,
                image_id=image_id,
                diff_ids=image_first["rootfs_diff_ids"],
                labels=labels,
                owned_local_tags=owned_local_tags,
            )
            daemon_after_publication = _parse_daemon_id(
                evidence.docker(
                    "system",
                    "info",
                    "--format",
                    "{{json .ID}}",
                    error_code="docker_daemon_identity_failed",
                ).stdout
            )
            if daemon_after_publication != daemon_first:
                raise BuildError("docker_daemon_changed_during_publication")
            if _trusted_binary_identity(TRUSTED_GIT_BIN) != git_binary_before:
                raise BuildError("command_binary_changed")
            if _trusted_binary_identity(TRUSTED_DOCKER_BIN) != docker_binary_before:
                raise BuildError("command_binary_changed")
            if _local_docker_socket_identity() != socket_before:
                raise BuildError("docker_socket_changed")

        generated = now()
        if generated.tzinfo is None or generated.utcoffset() != dt.timedelta(0):
            raise BuildError("invalid_clock")
        generated_at = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if not _RFC3339_UTC_SECONDS_RE.fullmatch(generated_at):
            raise BuildError("invalid_clock")
        published = registry_publication is not None
        receipt_payload: dict[str, Any] = {
            "schema": (
                PUBLISHED_BUILD_RECEIPT_SCHEMA if published else BUILD_RECEIPT_SCHEMA
            ),
            "generated_at": generated_at,
            "source_candidate_sha": config.source_candidate_sha,
            "metadata_envelope_sha": config.metadata_envelope_sha,
            "source_candidate": {
                "tree_sha": git_first.candidate_tree,
                "archive_sha256": _sha256(git_first.candidate_archive),
            },
            "metadata_envelope": {
                "tree_sha": git_first.envelope_tree,
                "archive_sha256": _sha256(git_first.envelope_archive),
                "changed_paths": list(git_first.changed_paths),
            },
            "dockerfile": {
                "path": dockerfile_path,
                "sha256": _sha256(git_first.dockerfile),
            },
            "release_manifest": {
                "path": RELEASE_MANIFEST_PATH,
                "file_sha256": _sha256(git_first.release_manifest),
                "manifest_sha256": git_first.release_manifest_sha256,
            },
            "labels": labels,
            "base_images": bases_first,
            "local_build": {
                "image_tag": config.local_image_tag,
                "platform": "linux/amd64",
                "network_mode": "none",
                "pull": False,
                "docker_daemon_id_sha256": _sha256(daemon_first.encode("ascii")),
            },
            "image_reference": (
                registry_publication["immutable_reference"]
                if registry_publication is not None
                else image_id
            ),
            "image_config_id": image_id,
            "oci_manifest_digest": oci_digest,
            "local_oci_manifest": local_oci,
            "authority": (
                {
                    "local_only": False,
                    "performs_local_docker_build": True,
                    "performs_registry_publication": True,
                    "authoritative_for_release_effects": False,
                    "public_launch_authority": False,
                    "production_ready": False,
                }
                if published
                else {
                    "local_only": True,
                    "performs_local_docker_build": True,
                    "authoritative_for_release_effects": False,
                    "public_launch_authority": False,
                    "production_ready": False,
                }
            ),
        }
        if registry_publication is not None:
            receipt_payload["registry_publication"] = registry_publication
        receipt = receipt_payload
        operation_succeeded = True
    except BaseException:
        cleanup_with_fresh_config = False
        try:
            if build_started and built_image_id is not None:
                _cleanup_owned_tags(
                    evidence,
                    tags=owned_local_tags,
                    expected_image_id=built_image_id,
                )
        except BaseException:
            cleanup_with_fresh_config = True
        if cleanup_with_fresh_config:
            assert build_started and built_image_id is not None
            _cleanup_built_tag_with_fresh_config(
                config,
                executor,
                expected_image_id=built_image_id,
                tags=owned_local_tags,
            )
        raise
    finally:
        config_cleanup_error: BaseException | None = None
        try:
            _remove_docker_config(docker_config)
        except BaseException as error:
            config_cleanup_error = error
            try:
                _discard_owned_docker_config(docker_config)
            except BaseException as discard_error:
                config_cleanup_error = discard_error
        try:
            _remove_buildx_config(buildx_config)
        except BaseException as error:
            config_cleanup_error = config_cleanup_error or error
        if config_cleanup_error is not None:
            if operation_succeeded and built_image_id is not None:
                _cleanup_built_tag_with_fresh_config(
                    config,
                    executor,
                    expected_image_id=built_image_id,
                    tags=owned_local_tags,
                )
            if operation_succeeded:
                raise config_cleanup_error

    assert receipt is not None
    raw = _canonical_json_bytes(receipt)
    try:
        _atomic_write_receipt(config.receipt_path, config.receipt_root, raw)
    except BaseException:
        assert built_image_id is not None
        _cleanup_built_tag_with_fresh_config(
            config,
            executor,
            expected_image_id=built_image_id,
            tags=owned_local_tags,
        )
        raise
    return BuildResult(receipt=receipt, receipt_sha256=_sha256(raw))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build one authenticated PropertyQuarry candidate locally, with optional "
            "explicit loopback-registry publication."
        )
    )
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--source-candidate-sha", required=True)
    parser.add_argument("--metadata-envelope-sha", required=True)
    parser.add_argument("--local-image-tag", required=True)
    parser.add_argument(
        "--image-kind",
        choices=tuple(IMAGE_KIND_DOCKERFILE_PATHS),
        default="web",
    )
    parser.add_argument("--output-receipt", required=True, type=Path)
    parser.add_argument("--execute-local-build", required=True, action="store_true")
    parser.add_argument("--registry-repository")
    parser.add_argument("--execute-registry-publication", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = produce_build_receipt(
            BuildConfig(
                repo_root=arguments.repo_root,
                receipt_root=arguments.receipt_root,
                source_candidate_sha=arguments.source_candidate_sha,
                metadata_envelope_sha=arguments.metadata_envelope_sha,
                local_image_tag=arguments.local_image_tag,
                receipt_path=arguments.output_receipt,
                execute_local_build=arguments.execute_local_build,
                image_kind=arguments.image_kind,
                execute_registry_publication=arguments.execute_registry_publication,
                registry_repository=arguments.registry_repository,
            )
        )
    except BuildError as error:
        print(f"error:{error.code}", file=sys.stderr)
        return 1
    summary = {
        "schema": result.receipt["schema"],
        "receipt_sha256": result.receipt_sha256,
        "image_config_id": result.receipt["image_config_id"],
        "oci_manifest_digest": result.receipt["oci_manifest_digest"],
        "local_only": result.receipt["authority"]["local_only"],
    }
    publication = result.receipt.get("registry_publication")
    if isinstance(publication, dict):
        summary["image_reference"] = publication["immutable_reference"]
        summary["registry_manifest_digest"] = publication["manifest_digest"]
    sys.stdout.buffer.write(_canonical_json_bytes(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
