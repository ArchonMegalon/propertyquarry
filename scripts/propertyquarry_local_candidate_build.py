#!/usr/bin/env python3
"""Produce a fail-closed, local-only PropertyQuarry candidate build receipt.

The Docker build context is an authenticated ``git archive`` byte string held
in memory.  The live worktree, untracked files, ignored files, Docker registry,
and release-control surfaces are not build inputs or authorities.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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

# Direct script execution sets ``sys.path[0]`` to ``scripts/`` rather than the
# repository root.  Keep the documented ``python3 scripts/...`` entrypoint
# self-contained instead of relying on an ambient PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if __package__ in {None, ""}:
    for bootstrap_root in (EA_ROOT, ROOT):
        bootstrap_path = str(bootstrap_root)
        if bootstrap_path not in sys.path:
            sys.path.insert(0, bootstrap_path)

from scripts import verify_generated_release_artifacts_clean as manifest_model


BUILD_RECEIPT_SCHEMA = "propertyquarry.local_candidate_build.v1"
BUILT_IMAGE_SMOKE_CONTRACT = "propertyquarry.built_image_import_start_smoke.v1"
BUILT_IMAGE_SMOKE_LABEL = "com.propertyquarry.local-smoke-nonce"
BUILT_IMAGE_SMOKE_MARKER = b"propertyquarry-built-image-smoke-v1\n"
BUILT_IMAGE_SMOKE_SCRIPT = """\
import asyncio

from app.main import app


async def _propertyquarry_built_image_smoke() -> None:
    if str(app.url_path_for("health_live")) != "/health/live":
        raise RuntimeError("health route missing")
    async with app.router.lifespan_context(app):
        pass


asyncio.run(_propertyquarry_built_image_smoke())
print("propertyquarry-built-image-smoke-v1")
"""
BUILT_IMAGE_SMOKE_ENTRYPOINT = (
    "/usr/local/bin/python",
    "-I",
    "-S",
    "/usr/local/libexec/property_web_entrypoint.py",
)
BUILT_IMAGE_SMOKE_COMMAND = (
    "/usr/local/bin/python",
    "-c",
    BUILT_IMAGE_SMOKE_SCRIPT,
)
BUILT_IMAGE_SMOKE_ENVIRONMENT = (
    "EA_RUNTIME_MODE=test",
    "EA_STORAGE_BACKEND=memory",
    "EA_ARTIFACTS_DIR=/tmp/ea_artifacts",
    "EA_ROLE=api",
    "EA_PUBLIC_RESULTS_ENABLED=1",
    "EA_PUBLIC_TOURS_ENABLED=1",
    "EA_LEGACY_RUNTIME_SURFACES_ENABLED=0",
)
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
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_REPO_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_DOCKER_CONFIG_NAME_RE = re.compile(r"\.pq-build-docker-[A-Za-z0-9_-]+\Z")
_DOCKER_BUILDX_DIRECTORY_LAYOUT: Mapping[str, frozenset[str]] = {
    "buildx": frozenset(
        {".buildNodeID", ".lock", "activity", "defaults", "instances", "refs"}
    ),
    "buildx/activity": frozenset({"default"}),
    "buildx/defaults": frozenset(),
    "buildx/instances": frozenset(),
    "buildx/refs": frozenset({"default"}),
    "buildx/refs/default": frozenset({"default"}),
    "buildx/refs/default/default": frozenset(),
}
_DOCKER_BUILDX_FILE_PATHS = (
    "buildx/.buildNodeID",
    "buildx/.lock",
    "buildx/activity/default",
)
_DOCKER_BUILDX_MAX_FILE_BYTES = 4_096
_DOCKER_BUILDX_REF_ID_RE = re.compile(r"[a-z0-9]{20,64}\Z")
_DOCKER_BUILDX_MAX_REF_FILES = 8
_FROM_RE = re.compile(
    r"FROM[ \t]+(?P<reference>[^ \t]+@sha256:[0-9a-f]{64})"
    r"(?:[ \t]+AS[ \t]+(?P<alias>[A-Za-z0-9_.-]+))?[ \t]*\Z",
    flags=re.IGNORECASE,
)
_FROM_SCRATCH_RE = re.compile(
    r"FROM[ \t]+scratch(?:[ \t]+AS[ \t]+(?P<alias>[A-Za-z0-9_.-]+))?[ \t]*\Z",
    flags=re.IGNORECASE,
)
_COPY_FROM_LOCAL_STAGE_RE = re.compile(
    r"--from=(?P<source>[A-Za-z0-9_.-]+)[ \t]+.+\Z",
    flags=re.IGNORECASE,
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


@dataclass(frozen=True)
class BuildConfig:
    repo_root: Path
    receipt_root: Path
    source_candidate_sha: str
    metadata_envelope_sha: str
    local_image_tag: str
    receipt_path: Path
    execute_local_build: bool
    git_timeout_s: float = 30.0
    docker_inspect_timeout_s: float = 30.0
    docker_build_timeout_s: float = 1_800.0
    docker_save_timeout_s: float = 300.0
    docker_smoke_timeout_s: float = 120.0
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
                env=self._ENVIRONMENT,
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
    if config.local_image_tag in {
        LOCAL_IMAGE_TAG_PREFIX + "latest",
        LOCAL_IMAGE_TAG_PREFIX + "main",
        LOCAL_IMAGE_TAG_PREFIX + "production",
    }:
        raise BuildError("invalid_local_image_tag")
    timeout_limits = (
        (config.git_timeout_s, 120.0),
        (config.docker_inspect_timeout_s, 120.0),
        (config.docker_build_timeout_s, 7_200.0),
        (config.docker_save_timeout_s, 1_200.0),
        (config.docker_smoke_timeout_s, 300.0),
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


class _Evidence:
    def __init__(
        self,
        config: BuildConfig,
        executor: CommandExecutor,
        docker_config: Path,
    ) -> None:
        self.config = config
        self.executor = executor
        self.docker_config = docker_config

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


def _one_line_container_id(data: bytes, error_code: str) -> str:
    try:
        value = data.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise BuildError(error_code) from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise BuildError(error_code)
    value = value[:-1]
    if not _CONTAINER_ID_RE.fullmatch(value):
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
) -> tuple[bytes, bytes]:
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
                    if path in {DOCKERFILE_PATH, RELEASE_MANIFEST_PATH}:
                        raise BuildError("build_input_too_large")
                    continue
                if path not in {DOCKERFILE_PATH, RELEASE_MANIFEST_PATH}:
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
    if set(files) != {DOCKERFILE_PATH, RELEASE_MANIFEST_PATH}:
        raise BuildError("git_archive_required_file_missing")
    return files[DOCKERFILE_PATH], files[RELEASE_MANIFEST_PATH]


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
        candidate_archive, max_file_bytes=config.max_input_file_bytes
    )
    dockerfile, release_manifest = _archive_required_files(
        envelope_archive, max_file_bytes=config.max_input_file_bytes
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
    stage_aliases: set[str] = set()
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
            scratch_match = _FROM_SCRATCH_RE.fullmatch(logical)
            if match is None and scratch_match is None:
                raise BuildError("dockerfile_base_not_digest_pinned")
            alias = (
                match.group("alias")
                if match is not None
                else scratch_match.group("alias")
            )
            normalized_alias = str(alias or "").strip().lower()
            if normalized_alias:
                if normalized_alias == "scratch" or normalized_alias in stage_aliases:
                    raise BuildError("dockerfile_local_only_contract_invalid")
                stage_aliases.add(normalized_alias)
            if match is not None:
                references.append(match.group("reference"))
        copy_from_present = instruction == "COPY" and re.search(
            r"(?:^|[ \t])--FROM(?:=|[ \t])",
            body.upper(),
        )
        if copy_from_present:
            local_copy = _COPY_FROM_LOCAL_STAGE_RE.fullmatch(body)
            if (
                local_copy is None
                or local_copy.group("source").lower() not in stage_aliases
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
    return {
        "reference": reference,
        "image_config_id": image_id,
        "observed_repo_digest": matching[0],
    }


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
    legacy_keys = {"Config", "RepoTags", "Layers"}
    modern_keys = legacy_keys | {"LayerSources"}
    if not isinstance(record_value, dict) or frozenset(record_value) not in {
        frozenset(legacy_keys),
        frozenset(modern_keys),
    }:
        raise BuildError("docker_archive_manifest_invalid")
    record = record_value
    modern_layout = "LayerSources" in record
    config_path = _string(record["Config"], "docker_archive_manifest_invalid")
    _safe_archive_member_path(config_path)
    config_digest = expected_image_id.removeprefix("sha256:")
    expected_config_path = (
        f"blobs/sha256/{config_digest}" if modern_layout else config_digest + ".json"
    )
    if config_path != expected_config_path:
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
    layer_sources: Mapping[str, Any] | None = None
    if modern_layout:
        expected_layer_paths = [
            f"blobs/sha256/{diff_id.removeprefix('sha256:')}"
            for diff_id in config_diff_ids
        ]
        if layer_paths != expected_layer_paths:
            raise BuildError("docker_archive_layer_source_invalid")
        value = record["LayerSources"]
        if not isinstance(value, dict) or set(value) != set(config_diff_ids):
            raise BuildError("docker_archive_layer_source_invalid")
        for diff_id, source_value in value.items():
            source = _exact_mapping(
                source_value,
                {"mediaType", "size", "digest"},
                "docker_archive_layer_source_invalid",
            )
            size = source["size"]
            if (
                source["mediaType"] != OCI_LAYER_MEDIA_TYPE
                or source["digest"] != diff_id
                or type(size) is not int
                or size < 0
            ):
                raise BuildError("docker_archive_layer_source_invalid")
        layer_sources = value
    config_config = config_document.get("config")
    if not isinstance(config_config, dict) or not isinstance(config_config.get("Labels"), dict):
        raise BuildError("docker_archive_config_invalid")
    if any(config_config["Labels"].get(key) != value for key, value in expected_labels.items()):
        raise BuildError("docker_archive_label_mismatch")
    if len(layer_paths) != len(config_diff_ids):
        raise BuildError("docker_archive_layer_count_mismatch")

    layers: list[dict[str, Any]] = []
    for path, diff_id in zip(layer_paths, config_diff_ids, strict=True):
        _safe_archive_member_path(path)
        location = members.get(path)
        if location is None:
            raise BuildError("docker_archive_layer_digest_mismatch")
        start, layer_size = location
        layer_view = memoryview(archive)[start : start + layer_size]
        if "sha256:" + hashlib.sha256(layer_view).hexdigest() != diff_id:
            raise BuildError("docker_archive_layer_digest_mismatch")
        if layer_sources is not None and layer_sources[diff_id]["size"] != layer_size:
            raise BuildError("docker_archive_layer_source_invalid")
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
        names = set(os.listdir(path))
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
        or not {"config.json"} <= names <= {"config.json", "buildx"}
        or stat.S_ISLNK(config.st_mode)
        or not stat.S_ISREG(config.st_mode)
        or stat.S_IMODE(config.st_mode) != 0o600
        or config.st_uid != os.geteuid()
        or config.st_nlink != 1
        or raw != b"{}\n"
    ):
        raise BuildError("docker_config_mutated")
    if "buildx" not in names:
        return
    for relative, allowed_names in _DOCKER_BUILDX_DIRECTORY_LAYOUT.items():
        directory = path / relative
        try:
            metadata = os.lstat(directory)
            children = set(os.listdir(directory))
        except OSError:
            raise BuildError("docker_config_mutated") from None
        dynamic_refs = relative == "buildx/refs/default/default"
        unexpected_children = (
            len(children) > _DOCKER_BUILDX_MAX_REF_FILES
            or any(_DOCKER_BUILDX_REF_ID_RE.fullmatch(name) is None for name in children)
            if dynamic_refs
            else bool(children.difference(allowed_names))
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
            or unexpected_children
        ):
            raise BuildError("docker_config_mutated")
    for relative in _DOCKER_BUILDX_FILE_PATHS:
        candidate = path / relative
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise BuildError("docker_config_mutated") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > _DOCKER_BUILDX_MAX_FILE_BYTES
        ):
            raise BuildError("docker_config_mutated")
    ref_directory = path / "buildx/refs/default/default"
    try:
        ref_names = os.listdir(ref_directory)
    except OSError:
        raise BuildError("docker_config_mutated") from None
    for name in ref_names:
        candidate = ref_directory / name
        try:
            metadata = os.lstat(candidate)
        except OSError:
            raise BuildError("docker_config_mutated") from None
        if (
            _DOCKER_BUILDX_REF_ID_RE.fullmatch(name) is None
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o644}
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > _DOCKER_BUILDX_MAX_FILE_BYTES
        ):
            raise BuildError("docker_config_mutated")


def _unlink_docker_config_tree(path: Path) -> None:
    ref_directory = path / "buildx/refs/default/default"
    try:
        ref_names = os.listdir(ref_directory)
    except FileNotFoundError:
        ref_names = []
    for name in ref_names:
        os.unlink(ref_directory / name)
    for relative in reversed(_DOCKER_BUILDX_FILE_PATHS):
        candidate = path / relative
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
    for relative in reversed(tuple(_DOCKER_BUILDX_DIRECTORY_LAYOUT)):
        candidate = path / relative
        try:
            os.rmdir(candidate)
        except FileNotFoundError:
            pass
    os.unlink(path / "config.json")
    os.rmdir(path)


def _remove_docker_config(path: Path) -> None:
    _audit_docker_config(path)
    try:
        _unlink_docker_config_tree(path)
    except OSError:
        raise BuildError("docker_config_cleanup_failed") from None


def _discard_owned_docker_config(path: Path) -> None:
    """Remove only the still-private config directory created by this process."""

    try:
        _audit_docker_config(path)
        _unlink_docker_config_tree(path)
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


def _cleanup_built_tag_with_fresh_config(
    config: BuildConfig,
    executor: CommandExecutor,
    *,
    expected_image_id: str,
) -> None:
    docker_config = _create_docker_config(config.receipt_root)
    cleanup_error: BaseException | None = None
    try:
        _cleanup_built_tag(
            _Evidence(config, executor, docker_config),
            tag=config.local_image_tag,
            expected_image_id=expected_image_id,
        )
    except BaseException as error:
        cleanup_error = error
    try:
        _remove_docker_config(docker_config)
    except BaseException as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        if isinstance(cleanup_error, BuildError):
            raise cleanup_error
        raise BuildError("local_build_cleanup_failed") from None


def _smoke_container_identity(
    raw: bytes,
    *,
    expected_name: str,
    expected_container_id: str | None,
    expected_image_id: str,
    expected_nonce: str,
) -> Mapping[str, Any]:
    container = _docker_inspect_object(
        raw,
        "built_image_smoke_container_invalid",
    )
    container_id = container.get("Id")
    config = container.get("Config")
    if (
        not isinstance(container_id, str)
        or not _CONTAINER_ID_RE.fullmatch(container_id)
        or (expected_container_id is not None and container_id != expected_container_id)
        or container.get("Name") != f"/{expected_name}"
        or container.get("Image") != expected_image_id
        or not isinstance(config, Mapping)
        or config.get("Image") != expected_image_id
        or not isinstance(config.get("Labels"), Mapping)
        or config["Labels"].get(BUILT_IMAGE_SMOKE_LABEL) != expected_nonce
    ):
        raise BuildError("built_image_smoke_container_invalid")
    return container


def _smoke_container_evidence(
    raw: bytes,
    *,
    expected_name: str,
    expected_container_id: str,
    expected_image_id: str,
    expected_nonce: str,
) -> Mapping[str, Any]:
    container = _smoke_container_identity(
        raw,
        expected_name=expected_name,
        expected_container_id=expected_container_id,
        expected_image_id=expected_image_id,
        expected_nonce=expected_nonce,
    )
    config = container["Config"]
    host = container.get("HostConfig")
    state = container.get("State")
    mounts = container.get("Mounts")
    environment = config.get("Env")
    if (
        config.get("User") != "10001:10001"
        or config.get("Entrypoint") != list(BUILT_IMAGE_SMOKE_ENTRYPOINT)
        or config.get("Cmd") != list(BUILT_IMAGE_SMOKE_COMMAND)
        or not isinstance(environment, list)
        or any(not isinstance(item, str) for item in environment)
        or any(environment.count(item) != 1 for item in BUILT_IMAGE_SMOKE_ENVIRONMENT)
        or any(
            sum(
                observed.partition("=")[0] == expected.partition("=")[0]
                for observed in environment
            )
            != 1
            for expected in BUILT_IMAGE_SMOKE_ENVIRONMENT
        )
        or any(
            item.startswith(
                (
                    "DATABASE_URL=",
                    "EA_API_TOKEN=",
                    "EA_SIGNING_SECRET=",
                    "HTTP_PROXY=",
                    "HTTPS_PROXY=",
                    "ALL_PROXY=",
                    "NO_PROXY=",
                )
            )
            for item in environment
        )
        or not isinstance(host, Mapping)
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("AutoRemove") is not False
        or host.get("CapAdd") not in (None, [])
        or host.get("CapDrop") != ["ALL"]
        or host.get("Binds") not in (None, [])
        or host.get("Devices") not in (None, [])
        or host.get("PidsLimit") != 128
        or host.get("Memory") != 536_870_912
        or host.get("MemorySwap") != 536_870_912
        or host.get("NanoCpus") != 1_000_000_000
        or not isinstance(host.get("SecurityOpt"), list)
        or "no-new-privileges=true" not in host["SecurityOpt"]
        or not isinstance(host.get("Tmpfs"), Mapping)
        or set(host["Tmpfs"]) != {"/tmp"}
        or not isinstance(mounts, list)
        or any(
            not isinstance(mount, Mapping)
            or mount.get("Type") != "tmpfs"
            or mount.get("Destination") != "/tmp"
            for mount in mounts
        )
        or not isinstance(state, Mapping)
        or state.get("Running") is not False
        or state.get("OOMKilled") is not False
        or state.get("ExitCode") != 0
        or state.get("Error") not in (None, "")
    ):
        raise BuildError("built_image_smoke_policy_invalid")
    return {
        "contract_name": BUILT_IMAGE_SMOKE_CONTRACT,
        "image_reference": expected_image_id,
        "container_id_sha256": _sha256(expected_container_id.encode("ascii")),
        "script_sha256": _sha256(BUILT_IMAGE_SMOKE_SCRIPT.encode("utf-8")),
        "entrypoint_enforced": True,
        "application_imported": True,
        "asgi_lifespan_completed": True,
        "health_route_registered": True,
        "runtime_mode": "test",
        "storage_backend": "memory",
        "network_mode": "none",
        "root_filesystem": "read_only",
        "capabilities": "none",
        "no_new_privileges": True,
        "pids_limit": 128,
        "memory_limit_bytes": 536_870_912,
        "memory_swap_limit_bytes": 536_870_912,
        "nano_cpus": 1_000_000_000,
        "external_authority": False,
        "production_runtime_proof": False,
    }


def _cleanup_smoke_container(
    evidence: _Evidence,
    *,
    name: str,
    expected_image_id: str,
    expected_nonce: str,
) -> None:
    observed = evidence.docker(
        "container",
        "inspect",
        name,
        error_code="built_image_smoke_cleanup_failed",
        allowed_returncodes=frozenset({0, 1}),
    )
    if observed.returncode == 1:
        return
    container = _smoke_container_identity(
        observed.stdout,
        expected_name=name,
        expected_container_id=None,
        expected_image_id=expected_image_id,
        expected_nonce=expected_nonce,
    )
    container_id = str(container["Id"])
    evidence.docker(
        "container",
        "rm",
        "--force",
        container_id,
        error_code="built_image_smoke_cleanup_failed",
    )
    absent = evidence.docker(
        "container",
        "inspect",
        container_id,
        error_code="built_image_smoke_cleanup_failed",
        allowed_returncodes=frozenset({0, 1}),
    )
    if absent.returncode != 1:
        raise BuildError("built_image_smoke_cleanup_failed")


def _run_built_image_smoke(
    config: BuildConfig,
    evidence: _Evidence,
    *,
    image_id: str,
) -> Mapping[str, Any]:
    nonce = secrets.token_hex(16)
    name = f"propertyquarry-smoke-{nonce}"
    existing = evidence.docker(
        "container",
        "inspect",
        name,
        error_code="built_image_smoke_preflight_failed",
        allowed_returncodes=frozenset({0, 1}),
    )
    if existing.returncode == 0:
        raise BuildError("built_image_smoke_name_collision")

    create_attempted = False
    container_id: str | None = None
    try:
        create_arguments: list[str] = [
            "container",
            "create",
            "--name",
            name,
            "--label",
            f"{BUILT_IMAGE_SMOKE_LABEL}={nonce}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "128",
            "--memory",
            "536870912",
            "--memory-swap",
            "536870912",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
        ]
        for item in BUILT_IMAGE_SMOKE_ENVIRONMENT:
            create_arguments.extend(("--env", item))
        create_arguments.extend((image_id, *BUILT_IMAGE_SMOKE_COMMAND))

        def capture_container_id(result: CommandResult) -> None:
            nonlocal container_id
            container_id = _one_line_container_id(
                result.stdout,
                "built_image_smoke_container_id_invalid",
            )

        create_attempted = True
        evidence.docker(
            *create_arguments,
            error_code="built_image_smoke_create_failed",
            success_observer=capture_container_id,
        )
        assert container_id is not None
        started = evidence.docker(
            "container",
            "start",
            "--attach",
            container_id,
            error_code="built_image_smoke_failed",
            timeout_s=config.docker_smoke_timeout_s,
        )
        if started.stdout != BUILT_IMAGE_SMOKE_MARKER:
            raise BuildError("built_image_smoke_output_invalid")
        observed = evidence.docker(
            "container",
            "inspect",
            container_id,
            error_code="built_image_smoke_inspect_failed",
        )
        return _smoke_container_evidence(
            observed.stdout,
            expected_name=name,
            expected_container_id=container_id,
            expected_image_id=image_id,
            expected_nonce=nonce,
        )
    finally:
        if create_attempted:
            _cleanup_smoke_container(
                evidence,
                name=name,
                expected_image_id=image_id,
                expected_nonce=nonce,
            )


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
    now: Callable[[], dt.datetime] | None = None,
) -> BuildResult:
    """Perform one explicitly authorized local build and publish its receipt."""

    _validate_config(config)
    _require_receipt_absent(config.receipt_path)
    executor = executor or BoundedSubprocessExecutor()
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    git_binary_before = _trusted_binary_identity(TRUSTED_GIT_BIN)
    docker_binary_before = _trusted_binary_identity(TRUSTED_DOCKER_BIN)
    socket_before = _local_docker_socket_identity()
    docker_config = _create_docker_config(config.receipt_root)
    receipt: Mapping[str, Any] | None = None
    built_image_id: str | None = None
    build_started = False
    operation_succeeded = False
    evidence = _Evidence(config, executor, docker_config)
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
            DOCKERFILE_PATH,
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
        built_image_smoke = _run_built_image_smoke(
            config,
            evidence,
            image_id=image_id,
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
        bases_second: list[Mapping[str, Any]] = []
        for reference in base_references:
            inspected = _docker_inspect_object(
                evidence.docker(
                    "image", "inspect", reference, error_code="base_image_reinspect_failed"
                ).stdout,
                "base_image_inspect_invalid",
            )
            bases_second.append(_base_image_evidence(reference, inspected))
        if bases_second != bases_first:
            raise BuildError("base_image_changed_during_build")
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

        generated = now()
        if generated.tzinfo is None or generated.utcoffset() != dt.timedelta(0):
            raise BuildError("invalid_clock")
        generated_at = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if not _RFC3339_UTC_SECONDS_RE.fullmatch(generated_at):
            raise BuildError("invalid_clock")
        receipt = {
            "schema": BUILD_RECEIPT_SCHEMA,
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
            "dockerfile": {"path": DOCKERFILE_PATH, "sha256": _sha256(git_first.dockerfile)},
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
            "image_reference": image_id,
            "image_config_id": image_id,
            "oci_manifest_digest": oci_digest,
            "local_oci_manifest": local_oci,
            "built_image_smoke": built_image_smoke,
            "authority": {
                "local_only": True,
                "performs_local_docker_build": True,
                "authoritative_for_release_effects": False,
                "public_launch_authority": False,
                "production_ready": False,
            },
        }
        operation_succeeded = True
    except BaseException:
        cleanup_with_fresh_config = False
        try:
            if build_started and built_image_id is not None:
                _cleanup_built_tag(
                    evidence,
                    tag=config.local_image_tag,
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
            )
        raise
    finally:
        try:
            _remove_docker_config(docker_config)
        except BaseException:
            if operation_succeeded and built_image_id is not None:
                _cleanup_built_tag_with_fresh_config(
                    config,
                    executor,
                    expected_image_id=built_image_id,
                )
            _discard_owned_docker_config(docker_config)
            if operation_succeeded:
                raise

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
        )
        raise
    return BuildResult(receipt=receipt, receipt_sha256=_sha256(raw))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one authenticated PropertyQuarry candidate in local Docker only."
    )
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--source-candidate-sha", required=True)
    parser.add_argument("--metadata-envelope-sha", required=True)
    parser.add_argument("--local-image-tag", required=True)
    parser.add_argument("--output-receipt", required=True, type=Path)
    parser.add_argument("--execute-local-build", required=True, action="store_true")
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
            )
        )
    except BuildError as error:
        print(f"error:{error.code}", file=sys.stderr)
        return 1
    summary = {
        "schema": BUILD_RECEIPT_SCHEMA,
        "receipt_sha256": result.receipt_sha256,
        "image_config_id": result.receipt["image_config_id"],
        "oci_manifest_digest": result.receipt["oci_manifest_digest"],
        "local_only": True,
    }
    sys.stdout.buffer.write(_canonical_json_bytes(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
