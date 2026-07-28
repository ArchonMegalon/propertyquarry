#!/usr/bin/env python3
"""Opt-in persistent no-effects Docker runtime lifecycle gate.

Importing this module and running its tests never contacts Docker.  The CLI is
the explicit mutation boundary after the daemonless OCI and one-shot gates.
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from typing import Callable, Final, Mapping, Sequence

import yaml

try:  # Support module and direct-script execution.
    from scripts import propertyquarry_release_local_container_gate as container_gate
    from scripts import propertyquarry_release_oci_materializer as oci
except ModuleNotFoundError:  # pragma: no cover - direct CLI coverage
    import propertyquarry_release_local_container_gate as container_gate
    import propertyquarry_release_oci_materializer as oci


RECEIPT_SCHEMA: Final = "propertyquarry.release-control.local-runtime-gate.v2"
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE_PATH: Final = REPOSITORY_ROOT / "compose.propertyquarry-release-runtime-v2.yml"
PINNED_COMPOSE_SHA256: Final = (
    "sha256:b7acdc195b40e14c305ddd6a12c0ee6e8739a9910b5fe8dc4d3b76ab1b7dd382"
)
RUNTIME_HEALTH_SCHEMA: Final = (
    "propertyquarry.release-control.local-runtime-health.v2"
)
RUNTIME_LABEL: Final = "propertyquarry.release-control.runtime-gate"
RUNTIME_SOCKET_DIRECTORY: Final = "/run/propertyquarry-release-control-v2"
RUNTIME_STATE_DIRECTORY: Final = "/var/lib/propertyquarry-release-control-v2"
EXTERNAL_ANCHOR_TARGET: Final = (
    "/run/secrets/propertyquarry-package-authority-v2.pem"
)
WATCHDOG_PATH: Final = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-watchdog-v2"
)
SUPERVISOR_PATH: Final = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-supervisor-v2"
)
PROJECT_RE: Final = re.compile(r"pq-release-runtime-[0-9a-f]{16}\Z")
DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME_USER_RE: Final = re.compile(
    r"(?:0|[1-9][0-9]{0,9}):(?:0|[1-9][0-9]{0,9})\Z"
)
MAX_ANCHOR_BYTES: Final = 65_536
MAX_POLL_ATTEMPTS: Final = 20
POLL_INTERVAL_SECONDS: Final = 0.5
RESTART_POLICY_ARM_SECONDS: Final = 11.0
VOLATILE_ANCHOR_PREFIXES: Final = (
    "/dev",
    "/proc",
    "/run",
    "/sys",
    "/tmp",
    "/var/tmp",
)
VOLATILE_FILESYSTEM_TYPES: Final = frozenset(
    {
        0x01021994,  # TMPFS_MAGIC
        0x858458F6,  # RAMFS_MAGIC
        0x00009FA0,  # PROC_SUPER_MAGIC
        0x62656572,  # SYSFS_MAGIC
        0x00001CD1,  # DEVPTS_SUPER_MAGIC
        0x0027E0EB,  # CGROUP_SUPER_MAGIC
        0x63677270,  # CGROUP2_SUPER_MAGIC
        0xCAFE4A11,  # BPF_FS_MAGIC
        0x64626720,  # DEBUGFS_MAGIC
        0x74726163,  # TRACEFS_MAGIC
        0x73636673,  # SECURITYFS_MAGIC
        0x19800202,  # MQUEUE_MAGIC
        0x6E736673,  # NSFS_MAGIC
        0x62656570,  # CONFIGFS_MAGIC
    }
)

_IMAGE_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_IMAGE:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_IMAGE to the verified local image ID "
    "sha256:<64 lowercase hex>}"
)
_AUTHORITY_USER_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_AUTHORITY_USER:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_AUTHORITY_USER to the numeric authority "
    "uid:gid from the materialization receipt}"
)
_CALLER_USER_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_CALLER_USER:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_CALLER_USER to the numeric caller uid:gid "
    "from the materialization receipt}"
)
_ANCHOR_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_ANCHOR:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_ANCHOR to the absolute root-owned public anchor}"
)
_GATE_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_GATE:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_GATE to the validated gate project}"
)
_SUPERVISOR_NAME_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_SUPERVISOR_NAME:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_SUPERVISOR_NAME to the validated "
    "gate-specific name}"
)
_WATCHDOG_NAME_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_WATCHDOG_NAME:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_WATCHDOG_NAME to the validated "
    "gate-specific name}"
)
_GID_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_RUNTIME_GID:?Set "
    "PROPERTYQUARRY_RELEASE_RUNTIME_GID to the validated service gid}"
)
_CONTAINER_STATE_FORMAT: Final = (
    "{{.Id}}\t{{.Name}}\t{{.State.Status}}\t"
    "{{if index .State \"Health\"}}"
    "{{index (index .State \"Health\") \"Status\"}}"
    "{{else}}none{{end}}\t"
    "{{.RestartCount}}"
)
_RUNTIME_STATE_FORMAT: Final = (
    "{{.Name}}\t{{.State.Status}}\t"
    "{{if index .State \"Health\"}}"
    "{{index (index .State \"Health\") \"Status\"}}"
    "{{else}}none{{end}}\t"
    "{{.RestartCount}}"
)


class RuntimeGateError(ValueError):
    """A bounded, path-free persistent runtime gate failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeGateResult:
    receipt: str
    receipt_sha256: str
    image_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class DurableAnchor:
    path: str
    parent_fd: int
    file_fd: int
    name: str
    identity: tuple[int, ...]
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeState:
    supervisor_restart_count: int
    watchdog_restart_count: int


CommandResult = container_gate.CommandResult
CommandRunner = container_gate.CommandRunner


class _LinuxStatfs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


def _fail(code: str) -> None:
    raise RuntimeGateError(code)


def _filesystem_type(descriptor: int) -> int:
    result = _LinuxStatfs()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fstatfs = libc.fstatfs
        fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatfs)]
        fstatfs.restype = ctypes.c_int
        return_code = fstatfs(descriptor, ctypes.byref(result))
    except (AttributeError, OSError, TypeError):
        _fail("runtime-anchor-filesystem-inspection-failed")
    if return_code != 0:
        _fail("runtime-anchor-filesystem-inspection-failed")
    bits = ctypes.sizeof(ctypes.c_long) * 8
    return int(result.f_type) & ((1 << bits) - 1)


def _require_durable_filesystem(descriptor: int) -> None:
    if _filesystem_type(descriptor) in VOLATILE_FILESYSTEM_TYPES:
        _fail("runtime-anchor-filesystem-volatile")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail("canonical-json-invalid")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _metadata_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_descriptor_exact(descriptor: int, size: int, code: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = os.read(descriptor, min(65_536, remaining))
        except OSError:
            _fail(code)
        if not chunk:
            _fail(code)
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        if os.read(descriptor, 1):
            _fail(code)
    except OSError:
        _fail(code)
    return b"".join(chunks)


def _anchor_ancestor_metadata_valid(value: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and value.st_uid == 0
        and value.st_gid == 0
        and stat.S_IMODE(value.st_mode) & 0o022 == 0
    )


def _anchor_file_metadata_valid(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and value.st_nlink == 1
        and value.st_uid == 0
        and value.st_gid == 0
        and stat.S_IMODE(value.st_mode) == 0o444
        and 0 < value.st_size <= MAX_ANCHOR_BYTES
    )


def _exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _exact_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _exact_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _expected_compose_document() -> dict[str, object]:
    anchor_mount = {
        "type": "bind",
        "source": _ANCHOR_INTERPOLATION,
        "target": EXTERNAL_ANCHOR_TARGET,
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    socket_mount = {
        "type": "volume",
        "source": "runtime-socket",
        "target": RUNTIME_SOCKET_DIRECTORY,
        "volume": {"nocopy": True},
    }
    state_mount = {
        "type": "volume",
        "source": "authority-replay-state-v1",
        "target": RUNTIME_STATE_DIRECTORY,
    }
    base: dict[str, object] = {
        "image": _IMAGE_INTERPOLATION,
        "pull_policy": "never",
        "working_dir": "/",
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 32,
        "mem_limit": "256m",
        "mem_reservation": "16m",
        "memswap_limit": "256m",
        "environment": [],
        "stdin_open": False,
        "tty": False,
        "init": False,
        "restart": "on-failure:3",
        "stop_signal": "SIGTERM",
        "stop_grace_period": "10s",
        "labels": {RUNTIME_LABEL: _GATE_INTERPOLATION},
        "healthcheck": {
            "test": [
                "CMD",
                WATCHDOG_PATH,
                "--installed-local-authority",
                "--health-json",
            ],
            "interval": "2s",
            "timeout": "2s",
            "retries": 5,
            "start_period": "2s",
        },
    }
    return {
        "name": "propertyquarry-release-runtime-v2",
        "x-propertyquarry-release-runtime": base,
        "services": {
            "release-supervisor": {
                **base,
                "container_name": _SUPERVISOR_NAME_INTERPOLATION,
                "user": _AUTHORITY_USER_INTERPOLATION,
                "entrypoint": [SUPERVISOR_PATH],
                "command": ["--installed-local-authority", "--docker-broker"],
                "volumes": [anchor_mount, socket_mount, state_mount],
            },
            "release-watchdog": {
                **base,
                "container_name": _WATCHDOG_NAME_INTERPOLATION,
                "user": _CALLER_USER_INTERPOLATION,
                "entrypoint": [WATCHDOG_PATH],
                "command": ["--installed-local-authority", "--docker-watchdog"],
                "depends_on": {
                    "release-supervisor": {
                        "condition": "service_healthy",
                        "restart": True,
                    }
                },
                "volumes": [anchor_mount, socket_mount],
            },
        },
        "volumes": {
            "runtime-socket": {
                "driver": "local",
                "driver_opts": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": (
                        f"uid={oci.AUTHORITY_UID},gid={_GID_INTERPOLATION},"
                        "mode=0750,nosuid,nodev,noexec,size=1048576"
                    ),
                },
            },
            "authority-replay-state-v1": {
                "driver": "local",
            },
        },
    }


def _validate_runtime_compose(raw: bytes) -> None:
    if type(raw) is not bytes or _digest(raw) != PINNED_COMPOSE_SHA256:
        _fail("runtime-compose-pinned-digest-mismatch")
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        _fail("runtime-compose-contract-invalid")
    if not _exact_equal(document, _expected_compose_document()):
        _fail("runtime-compose-contract-invalid")


def _read_runtime_compose() -> bytes:
    try:
        return container_gate._read_regular(
            COMPOSE_PATH,
            maximum=container_gate.MAX_JSON_BYTES,
            mode=0o644,
        )
    except container_gate.ContainerGateError as error:
        raise RuntimeGateError(error.code) from None


def _open_durable_anchor(path_text: str) -> DurableAnchor:
    try:
        path = Path(oci.package_payload._path_text(path_text, "runtime-anchor-invalid"))
    except oci.package_payload.PayloadError as error:
        raise RuntimeGateError(error.code) from None
    if (
        not path.is_absolute()
        or str(path) == "/"
        or any(
            str(path) == prefix or str(path).startswith(prefix + "/")
            for prefix in VOLATILE_ANCHOR_PREFIXES
        )
    ):
        _fail("runtime-anchor-not-durable")
    components = path.parts[1:]
    if not components or any(component in {"", ".", ".."} for component in components):
        _fail("runtime-anchor-path-invalid")
    parent_fd = os.open(
        "/",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    file_fd = -1
    try:
        _require_durable_filesystem(parent_fd)
        if not _anchor_ancestor_metadata_valid(os.fstat(parent_fd)):
            _fail("runtime-anchor-ancestor-invalid")
        for component in components[:-1]:
            metadata = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if not _anchor_ancestor_metadata_valid(metadata):
                _fail("runtime-anchor-ancestor-invalid")
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(next_fd)
            if _metadata_identity(opened) != _metadata_identity(metadata):
                os.close(next_fd)
                _fail("runtime-anchor-ancestor-replaced")
            try:
                _require_durable_filesystem(next_fd)
            except RuntimeGateError:
                os.close(next_fd)
                raise
            os.close(parent_fd)
            parent_fd = next_fd
        name = components[-1]
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        file_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(file_fd)
        identity = _metadata_identity(before)
        if (
            not _anchor_file_metadata_valid(before)
            or _metadata_identity(opened) != identity
        ):
            _fail("runtime-anchor-metadata-invalid")
        _require_durable_filesystem(file_fd)
        data = _read_descriptor_exact(
            file_fd, before.st_size, "runtime-anchor-read-failed"
        )
        if _metadata_identity(os.fstat(file_fd)) != identity:
            _fail("runtime-anchor-concurrent-mutation")
        os.lseek(file_fd, 0, os.SEEK_SET)
        return DurableAnchor(str(path), parent_fd, file_fd, name, identity, data)
    except RuntimeGateError:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)
        raise
    except OSError:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)
        _fail("runtime-anchor-open-failed")


def _revalidate_anchor(anchor: DurableAnchor) -> None:
    try:
        descriptor_metadata = os.fstat(anchor.file_fd)
        name_metadata = os.stat(
            anchor.name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
        path_metadata = os.stat(anchor.path, follow_symlinks=False)
        if (
            _metadata_identity(descriptor_metadata) != anchor.identity
            or _metadata_identity(name_metadata) != anchor.identity
            or _metadata_identity(path_metadata) != anchor.identity
        ):
            _fail("runtime-anchor-concurrent-mutation")
        os.lseek(anchor.file_fd, 0, os.SEEK_SET)
        data = _read_descriptor_exact(
            anchor.file_fd,
            len(anchor.data),
            "runtime-anchor-concurrent-mutation",
        )
        if (
            data != anchor.data
            or _metadata_identity(os.fstat(anchor.file_fd)) != anchor.identity
        ):
            _fail("runtime-anchor-concurrent-mutation")
        os.lseek(anchor.file_fd, 0, os.SEEK_SET)
    except RuntimeGateError:
        raise
    except OSError:
        _fail("runtime-anchor-concurrent-mutation")


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> CommandResult:
    try:
        return container_gate._run(runner, command, environment)
    except container_gate.ContainerGateError as error:
        raise RuntimeGateError(error.code) from None


def _sealed_memfd(name: str, data: bytes) -> int:
    try:
        return container_gate._sealed_memfd(name, data)
    except container_gate.ContainerGateError as error:
        raise RuntimeGateError(error.code) from None


def _revalidate_sealed_memfd(descriptor: int, expected: bytes) -> None:
    try:
        container_gate._revalidate_sealed_memfd(descriptor, expected)
    except container_gate.ContainerGateError as error:
        raise RuntimeGateError(error.code) from None


def _parse_container_inventory(
    identifiers: bytes,
    states: bytes,
) -> bytes:
    try:
        id_text = identifiers.decode("ascii")
        state_text = states.decode("ascii")
    except UnicodeDecodeError:
        _fail("container-inventory-invalid")
    ids = [] if not id_text else id_text.splitlines()
    if (
        any(CONTAINER_ID_RE.fullmatch(value) is None for value in ids)
        or len(ids) != len(set(ids))
    ):
        _fail("container-inventory-invalid")
    records: list[dict[str, object]] = []
    lines = [] if not state_text else state_text.splitlines()
    if len(lines) != len(ids):
        _fail("container-health-invalid")
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 5:
            _fail("container-health-invalid")
        identifier, name, status_text, health, restart_text = fields
        if (
            identifier not in ids
            or not name.startswith("/")
            or "\t" in name
            or status_text
            not in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }
            or health not in {"none", "starting", "healthy", "unhealthy"}
            or not restart_text.isascii()
            or not restart_text.isdigit()
        ):
            _fail("container-health-invalid")
        records.append(
            {
                "health": health,
                "id": identifier,
                "name": name[1:],
                "restart_count": int(restart_text),
                "status": status_text,
            }
        )
    if {record["id"] for record in records} != set(ids):
        _fail("container-health-invalid")
    return _canonical(sorted(records, key=lambda item: str(item["id"])))


def _container_inventory(
    runner: CommandRunner,
    base: Sequence[str],
    environment: Mapping[str, str],
    run_pinned: Callable[[Sequence[str]], CommandResult],
) -> bytes:
    del runner, environment
    identifiers = run_pinned(
        list(base) + ["container", "ls", "--all", "--quiet", "--no-trunc"]
    )
    if identifiers.returncode != 0 or identifiers.stderr:
        _fail("container-inventory-failed")
    try:
        values = [] if not identifiers.stdout else identifiers.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _fail("container-inventory-invalid")
    if not values:
        return _parse_container_inventory(identifiers.stdout, b"")
    states = run_pinned(
        list(base)
        + ["container", "inspect", "--format", _CONTAINER_STATE_FORMAT, *sorted(values)]
    )
    if states.returncode != 0 or states.stderr:
        _fail("container-health-failed")
    return _parse_container_inventory(identifiers.stdout, states.stdout)


def _parse_runtime_state(
    output: bytes,
    supervisor_name: str,
    watchdog_name: str,
) -> RuntimeState | None:
    try:
        text = output.decode("ascii")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if len(lines) != 2:
        return None
    values: dict[str, tuple[str, str, int]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 4 or not fields[3].isdigit():
            return None
        name, status_text, health, restart_text = fields
        values[name.removeprefix("/")] = (status_text, health, int(restart_text))
    if set(values) != {supervisor_name, watchdog_name}:
        return None
    if any(value[:2] != ("running", "healthy") for value in values.values()):
        return None
    return RuntimeState(values[supervisor_name][2], values[watchdog_name][2])


def _wait_runtime_ready(
    run_pinned: Callable[[Sequence[str]], CommandResult],
    base: Sequence[str],
    supervisor_name: str,
    watchdog_name: str,
    *,
    minimum_supervisor_restarts: int,
    minimum_watchdog_restarts: int,
) -> RuntimeState:
    command = list(base) + [
        "container",
        "inspect",
        "--format",
        _RUNTIME_STATE_FORMAT,
        supervisor_name,
        watchdog_name,
    ]
    for attempt in range(MAX_POLL_ATTEMPTS):
        result = run_pinned(command)
        if result.returncode == 0 and not result.stderr:
            state = _parse_runtime_state(
                result.stdout, supervisor_name, watchdog_name
            )
            if (
                state is not None
                and state.supervisor_restart_count >= minimum_supervisor_restarts
                and state.watchdog_restart_count >= minimum_watchdog_restarts
            ):
                return state
        if attempt + 1 < MAX_POLL_ATTEMPTS:
            time.sleep(POLL_INTERVAL_SECONDS)
    _fail("runtime-readiness-timeout")


def _expected_health_document(
    *,
    image_receipt: Mapping[str, object],
    authority_key_id: str,
    source_manifest_digest: str,
) -> dict[str, object]:
    return {
        "authentication_digest": image_receipt["authentication_sha256"],
        "authoritative_for_package_authentication": True,
        "authoritative_for_release_effects": False,
        "authority_key_id": authority_key_id,
        "component": "propertyquarry-release-watchdog-v2",
        "installed_local_authority_verified": True,
        "payload_tree_digest": image_receipt["authenticated_payload_tree_digest"],
        "performs_release_effects": False,
        "production_ready": False,
        "ready": True,
        "schema": RUNTIME_HEALTH_SCHEMA,
        "socket_accepting": True,
        "source_manifest_digest": source_manifest_digest,
        "version": 2,
    }


def _validate_health_output(output: bytes, expected: Mapping[str, object]) -> str:
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        _fail("runtime-health-output-invalid")
    try:
        document = json.loads(output[:-1].decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("runtime-health-output-invalid")
    if _canonical(document) != output[:-1] or not _exact_equal(document, expected):
        _fail("runtime-health-output-invalid")
    return _digest(output)


def _write_receipt(path: Path, data: bytes) -> None:
    try:
        container_gate._write_receipt(path, data)
    except container_gate.ContainerGateError as error:
        raise RuntimeGateError(error.code) from None


def run_gate(
    *,
    layout: str,
    wrapper: str,
    external_anchor: str,
    native_build_receipt: str,
    output_receipt: str,
    docker_binary: str = "/usr/bin/docker",
    docker_host: str = "unix:///var/run/docker.sock",
    command_runner: CommandRunner = container_gate._default_runner,
) -> RuntimeGateResult:
    """Start, prove, drift/recover, remove, and receipt the runtime plane."""

    if (
        not Path(docker_binary).is_absolute()
        or container_gate._DOCKER_HOST_RE.fullmatch(docker_host) is None
    ):
        _fail("docker-boundary-invalid")
    compose_bytes = _read_runtime_compose()
    _validate_runtime_compose(compose_bytes)
    anchor = _open_durable_anchor(external_anchor)
    layout_fd = -1
    compose_fd = -1
    docker_config: tempfile.TemporaryDirectory[str] | None = None
    up_attempted = False
    cleanup_complete = False
    try:
        try:
            layout_path = oci.package_payload._path_text(
                layout, "oci-output-path-invalid"
            )
            layout_fd, _layout_identity = (
                oci.package_payload._open_controlled_directory(
                    layout_path, writable=False
                )
            )
        except oci.package_payload.PayloadError:
            _fail("oci-layout-open-failed")
        image_receipt = oci.verify_pinned(
            layout_fd,
            wrapper=wrapper,
            external_anchor=external_anchor,
        )
        initial_tree = oci._snapshot_oci_tree(layout_fd)
        try:
            native_receipt_bytes, source_manifest_digest, _toolchain = (
                container_gate._parse_native_build_receipt(
                    Path(native_build_receipt)
                )
            )
        except container_gate.ContainerGateError as error:
            raise RuntimeGateError(error.code) from None
        if (
            _digest(native_receipt_bytes)
            != image_receipt["native_build_receipt_sha256"]
        ):
            _fail("native-build-receipt-binding-mismatch")
        try:
            _source, _auth, _signature, auth_document, _payload = (
                oci._snapshot_authenticated_wrapper(
                    wrapper=wrapper,
                    external_anchor=external_anchor,
                )
            )
            authority_key_id = auth_document["signature_profile"]["key_id"]
        except (KeyError, TypeError):
            _fail("runtime-authority-key-id-invalid")
        if (
            not isinstance(authority_key_id, str)
            or DIGEST_RE.fullmatch(authority_key_id) is None
        ):
            _fail("runtime-authority-key-id-invalid")
        image_id = image_receipt["image_id"]
        runtime_user = image_receipt["runtime_user"]
        authority_user = image_receipt["authority_user"]
        caller_user = image_receipt["caller_user"]
        if (
            not isinstance(image_id, str)
            or DIGEST_RE.fullmatch(image_id) is None
            or not isinstance(runtime_user, str)
            or RUNTIME_USER_RE.fullmatch(runtime_user) is None
            or not isinstance(authority_user, str)
            or RUNTIME_USER_RE.fullmatch(authority_user) is None
            or not isinstance(caller_user, str)
            or RUNTIME_USER_RE.fullmatch(caller_user) is None
        ):
            _fail("runtime-interpolation-invalid")
        authority_uid, runtime_gid = authority_user.split(":", 1)
        caller_uid, caller_gid = caller_user.split(":", 1)
        if (
            runtime_user != authority_user
            or authority_uid != str(oci.AUTHORITY_UID)
            or caller_uid != str(oci.CALLER_UID)
            or caller_gid != runtime_gid
            or int(runtime_gid) > 0xFFFFFFFF
        ):
            _fail("runtime-interpolation-invalid")
        project = "pq-release-runtime-" + secrets.token_hex(8)
        if PROJECT_RE.fullmatch(project) is None:  # pragma: no cover - invariant
            _fail("runtime-project-invalid")
        supervisor_name = project + "-supervisor"
        watchdog_name = project + "-watchdog"
        compose_fd = _sealed_memfd(
            "propertyquarry-release-runtime-compose", compose_bytes
        )
        compose_path = f"/proc/{os.getpid()}/fd/{compose_fd}"
        docker_config = tempfile.TemporaryDirectory(prefix="pq-runtime-docker-")
        environment = {
            "DOCKER_CONFIG": docker_config.name,
            "HOME": docker_config.name,
            "PATH": "/usr/bin:/bin",
            "PROPERTYQUARRY_RELEASE_RUNTIME_ANCHOR": anchor.path,
            "PROPERTYQUARRY_RELEASE_RUNTIME_AUTHORITY_USER": authority_user,
            "PROPERTYQUARRY_RELEASE_RUNTIME_CALLER_USER": caller_user,
            "PROPERTYQUARRY_RELEASE_RUNTIME_GATE": project,
            "PROPERTYQUARRY_RELEASE_RUNTIME_GID": runtime_gid,
            "PROPERTYQUARRY_RELEASE_RUNTIME_IMAGE": image_id,
            "PROPERTYQUARRY_RELEASE_RUNTIME_SUPERVISOR_NAME": supervisor_name,
            "PROPERTYQUARRY_RELEASE_RUNTIME_WATCHDOG_NAME": watchdog_name,
        }
        base = [docker_binary, "--host", docker_host]
        compose = base + [
            "compose",
            "--progress",
            "quiet",
            "--project-name",
            project,
            "--file",
            compose_path,
        ]

        def run_pinned(command: Sequence[str]) -> CommandResult:
            _revalidate_sealed_memfd(compose_fd, compose_bytes)
            _revalidate_anchor(anchor)
            result = _run(command_runner, command, environment)
            _revalidate_anchor(anchor)
            _revalidate_sealed_memfd(compose_fd, compose_bytes)
            return result

        before_inventory = _container_inventory(
            command_runner, base, environment, run_pinned
        )
        inspected = run_pinned(
            base + ["image", "inspect", "--format", "{{.Id}}", image_id]
        )
        if (
            inspected.returncode != 0
            or inspected.stdout != image_id.encode("ascii") + b"\n"
            or inspected.stderr
        ):
            _fail("runtime-image-id-mismatch")
        up_attempted = True
        started = run_pinned(
            compose
            + [
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "60",
                "release-supervisor",
                "release-watchdog",
            ]
        )
        if started.returncode != 0:
            _fail("runtime-start-failed")
        initial_state = _wait_runtime_ready(
            run_pinned,
            base,
            supervisor_name,
            watchdog_name,
            minimum_supervisor_restarts=0,
            minimum_watchdog_restarts=0,
        )
        if (
            initial_state.supervisor_restart_count != 0
            or initial_state.watchdog_restart_count != 0
        ):
            _fail("runtime-initial-restart-count-invalid")
        expected_health = _expected_health_document(
            image_receipt=image_receipt,
            authority_key_id=authority_key_id,
            source_manifest_digest=source_manifest_digest,
        )
        watchdog_logs = run_pinned(
            base + ["container", "logs", watchdog_name]
        )
        if watchdog_logs.returncode != 0 or watchdog_logs.stderr:
            _fail("runtime-watchdog-log-invalid")
        initial_health_sha256 = _validate_health_output(
            watchdog_logs.stdout, expected_health
        )
        supervisor_logs = run_pinned(
            base + ["container", "logs", supervisor_name]
        )
        if (
            supervisor_logs.returncode != 0
            or supervisor_logs.stdout
            or supervisor_logs.stderr
        ):
            _fail("runtime-supervisor-log-invalid")
        health = run_pinned(
            base
            + [
                "container",
                "exec",
                watchdog_name,
                WATCHDOG_PATH,
                "--installed-local-authority",
                "--health-json",
            ]
        )
        if health.returncode != 0 or health.stderr:
            _fail("runtime-health-command-failed")
        health_sha256 = _validate_health_output(health.stdout, expected_health)
        request_smoke = run_pinned(
            compose
            + [
                "exec",
                "-T",
                "release-watchdog",
                SUPERVISOR_PATH,
                "--installed-local-authority",
                "--request-smoke",
            ]
        )
        if (
            request_smoke.returncode != 0
            or request_smoke.stdout
            or request_smoke.stderr
        ):
            _fail("runtime-framed-request-smoke-failed")
        # Docker only activates a restart policy after a container has stayed
        # up successfully. Hold a measured healthy interval, then prove both
        # counts are still zero before applying the reversible SIGKILL.
        time.sleep(RESTART_POLICY_ARM_SECONDS)
        armed_state = _wait_runtime_ready(
            run_pinned,
            base,
            supervisor_name,
            watchdog_name,
            minimum_supervisor_restarts=0,
            minimum_watchdog_restarts=0,
        )
        if (
            armed_state.supervisor_restart_count != 0
            or armed_state.watchdog_restart_count != 0
        ):
            _fail("runtime-policy-arm-restart-count-invalid")
        crashed = run_pinned(
            compose
            + [
                "exec",
                "-T",
                "release-supervisor",
                SUPERVISOR_PATH,
                "--installed-local-authority",
                "--docker-restart-stimulus",
            ]
        )
        if (
            crashed.returncode != 137
            or crashed.stdout
            or crashed.stderr
        ):
            _fail("runtime-drift-stimulus-failed")
        recovered_state = _wait_runtime_ready(
            run_pinned,
            base,
            supervisor_name,
            watchdog_name,
            minimum_supervisor_restarts=1,
            minimum_watchdog_restarts=1,
        )
        recovered_health = run_pinned(
            base
            + [
                "container",
                "exec",
                watchdog_name,
                WATCHDOG_PATH,
                "--installed-local-authority",
                "--health-json",
            ]
        )
        if recovered_health.returncode != 0 or recovered_health.stderr:
            _fail("runtime-recovered-health-failed")
        recovered_health_sha256 = _validate_health_output(
            recovered_health.stdout, expected_health
        )
        recovered_smoke = run_pinned(
            compose
            + [
                "exec",
                "-T",
                "release-watchdog",
                SUPERVISOR_PATH,
                "--installed-local-authority",
                "--request-smoke",
            ]
        )
        if (
            recovered_smoke.returncode != 0
            or recovered_smoke.stdout
            or recovered_smoke.stderr
        ):
            _fail("runtime-recovered-request-smoke-failed")
        final_image_receipt = oci.verify_pinned(
            layout_fd,
            wrapper=wrapper,
            external_anchor=external_anchor,
        )
        final_tree = oci._snapshot_oci_tree(layout_fd)
        if final_image_receipt != image_receipt or final_tree != initial_tree:
            _fail("runtime-pinned-layout-mutated")
        lifecycle_evidence = {
            "before_inventory": before_inventory,
            "health_sha256": health_sha256,
            "initial_health_sha256": initial_health_sha256,
            "recovered_health_sha256": recovered_health_sha256,
            "recovered_state": recovered_state,
        }
    finally:
        cleanup_error: RuntimeGateError | None = None
        if up_attempted and docker_config is not None and compose_fd >= 0:
            def record_cleanup_error(error: RuntimeGateError) -> None:
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error

            def cleanup_run(command: Sequence[str]) -> CommandResult | None:
                # Anchor drift invalidates the proof, but cannot be allowed to
                # strand this gate's sealed, randomly named Docker project.
                for validation in (
                    lambda: _revalidate_sealed_memfd(compose_fd, compose_bytes),
                    lambda: _revalidate_anchor(anchor),
                ):
                    try:
                        validation()
                    except RuntimeGateError as error:
                        record_cleanup_error(error)
                try:
                    result = _run(command_runner, command, environment)
                except RuntimeGateError as error:
                    record_cleanup_error(error)
                    return None
                for validation in (
                    lambda: _revalidate_anchor(anchor),
                    lambda: _revalidate_sealed_memfd(compose_fd, compose_bytes),
                ):
                    try:
                        validation()
                    except RuntimeGateError as error:
                        record_cleanup_error(error)
                return result

            def cleanup_lines(
                result: CommandResult | None,
                *,
                validator: Callable[[str], bool],
            ) -> list[str] | None:
                if result is None or result.returncode != 0 or result.stderr:
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
                    return None
                try:
                    values = (
                        []
                        if not result.stdout
                        else result.stdout.decode("ascii").splitlines()
                    )
                except UnicodeDecodeError:
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
                    return None
                if len(values) != len(set(values)) or any(
                    not validator(value) for value in values
                ):
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
                    return None
                return values

            stopped = cleanup_run(
                compose
                + [
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "10",
                ]
            )
            if stopped is None or stopped.returncode != 0:
                record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))

            container_query = base + [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={RUNTIME_LABEL}={project}",
            ]
            volume_query = base + [
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
            network_query = base + [
                "network",
                "ls",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
            remaining_ids = cleanup_lines(
                cleanup_run(container_query),
                validator=lambda value: CONTAINER_ID_RE.fullmatch(value) is not None,
            )
            expected_volumes = {
                f"{project}_runtime-socket",
                f"{project}_authority-replay-state-v1",
            }
            remaining_volumes = cleanup_lines(
                cleanup_run(volume_query),
                validator=lambda value: value in expected_volumes,
            )
            remaining_networks = cleanup_lines(
                cleanup_run(network_query),
                validator=lambda value: CONTAINER_ID_RE.fullmatch(value) is not None,
            )
            if remaining_ids:
                forced = cleanup_run(
                    base + ["container", "rm", "--force", *remaining_ids]
                )
                if forced is None or forced.returncode != 0 or forced.stderr:
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
            if remaining_volumes:
                removed = cleanup_run(
                    base + ["volume", "rm", *remaining_volumes]
                )
                if removed is None or removed.returncode != 0 or removed.stderr:
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
            if remaining_networks:
                removed_networks = cleanup_run(
                    base + ["network", "rm", *remaining_networks]
                )
                if (
                    removed_networks is None
                    or removed_networks.returncode != 0
                    or removed_networks.stderr
                ):
                    record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))

            final_ids = cleanup_lines(
                cleanup_run(container_query),
                validator=lambda value: CONTAINER_ID_RE.fullmatch(value) is not None,
            )
            final_volumes = cleanup_lines(
                cleanup_run(volume_query),
                validator=lambda value: value in expected_volumes,
            )
            final_networks = cleanup_lines(
                cleanup_run(network_query),
                validator=lambda value: CONTAINER_ID_RE.fullmatch(value) is not None,
            )
            cleanup_complete = (
                final_ids == [] and final_volumes == [] and final_networks == []
            )
            if not cleanup_complete:
                record_cleanup_error(RuntimeGateError("runtime-cleanup-failed"))
        if docker_config is not None:
            docker_config.cleanup()
        if compose_fd >= 0:
            os.close(compose_fd)
        if layout_fd >= 0:
            os.close(layout_fd)
        os.close(anchor.file_fd)
        os.close(anchor.parent_fd)
        if cleanup_error is not None:
            raise cleanup_error
    if not cleanup_complete:
        _fail("runtime-cleanup-failed")
    # The runtime containers and volumes are gone; compare all pre-existing
    # container identities, status, health, and restart counts byte-for-byte.
    docker_config_after = tempfile.TemporaryDirectory(prefix="pq-runtime-after-")
    try:
        after_environment = {
            "DOCKER_CONFIG": docker_config_after.name,
            "HOME": docker_config_after.name,
            "PATH": "/usr/bin:/bin",
        }
        base = [docker_binary, "--host", docker_host]

        def after_run(command: Sequence[str]) -> CommandResult:
            return _run(command_runner, command, after_environment)

        after_inventory = _container_inventory(
            command_runner, base, after_environment, after_run
        )
    finally:
        docker_config_after.cleanup()
    if after_inventory != lifecycle_evidence["before_inventory"]:
        _fail("preexisting-container-health-changed")
    recovered_state = lifecycle_evidence["recovered_state"]
    if not isinstance(recovered_state, RuntimeState):  # pragma: no cover - invariant
        _fail("runtime-state-invalid")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": 2,
        "authoritative_for_local_runtime_gate": True,
        "authoritative_for_release_effects": False,
        "production_ready": False,
        "performs_release_effects": False,
        "network_requests_permitted": False,
        "docker_store_mutated": True,
        "runtime_plane_started": True,
        "runtime_plane_ready": True,
        "socket_accepting": True,
        "framed_request_fail_closed": True,
        "drift_restart_verified": True,
        "runtime_plane_removed": True,
        "no_runtime_containers_retained": True,
        "no_runtime_networks_retained": True,
        "no_runtime_volumes_retained": True,
        "preexisting_container_health_unchanged": True,
        "image_id": image_id,
        "image_digest": image_receipt["image_digest"],
        "runtime_compose_sha256": _digest(compose_bytes),
        "external_anchor_sha256": _digest(anchor.data),
        "authentication_sha256": image_receipt["authentication_sha256"],
        "native_build_receipt_sha256": _digest(native_receipt_bytes),
        "source_manifest_sha256": source_manifest_digest,
        "before_container_health_sha256": _digest(before_inventory),
        "after_container_health_sha256": _digest(after_inventory),
        "initial_health_stdout_sha256": lifecycle_evidence[
            "initial_health_sha256"
        ],
        "health_stdout_sha256": lifecycle_evidence["health_sha256"],
        "recovered_health_stdout_sha256": lifecycle_evidence[
            "recovered_health_sha256"
        ],
        "supervisor_restart_count": recovered_state.supervisor_restart_count,
        "watchdog_restart_count": recovered_state.watchdog_restart_count,
        "runtime_user": runtime_user,
        "authority_user": authority_user,
        "caller_user": caller_user,
    }
    receipt_bytes = _canonical(receipt)
    receipt_path = Path(output_receipt)
    _write_receipt(receipt_path, receipt_bytes)
    return RuntimeGateResult(str(receipt_path), _digest(receipt_bytes), image_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in PropertyQuarry persistent no-effects runtime gate."
    )
    parser.add_argument("--layout", required=True)
    parser.add_argument("--wrapper", required=True)
    parser.add_argument("--external-anchor", required=True)
    parser.add_argument("--native-build-receipt", required=True)
    parser.add_argument("--output-receipt", required=True)
    parser.add_argument("--docker-binary", default="/usr/bin/docker")
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_gate(
            layout=arguments.layout,
            wrapper=arguments.wrapper,
            external_anchor=arguments.external_anchor,
            native_build_receipt=arguments.native_build_receipt,
            output_receipt=arguments.output_receipt,
            docker_binary=arguments.docker_binary,
            docker_host=arguments.docker_host,
        )
    except (
        RuntimeGateError,
        container_gate.ContainerGateError,
        oci.MaterializationError,
    ) as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    print(
        _canonical(
            {
                "image_id": result.image_id,
                "receipt": result.receipt,
                "receipt_sha256": result.receipt_sha256,
            }
        ).decode("ascii")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
