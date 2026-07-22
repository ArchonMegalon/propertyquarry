#!/usr/bin/env python3
"""Run the PostgreSQL browser contract without touching the live PQ stack.

The outer process re-executes this controller in a bounded user systemd scope.
Inside that scope, Docker is used only for one digest-pinned PostgreSQL
container plus its uniquely named network and volume.  Candidate migrations,
the API, session bootstrap, and Playwright all run from the selected worktree
virtual environment.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import hmac
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import site
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Final, Mapping, MutableMapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


POSTGRES_IMAGE: Final = (
    "postgres:16-alpine@sha256:"
    "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)
DOCKER_HOST: Final = "unix:///var/run/docker.sock"
RUN_LABEL_KEY: Final = "propertyquarry.postgres-browser-e2e.run"
RUN_ID_RE: Final = re.compile(r"[0-9a-f]{16}\Z")
IMAGE_ID_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
NETWORK_ID_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
RESOURCE_NAME_RE: Final = re.compile(r"pq-pg-e2e-[0-9a-f]{16}-(?:db|net|data)\Z")
ADMISSION_CAPACITY_OWNER_ROLE_RE: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
DISPOSABLE_API_ADMISSION_ROLE: Final = "propertyquarry_api_admission"
DISPOSABLE_DATABASE_PASSWORD_RE: Final = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
POSTGRES_SCRAM_ITERATIONS: Final = 4096
POSTGRES_SCRAM_SALT_BYTES: Final = 16
POSTGRES_SCRAM_VERIFIER_RE: Final = re.compile(
    r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$"
    r"[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=\Z"
)

HOST_MEMORY_MAX_BYTES: Final = 1024 * 1024 * 1024
HOST_SWAP_MAX_BYTES: Final = 0
HOST_TASKS_MAX: Final = 128
HOST_CPU_MAX_RATIO: Final = 1.0
HOST_RUNTIME_MAX_SECONDS: Final = 1200
INTERNAL_RUNTIME_MAX_SECONDS: Final = 840
POSTGRES_MEMORY: Final = "512m"
POSTGRES_CPUS: Final = "1.0"
POSTGRES_PIDS: Final = "128"
POSTGRES_VOLUME_MAX_BYTES: Final = 256 * 1024 * 1024
POSTGRES_CONTAINER_PORT_NUMBER: Final = 5432
MAX_READY_ATTEMPTS: Final = 60
MAX_SESSION_BYTES: Final = 1024 * 1024
CLEANUP_COMMAND_TIMEOUT_SECONDS: Final = 15
API_TERM_TIMEOUT_SECONDS: Final = 15
API_KILL_TIMEOUT_SECONDS: Final = 5
PRODUCER_TERM_TIMEOUT_SECONDS: Final = 2
PRODUCER_KILL_TIMEOUT_SECONDS: Final = 2
STORAGE_GUARD_JOIN_TIMEOUT_SECONDS: Final = 6
CLEANUP_DOCKER_COMMAND_COUNT: Final = 15
DATABASE_RELAY_MAX_CONNECTIONS: Final = 8
DATABASE_RELAY_BACKLOG: Final = 16
DATABASE_RELAY_CONNECT_TIMEOUT_SECONDS: Final = 5.0
DATABASE_RELAY_IO_TIMEOUT_SECONDS: Final = 5.0
DATABASE_RELAY_POLL_SECONDS: Final = 0.25
DATABASE_RELAY_JOIN_TIMEOUT_SECONDS: Final = 5.0
DATABASE_RELAY_BUFFER_BYTES: Final = 64 * 1024
RELAY_STOP_WORST_CASE_SECONDS: Final = 2 * DATABASE_RELAY_JOIN_TIMEOUT_SECONDS
CLEANUP_WORST_CASE_SECONDS: Final = (
    CLEANUP_DOCKER_COMMAND_COUNT * CLEANUP_COMMAND_TIMEOUT_SECONDS
    + API_TERM_TIMEOUT_SECONDS
    + API_KILL_TIMEOUT_SECONDS
    + PRODUCER_TERM_TIMEOUT_SECONDS
    + PRODUCER_KILL_TIMEOUT_SECONDS
    + RELAY_STOP_WORST_CASE_SECONDS
    + STORAGE_GUARD_JOIN_TIMEOUT_SECONDS
)
CLEANUP_SAFETY_MARGIN_SECONDS: Final = 60
PRODUCER_LOG_MAX_BYTES: Final = 8 * 1024 * 1024
PRODUCER_FILE_MAX_BYTES: Final = 8 * 1024 * 1024
BROWSER_PRODUCER_FILE_MAX_BYTES: Final = 128 * 1024 * 1024
DEPENDENCY_OVERLAY_MAX_BYTES: Final = 128 * 1024 * 1024
DEPENDENCY_DISTRIBUTION_MAX: Final = 512
DEPENDENCY_PROFILE_FILE_RECORD_MAX: Final = 8192
RUN_TEMP_MAX_BYTES: Final = 512 * 1024 * 1024
RUN_TEMP_MAX_ENTRIES: Final = 16_384
RUN_STORAGE_POLL_SECONDS: Final = 0.1
FILESYSTEM_BLOCK_BYTES: Final = 512
PRLIMIT_BINARY: Final = "/usr/bin/prlimit"
LIVE_REPOSITORY_ROOT: Final = Path("/docker/property")
CHROMIUM_EXECUTABLE_ENV: Final = "PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE"
CHROMIUM_HEADLESS_SHELL_LAYOUT_RE: Final = re.compile(
    r"chromium_headless_shell-[1-9][0-9]*\Z"
)
CHROMIUM_HEADLESS_SHELL_PARENT: Final = "chrome-headless-shell-linux64"
CHROMIUM_HEADLESS_SHELL_NAME: Final = "chrome-headless-shell"
MAX_CHROMIUM_HEADLESS_SHELL_BYTES: Final = 512 * 1024 * 1024
POSTGRES_CHROMIUM_ARGS: Final = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-proxy-server",
)
DEPENDENCY_PROFILE: Final = (
    ("annotated-doc", "0.0.4"),
    ("annotated-types", "0.7.0"),
    ("anyio", "4.13.0"),
    ("fastapi", "0.135.1"),
    ("h11", "0.16.0"),
    ("httpcore", "1.0.9"),
    ("httpx", "0.28.1"),
    ("iniconfig", "2.3.0"),
    ("numpy", "2.3.5"),
    ("packaging", "26.2"),
    ("pluggy", "1.6.0"),
    ("psycopg", "3.3.4"),
    ("psycopg-binary", "3.3.4"),
    ("pydantic", "2.12.5"),
    ("pydantic-core", "2.41.5"),
    ("PyJWT", "2.13.0"),
    ("Pygments", "2.20.0"),
    ("pytest", "9.0.3"),
    ("starlette", "1.3.1"),
    ("typing-extensions", "4.15.0"),
    ("typing-inspection", "0.4.2"),
)
COLLISION_PHASE_SUFFIXES: Final = (
    "container-name",
    "container-label",
    "network-name",
    "network-label",
    "volume-name",
    "volume-label",
)
COMMAND_PHASES: Final = frozenset(
    {
        *(f"docker-preflight-{suffix}" for suffix in COLLISION_PHASE_SUFFIXES),
        *(f"cleanup-inventory-{suffix}" for suffix in COLLISION_PHASE_SUFFIXES),
        "docker-image-inspect",
        "docker-network-create",
        "docker-network-label",
        "docker-volume-create",
        "docker-volume-label",
        "docker-container-create",
        "docker-container-label",
        "docker-health-inspect",
        "docker-address-inspect",
        "docker-role-bootstrap",
        "docker-role-verify",
        "schema-migrate",
        "schema-check",
        "session-bootstrap",
        "browser-test",
        "cleanup-container-presence",
        "cleanup-container-label",
        "cleanup-container-remove",
        "cleanup-network-presence",
        "cleanup-network-label",
        "cleanup-network-remove",
        "cleanup-volume-presence",
        "cleanup-volume-label",
        "cleanup-volume-remove",
    }
)
HOST_COMMAND_PHASES: Final = frozenset(
    {"schema-migrate", "schema-check", "session-bootstrap", "browser-test"}
)
OUTPUT_COMMAND_PHASES: Final = COMMAND_PHASES - HOST_COMMAND_PHASES
COLLISION_COMMAND_PHASES: Final = frozenset(
    {
        *(f"docker-preflight-{suffix}" for suffix in COLLISION_PHASE_SUFFIXES),
        *(f"cleanup-inventory-{suffix}" for suffix in COLLISION_PHASE_SUFFIXES),
    }
)
OUTPUT_FAILURE_REASONS: Final = frozenset(
    {
        "execution-failed",
        "timeout",
        "exit-nonzero",
        "stderr-not-empty",
        "stdout-too-large",
    }
)
HOST_FAILURE_REASONS: Final = frozenset(
    {
        "execution-failed",
        "exit-nonzero",
        "log-invalid",
    }
)
COMMAND_FAILURE_REASONS: Final = (
    OUTPUT_FAILURE_REASONS | HOST_FAILURE_REASONS | frozenset({"collision"})
)
PHASE_SEMANTIC_FAILURE_CODES: Final = frozenset(
    {
        "docker-image-inspect-output-invalid",
        "docker-network-create-output-invalid",
        "docker-network-label-mismatch",
        "docker-volume-create-output-invalid",
        "docker-volume-label-mismatch",
        "docker-container-create-output-invalid",
        "docker-container-label-mismatch",
        "docker-health-output-invalid",
        "docker-health-container-exited",
        "docker-health-timeout",
        "docker-address-output-invalid",
        "admission-capacity-owner-role-invalid",
        "docker-role-bootstrap-output-invalid",
        "docker-role-verification-mismatch",
        "api-admission-role-dsn-invalid",
        "api-admission-role-collision",
        "api-admission-role-provision-failed",
        "api-admission-role-verification-failed",
        "libpq-environment-not-closed",
        "database-relay-start-failed",
        "database-relay-runtime-failed",
        "database-relay-stop-failed",
        "dependency-snapshot-invalid",
        "producer-file-limit-exceeded",
        "producer-file-limit-unavailable",
        "producer-log-limit-exceeded",
        "producer-process-group-invalid",
        "run-storage-limit-exceeded",
        "candidate-api-start-failed",
        "candidate-api-log-invalid",
        "candidate-api-exited",
        "candidate-api-readiness-timeout",
        "bootstrap-session-invalid",
        "bootstrap-session-copy-failed",
        "bootstrap-session-source-cleanup-failed",
        "cleanup-container-label-mismatch",
        "cleanup-network-label-mismatch",
        "cleanup-volume-label-mismatch",
        "internal-watchdog-expired",
    }
)
SAFE_SCOPED_FAILURE_CODES: Final = frozenset(
    {
        *(
            f"{phase}-{reason}"
            for phase in OUTPUT_COMMAND_PHASES
            for reason in OUTPUT_FAILURE_REASONS
        ),
        *(
            f"{phase}-{reason}"
            for phase in HOST_COMMAND_PHASES
            for reason in HOST_FAILURE_REASONS
        ),
        *(f"{phase}-collision" for phase in COLLISION_COMMAND_PHASES),
        *PHASE_SEMANTIC_FAILURE_CODES,
    }
)
SCOPED_FAILURE_LINE_RE: Final = re.compile(
    rb"isolated PostgreSQL browser gate failed: ([a-z0-9][a-z0-9-]{0,159})\n\Z"
)
MAX_SCOPED_DIAGNOSTIC_BYTES: Final = 512


class IsolatedPostgresError(RuntimeError):
    """A bounded, secret-free harness failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _LifecycleGuard:
    """Raise early enough that the resource finally block can finish safely."""

    def __init__(
        self,
        *,
        timeout_seconds: int = INTERNAL_RUNTIME_MAX_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], object] | None = None,
        kill: Callable[[int, int], None] | None = None,
        signal_getter: Callable[[int], object] | None = None,
        signal_setter: Callable[[int, object], object] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds >= HOST_RUNTIME_MAX_SECONDS:
            _fail("internal-watchdog-invalid")
        self.timeout_seconds = timeout_seconds
        self._timer_factory = timer_factory or threading.Timer
        self._kill = kill or os.kill
        self._signal_getter = signal_getter or signal.getsignal
        self._signal_setter = signal_setter or signal.signal
        self._timer: object | None = None
        self._previous: dict[int, object] = {}
        self._watchdog_expired = False
        self._unwinding = False

    def _expire(self) -> None:
        self._watchdog_expired = True
        self._kill(os.getpid(), signal.SIGTERM)

    def begin_cleanup(self) -> None:
        if self._unwinding:
            return
        self._unwinding = True
        self._signal_setter(signal.SIGTERM, signal.SIG_IGN)
        self._signal_setter(signal.SIGINT, signal.SIG_IGN)

    def _handle(self, signum: int, _frame: object) -> None:
        if self._unwinding:
            return
        self.begin_cleanup()
        _fail(
            "internal-watchdog-expired"
            if self._watchdog_expired
            else f"harness-signal-{signal.Signals(signum).name.lower()}"
        )

    def __enter__(self) -> "_LifecycleGuard":
        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous[signum] = self._signal_getter(signum)
            self._signal_setter(signum, self._handle)
        timer = self._timer_factory(self.timeout_seconds, self._expire)
        try:
            setattr(timer, "daemon", True)
            getattr(timer, "start")()
        except (AttributeError, OSError, RuntimeError):
            for signum, previous in self._previous.items():
                self._signal_setter(signum, previous)
            _fail("internal-watchdog-start-failed")
        self._timer = timer
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._timer is not None:
            try:
                getattr(self._timer, "cancel")()
            except (AttributeError, RuntimeError):
                pass
        if not self._unwinding:
            for signum, previous in self._previous.items():
                self._signal_setter(signum, previous)


class ResourceNames(tuple):
    """Immutable, destructurable names for one disposable database run."""

    __slots__ = ()

    def __new__(cls, run_id: str) -> "ResourceNames":
        if RUN_ID_RE.fullmatch(run_id) is None:
            raise IsolatedPostgresError("run-id-invalid")
        prefix = f"pq-pg-e2e-{run_id}"
        return tuple.__new__(cls, (f"{prefix}-db", f"{prefix}-net", f"{prefix}-data"))

    @property
    def container(self) -> str:
        return self[0]

    @property
    def network(self) -> str:
        return self[1]

    @property
    def volume(self) -> str:
        return self[2]


def _fail(code: str) -> None:
    raise IsolatedPostgresError(code)


def _private_container_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        _fail("docker-address-output-invalid")
    private_networks = (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    )
    if not any(address in network for network in private_networks):
        _fail("docker-address-output-invalid")
    return str(address)


class _LoopbackDatabaseRelay:
    """Bounded host relay from loopback into one internal Docker network."""

    def __init__(
        self,
        target_ipv4: str,
        *,
        connector: Callable[[tuple[str, int], float], socket.socket] = (
            socket.create_connection
        ),
    ) -> None:
        self._target_ipv4 = _private_container_ipv4(target_ipv4)
        self._connector = connector
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._slots = threading.BoundedSemaphore(DATABASE_RELAY_MAX_CONNECTIONS)
        self._lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._active_sockets: set[socket.socket] = set()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> int:
        if self._listener is not None or self._accept_thread is not None:
            _fail("database-relay-start-failed")
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(DATABASE_RELAY_BACKLOG)
            listener.settimeout(DATABASE_RELAY_POLL_SECONDS)
            port = int(listener.getsockname()[1])
            thread = threading.Thread(
                target=self._accept_loop,
                name="pq-postgres-loopback-relay",
                daemon=True,
            )
            self._listener = listener
            self._accept_thread = thread
            thread.start()
        except (OSError, RuntimeError):
            if listener is not None:
                listener.close()
            self._listener = None
            self._accept_thread = None
            _fail("database-relay-start-failed")
        if port <= 0 or port > 65_535:
            self.stop()
            _fail("database-relay-start-failed")
        return port

    def assert_healthy(self) -> None:
        accept_thread = self._accept_thread
        if (
            self._failed.is_set()
            or accept_thread is None
            or (not self._stop.is_set() and not accept_thread.is_alive())
        ):
            _fail("database-relay-runtime-failed")

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            self._failed.set()
            return
        while not self._stop.is_set():
            if not self._slots.acquire(timeout=DATABASE_RELAY_POLL_SECONDS):
                continue
            try:
                client, peer = listener.accept()
            except socket.timeout:
                self._slots.release()
                continue
            except OSError:
                self._slots.release()
                if not self._stop.is_set():
                    self._failed.set()
                return
            if peer[0] != "127.0.0.1":
                client.close()
                self._slots.release()
                self._failed.set()
                return
            worker = threading.Thread(
                target=self._relay_connection,
                args=(client,),
                name="pq-postgres-loopback-connection",
                daemon=True,
            )
            with self._lock:
                self._workers.add(worker)
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._workers.discard(worker)
                client.close()
                self._slots.release()
                self._failed.set()
                return

    def _relay_connection(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = self._connector(
                (self._target_ipv4, POSTGRES_CONTAINER_PORT_NUMBER),
                DATABASE_RELAY_CONNECT_TIMEOUT_SECONDS,
            )
            client.settimeout(DATABASE_RELAY_IO_TIMEOUT_SECONDS)
            upstream.settimeout(DATABASE_RELAY_IO_TIMEOUT_SECONDS)
            with self._lock:
                self._active_sockets.update((client, upstream))
            readers = [client, upstream]
            while readers and not self._stop.is_set():
                readable, _, _ = select.select(
                    readers, [], [], DATABASE_RELAY_POLL_SECONDS
                )
                for source in readable:
                    destination = upstream if source is client else client
                    chunk = source.recv(DATABASE_RELAY_BUFFER_BYTES)
                    if not chunk:
                        readers.remove(source)
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    destination.sendall(chunk)
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._failed.set()
        finally:
            for connection in (client, upstream):
                if connection is None:
                    continue
                with self._lock:
                    self._active_sockets.discard(connection)
                try:
                    connection.close()
                except OSError:
                    pass
            with self._lock:
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        accept_thread = self._accept_thread
        if accept_thread is not None:
            accept_thread.join(DATABASE_RELAY_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            active_sockets = tuple(self._active_sockets)
        for connection in active_sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        deadline = time.monotonic() + DATABASE_RELAY_JOIN_TIMEOUT_SECONDS
        while True:
            with self._lock:
                workers = tuple(self._workers)
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for worker in workers:
                worker.join(min(remaining, DATABASE_RELAY_POLL_SECONDS))
        with self._lock:
            workers_alive = any(worker.is_alive() for worker in self._workers)
        if (
            (accept_thread is not None and accept_thread.is_alive())
            or workers_alive
        ):
            _fail("database-relay-stop-failed")


def _require_absolute_executable(path_text: str, code: str) -> str:
    path = Path(path_text)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        _fail(code)
    return str(path)


@dataclass(frozen=True)
class _DependencyFile:
    relative_path: str
    size: int
    sha256: str
    executable: bool


@dataclass(frozen=True)
class _DependencySnapshot:
    source_site: Path
    files: tuple[_DependencyFile, ...]
    manifest_sha256: str
    total_bytes: int


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def _raw_owned_directory(path: Path, *, owned_from: Path) -> None:
    if (
        not path.is_absolute()
        or not owned_from.is_absolute()
        or ".." in path.parts
        or ".." in owned_from.parts
    ):
        _fail("dependency-snapshot-invalid")
    try:
        path.relative_to(owned_from)
    except ValueError:
        _fail("dependency-snapshot-invalid")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            _fail("dependency-snapshot-invalid")
        if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
            _fail("dependency-snapshot-invalid")
        if current == owned_from or current.is_relative_to(owned_from):
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o002:
                _fail("dependency-snapshot-invalid")


def _source_file_digest(path: Path, *, maximum: int) -> tuple[int, str, bool]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o002
            or before.st_size < 0
            or before.st_size > maximum
        ):
            _fail("dependency-snapshot-invalid")
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > maximum:
                _fail("dependency-snapshot-invalid")
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError:
        _fail("dependency-snapshot-invalid")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after) or observed != before.st_size:
        _fail("dependency-snapshot-invalid")
    return observed, digest.hexdigest(), bool(stat.S_IMODE(before.st_mode) & 0o111)


def _dependency_source_snapshot() -> _DependencySnapshot:
    raw_site = site.getusersitepackages()
    raw_base = site.getuserbase()
    if not isinstance(raw_site, str) or not isinstance(raw_base, str):
        _fail("dependency-snapshot-invalid")
    source_site = Path(raw_site)
    user_base = Path(raw_base)
    _raw_owned_directory(source_site, owned_from=user_base)
    distributions: dict[str, importlib.metadata.Distribution] = {}
    try:
        discovered = importlib.metadata.distributions(path=[str(source_site)])
    except (OSError, ValueError):
        _fail("dependency-snapshot-invalid")
    try:
        for distribution_count, distribution in enumerate(discovered, start=1):
            if distribution_count > DEPENDENCY_DISTRIBUTION_MAX:
                _fail("dependency-snapshot-invalid")
            name = _normalized_distribution_name(
                distribution.metadata.get("Name", "")
            )
            if name and name not in distributions:
                distributions[name] = distribution
    except (OSError, ValueError):
        _fail("dependency-snapshot-invalid")
    selected: dict[str, _DependencyFile] = {}
    listed_file_count = 0
    for expected_name, expected_version in DEPENDENCY_PROFILE:
        distribution = distributions.get(_normalized_distribution_name(expected_name))
        if distribution is None or distribution.version != expected_version:
            _fail("dependency-snapshot-invalid")
        files = distribution.files
        if not files:
            _fail("dependency-snapshot-invalid")
        for package_path in files:
            listed_file_count += 1
            if listed_file_count > DEPENDENCY_PROFILE_FILE_RECORD_MAX:
                _fail("dependency-snapshot-invalid")
            relative = Path(str(package_path))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = source_site / relative
            current = source_site
            for part in relative.parts[:-1]:
                current /= part
                try:
                    metadata = current.lstat()
                except OSError:
                    _fail("dependency-snapshot-invalid")
                if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                    _fail("dependency-snapshot-invalid")
            if source.is_symlink():
                _fail("dependency-snapshot-invalid")
            size, digest, executable = _source_file_digest(
                source, maximum=DEPENDENCY_OVERLAY_MAX_BYTES
            )
            key = relative.as_posix()
            entry = _DependencyFile(key, size, digest, executable)
            previous = selected.get(key)
            if previous is not None and previous != entry:
                _fail("dependency-snapshot-invalid")
            selected[key] = entry
    ordered = tuple(selected[key] for key in sorted(selected))
    total = sum(entry.size for entry in ordered)
    if not ordered or total <= 0 or total > DEPENDENCY_OVERLAY_MAX_BYTES:
        _fail("dependency-snapshot-invalid")
    manifest = {
        "profile": list(DEPENDENCY_PROFILE),
        "files": [
            [entry.relative_path, entry.size, entry.sha256, entry.executable]
            for entry in ordered
        ],
    }
    manifest_sha256 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _DependencySnapshot(source_site, ordered, manifest_sha256, total)


def _copy_dependency_overlay(
    snapshot: _DependencySnapshot, *, temp_root: Path
) -> tuple[Path, Path]:
    overlay_base = temp_root / "dependency-overlay"
    overlay_site = (
        overlay_base
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    try:
        overlay_site.mkdir(parents=True, mode=0o700)
    except OSError:
        _fail("dependency-snapshot-invalid")
    directories = {overlay_base, overlay_base / "lib", overlay_site.parent, overlay_site}
    for entry in snapshot.files:
        source = snapshot.source_site / entry.relative_path
        destination = overlay_site / entry.relative_path
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            _fail("dependency-snapshot-invalid")
        current = destination.parent
        while current.is_relative_to(overlay_base):
            directories.add(current)
            if current == overlay_base:
                break
            current = current.parent
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            source_fd = os.open(source, source_flags)
            destination_fd = os.open(destination, destination_flags, 0o600)
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > entry.size:
                    _fail("dependency-snapshot-invalid")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        _fail("dependency-snapshot-invalid")
                    view = view[written:]
            os.fsync(destination_fd)
        except OSError:
            _fail("dependency-snapshot-invalid")
        finally:
            if "source_fd" in locals():
                os.close(source_fd)
                del source_fd
            if "destination_fd" in locals():
                os.close(destination_fd)
                del destination_fd
        if copied != entry.size or digest.hexdigest() != entry.sha256:
            _fail("dependency-snapshot-invalid")
        try:
            destination.chmod(0o500 if entry.executable else 0o400)
        except OSError:
            _fail("dependency-snapshot-invalid")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            # Owner write permission is retained solely so TemporaryDirectory can
            # remove the private overlay.  Group/world mutation stays forbidden.
            directory.chmod(0o700)
        except OSError:
            _fail("dependency-snapshot-invalid")
    _verify_dependency_overlay(snapshot, overlay_site=overlay_site)
    return overlay_base, overlay_site


def _verify_dependency_overlay(
    snapshot: _DependencySnapshot, *, overlay_site: Path
) -> None:
    expected = {entry.relative_path: entry for entry in snapshot.files}
    observed: set[str] = set()
    stack = [overlay_site]
    entry_count = 0
    while stack:
        directory = stack.pop()
        try:
            directory_metadata = directory.lstat()
        except OSError:
            _fail("dependency-snapshot-invalid")
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory.is_symlink()
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            _fail("dependency-snapshot-invalid")
        try:
            with os.scandir(directory) as entries:
                for item in entries:
                    entry_count += 1
                    if entry_count > RUN_TEMP_MAX_ENTRIES:
                        _fail("dependency-snapshot-invalid")
                    try:
                        metadata = item.stat(follow_symlinks=False)
                    except OSError:
                        _fail("dependency-snapshot-invalid")
                    path = Path(item.path)
                    mode = stat.S_IMODE(metadata.st_mode)
                    if item.is_symlink() or mode & 0o022:
                        _fail("dependency-snapshot-invalid")
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append(path)
                        continue
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                    ):
                        _fail("dependency-snapshot-invalid")
                    relative = path.relative_to(overlay_site).as_posix()
                    entry = expected.get(relative)
                    if entry is None:
                        _fail("dependency-snapshot-invalid")
                    if mode != (0o500 if entry.executable else 0o400):
                        _fail("dependency-snapshot-invalid")
                    size, digest, executable = _source_file_digest(
                        path, maximum=DEPENDENCY_OVERLAY_MAX_BYTES
                    )
                    if (
                        size != entry.size
                        or digest != entry.sha256
                        or executable != entry.executable
                    ):
                        _fail("dependency-snapshot-invalid")
                    observed.add(relative)
        except OSError:
            _fail("dependency-snapshot-invalid")
    if observed != set(expected):
        _fail("dependency-snapshot-invalid")


def _regular_file_shape(path: Path, *, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail("worktree-venv-invalid")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or metadata.st_size <= 0
        or metadata.st_size > maximum
        or stat.S_IMODE(metadata.st_mode) & 0o002
    ):
        _fail("worktree-venv-invalid")
    return metadata


def _validate_worktree(repo_root_text: str, venv_text: str) -> tuple[Path, str]:
    try:
        repo_root = Path(repo_root_text).resolve(strict=True)
    except OSError:
        _fail("worktree-invalid")
    if not (repo_root / "ea" / "app").is_dir() or not (repo_root / "tests").is_dir():
        _fail("worktree-invalid")
    try:
        live_root = LIVE_REPOSITORY_ROOT.resolve(strict=True)
    except OSError:
        live_root = LIVE_REPOSITORY_ROOT
    if repo_root == live_root:
        _fail("live-worktree-forbidden")
    supplied_venv = Path(venv_text)
    candidate_venv = supplied_venv if supplied_venv.is_absolute() else repo_root / supplied_venv
    try:
        candidate_metadata = candidate_venv.lstat()
    except OSError:
        _fail("worktree-venv-invalid")
    if (
        not stat.S_ISDIR(candidate_metadata.st_mode)
        or candidate_venv.is_symlink()
        or candidate_metadata.st_uid != os.getuid()
        or stat.S_IMODE(candidate_metadata.st_mode) & 0o002
    ):
        _fail("worktree-venv-invalid")
    try:
        venv = candidate_venv.resolve(strict=True)
    except OSError:
        _fail("worktree-venv-invalid")
    if not venv.is_dir() or venv.is_symlink():
        _fail("worktree-venv-invalid")
    bin_dir = venv / "bin"
    try:
        bin_metadata = bin_dir.lstat()
    except OSError:
        _fail("worktree-venv-invalid")
    if (
        not stat.S_ISDIR(bin_metadata.st_mode)
        or bin_dir.is_symlink()
        or bin_metadata.st_uid != os.getuid()
        or stat.S_IMODE(bin_metadata.st_mode) & 0o002
    ):
        _fail("worktree-venv-invalid")
    config = venv / "pyvenv.cfg"
    _regular_file_shape(config, maximum=65_536)
    try:
        config_text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _fail("worktree-venv-invalid")
    if not any(line.strip().lower().startswith("home =") for line in config_text.splitlines()):
        _fail("worktree-venv-invalid")
    python = venv / "bin" / "python"
    _require_absolute_executable(str(python), "worktree-venv-invalid")
    return repo_root, str(python)


def _validate_chromium_headless_shell(path_text: str) -> str:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        _fail("chromium-headless-shell-invalid")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError:
        _fail("chromium-headless-shell-invalid")
    if resolved != candidate or candidate.is_symlink():
        _fail("chromium-headless-shell-invalid")
    if (
        len(candidate.parts) < 3
        or candidate.name != CHROMIUM_HEADLESS_SHELL_NAME
        or candidate.parent.name != CHROMIUM_HEADLESS_SHELL_PARENT
        or CHROMIUM_HEADLESS_SHELL_LAYOUT_RE.fullmatch(candidate.parent.parent.name)
        is None
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or metadata.st_size < 4
        or metadata.st_size > MAX_CHROMIUM_HEADLESS_SHELL_BYTES
        or stat.S_IMODE(metadata.st_mode) & 0o002
        or stat.S_IMODE(metadata.st_mode) & 0o111 == 0
        or not os.access(candidate, os.X_OK)
    ):
        _fail("chromium-headless-shell-invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        magic = os.read(descriptor, 4)
        after = os.fstat(descriptor)
    except OSError:
        _fail("chromium-headless-shell-invalid")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    identity = lambda value: (  # noqa: E731 - closed metadata tuple
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(metadata) != identity(opened) or identity(opened) != identity(after):
        _fail("chromium-headless-shell-invalid")
    if magic != b"\x7fELF":
        _fail("chromium-headless-shell-invalid")
    return str(candidate)


def build_systemd_scope_command(
    *,
    systemd_run: str,
    python: str,
    script: str,
    repo_root: str,
    venv: str,
    chromium_headless_shell: str,
    docker_binary: str,
    run_id: str,
) -> list[str]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        _fail("run-id-invalid")
    unit = f"propertyquarry-postgres-browser-{run_id}"
    return [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--property=MemoryMax={HOST_MEMORY_MAX_BYTES}",
        f"--property=MemorySwapMax={HOST_SWAP_MAX_BYTES}",
        f"--property=TasksMax={HOST_TASKS_MAX}",
        "--property=CPUQuota=100%",
        f"--property=RuntimeMaxSec={HOST_RUNTIME_MAX_SECONDS}s",
        "--",
        python,
        script,
        "--inside-systemd-scope",
        "--run-id",
        run_id,
        "--repo-root",
        repo_root,
        "--venv",
        venv,
        "--chromium-headless-shell",
        chromium_headless_shell,
        "--docker-binary",
        docker_binary,
        "--systemd-run",
        systemd_run,
    ]


def _bounded_integer(path: Path, code: str) -> int:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        _fail(code)
    if not raw.isdigit():
        _fail(code)
    return int(raw)


def require_cgroup_limits(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, float | int]:
    try:
        lines = proc_cgroup.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        _fail("host-cgroup-unavailable")
    unified = [line.split(":", 2)[2] for line in lines if line.startswith("0::")]
    if len(unified) != 1:
        _fail("host-cgroup-unavailable")
    relative = Path(unified[0].lstrip("/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        _fail("host-cgroup-unavailable")
    scope = cgroup_root / relative
    memory = _bounded_integer(scope / "memory.max", "host-memory-uncapped")
    swap = _bounded_integer(scope / "memory.swap.max", "host-swap-uncapped")
    tasks = _bounded_integer(scope / "pids.max", "host-tasks-uncapped")
    try:
        cpu_fields = (scope / "cpu.max").read_text(encoding="ascii").split()
    except (OSError, UnicodeError):
        _fail("host-cpu-uncapped")
    if len(cpu_fields) != 2 or not all(value.isdigit() for value in cpu_fields):
        _fail("host-cpu-uncapped")
    quota, period = (int(value) for value in cpu_fields)
    if period <= 0:
        _fail("host-cpu-uncapped")
    cpu_ratio = quota / period
    if (
        memory > HOST_MEMORY_MAX_BYTES
        or swap > HOST_SWAP_MAX_BYTES
        or tasks > HOST_TASKS_MAX
        or not math.isfinite(cpu_ratio)
        or cpu_ratio > HOST_CPU_MAX_RATIO
    ):
        _fail("host-cgroup-limits-too-loose")
    return {
        "memory_max_bytes": memory,
        "memory_swap_max_bytes": swap,
        "tasks_max": tasks,
        "cpu_quota_percent": cpu_ratio * 100.0,
    }


def _docker_base(docker_binary: str) -> list[str]:
    return [docker_binary, "--host", DOCKER_HOST]


def _label(run_id: str) -> str:
    return f"{RUN_LABEL_KEY}={run_id}"


def build_collision_preflight_commands(
    *, docker_binary: str, names: ResourceNames, run_id: str
) -> list[list[str]]:
    base = _docker_base(docker_binary)
    label = _label(run_id)
    return [
        base
        + [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"name=^/{names.container}$",
        ],
        base
        + ["container", "ls", "--all", "--quiet", "--filter", f"label={label}"],
        base + ["network", "ls", "--quiet", "--filter", f"name=^{names.network}$"],
        base + ["network", "ls", "--quiet", "--filter", f"label={label}"],
        base + ["volume", "ls", "--quiet", "--filter", f"name=^{names.volume}$"],
        base + ["volume", "ls", "--quiet", "--filter", f"label={label}"],
    ]


def build_postgres_run_command(
    *,
    docker_binary: str,
    names: ResourceNames,
    run_id: str,
    image_id: str,
    db_env_file: str,
) -> list[str]:
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        _fail("postgres-image-id-invalid")
    return _docker_base(docker_binary) + [
        "run",
        "--detach",
        "--name",
        names.container,
        "--label",
        _label(run_id),
        "--network",
        names.network,
        "--mount",
        f"type=volume,source={names.volume},target=/var/lib/postgresql/data",
        "--cpus",
        POSTGRES_CPUS,
        "--memory",
        POSTGRES_MEMORY,
        "--memory-swap",
        POSTGRES_MEMORY,
        "--pids-limit",
        POSTGRES_PIDS,
        "--restart",
        "no",
        "--health-cmd",
        "pg_isready -U postgres -d postgres",
        "--health-interval",
        "2s",
        "--health-timeout",
        "2s",
        "--health-start-period",
        "2s",
        "--health-retries",
        "30",
        "--env-file",
        db_env_file,
        "--pull",
        "never",
        image_id,
    ]


def build_volume_create_command(
    *, docker_binary: str, names: ResourceNames, run_id: str
) -> list[str]:
    return _docker_base(docker_binary) + [
        "volume",
        "create",
        "--driver",
        "local",
        "--label",
        _label(run_id),
        "--opt",
        "type=tmpfs",
        "--opt",
        "device=tmpfs",
        "--opt",
        (
            f"o=size={POSTGRES_VOLUME_MAX_BYTES},mode=0700,"
            "nosuid,nodev,noexec"
        ),
        names.volume,
    ]


def build_container_address_inspect_command(
    *, docker_binary: str, names: ResourceNames
) -> list[str]:
    return _docker_base(docker_binary) + [
        "container",
        "inspect",
        "--format",
        (
            f'{{{{with index .NetworkSettings.Networks "{names.network}"}}}}'
            "{{.IPAddress}} {{.NetworkID}}{{end}}"
        ),
        names.container,
    ]


def build_admission_capacity_owner_commands(
    *,
    docker_binary: str,
    names: ResourceNames,
    role_name: str,
) -> tuple[list[str], list[str]]:
    """Build fixed-argv role bootstrap and verification commands for the disposable DB."""
    if ADMISSION_CAPACITY_OWNER_ROLE_RE.fullmatch(role_name) is None:
        _fail("admission-capacity-owner-role-invalid")
    quoted_role = f'"{role_name}"'
    role_literal = role_name
    psql = [
        "exec",
        "--user",
        "postgres",
        names.container,
        "/usr/local/bin/psql",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--host=/var/run/postgresql",
        "--dbname=postgres",
        "--username=postgres",
        "--no-align",
        "--tuples-only",
    ]
    create_sql = (
        f"CREATE ROLE {quoted_role} WITH "
        "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS"
    )
    verify_sql = (
        "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, "
        "rolcreatedb, rolreplication, rolbypassrls, "
        "(SELECT COUNT(*) FROM pg_catalog.pg_auth_members AS membership "
        "WHERE membership.member = owner_role.oid) "
        "FROM pg_catalog.pg_roles AS owner_role "
        f"WHERE owner_role.rolname = '{role_literal}'"
    )
    base = _docker_base(docker_binary)
    return (
        base + psql + ["--command", create_sql],
        base + psql + ["--command", verify_sql],
    )


def _write_env_file(path: Path, values: Mapping[str, str]) -> None:
    if not path.is_absolute() or path.exists():
        _fail("temporary-env-invalid")
    for key, value in values.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or any(
            marker in value for marker in ("\n", "\r", "\x00")
        ):
            _fail("temporary-env-invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key in sorted(values):
                handle.write(f"{key}={values[key]}\n")
    except OSError:
        _fail("temporary-env-invalid")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        _fail("temporary-env-mode-invalid")


def _read_env_file(path: Path) -> dict[str, str]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        _fail("temporary-env-mode-invalid")
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
                _fail("temporary-env-invalid")
            values[key] = value
    except OSError:
        _fail("temporary-env-invalid")
    return values


def _open_private_log(path: Path) -> int:
    if not path.is_absolute() or not path.parent.is_dir() or path.exists():
        _fail("private-log-invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
    except OSError:
        _fail("private-log-invalid")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        _fail("private-log-invalid")
    return descriptor


class _RunStorageGuard:
    """Bound every filesystem entry below one private run root.

    Only explicitly registered host producers may be terminated. Docker CLI
    inventory and cleanup subprocesses are deliberately never registered.
    """

    def __init__(
        self,
        temp_root: Path,
        *,
        maximum_bytes: int = RUN_TEMP_MAX_BYTES,
        maximum_entries: int = RUN_TEMP_MAX_ENTRIES,
        poll_seconds: float = RUN_STORAGE_POLL_SECONDS,
    ) -> None:
        try:
            root_metadata = temp_root.lstat()
        except OSError:
            _fail("run-storage-limit-exceeded")
        if (
            not temp_root.is_absolute()
            or not stat.S_ISDIR(root_metadata.st_mode)
            or temp_root.is_symlink()
            or root_metadata.st_uid != os.getuid()
            or maximum_bytes <= 0
            or maximum_entries <= 0
            or poll_seconds <= 0
        ):
            _fail("run-storage-limit-exceeded")
        self._temp_root = temp_root
        self._maximum_bytes = maximum_bytes
        self._maximum_entries = maximum_entries
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._lock = threading.Lock()
        self._producers: dict[subprocess.Popen[bytes], int | None] = {}
        self._thread: threading.Thread | None = None

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes], process_group: int | None, signum: int
    ) -> None:
        if process_group is not None:
            try:
                os.killpg(process_group, signum)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            return
        try:
            if process.poll() is None:
                if signum == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()
        except (AttributeError, OSError):
            pass

    @classmethod
    def _wait_process_groups(
        cls,
        producers: Sequence[tuple[subprocess.Popen[bytes], int | None]],
        timeout_seconds: int,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            survivors = tuple(
                entry
                for entry in producers
                if cls._process_group_exists(entry[0], entry[1])
            )
            if not survivors:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    @staticmethod
    def _process_group_exists(
        process: subprocess.Popen[bytes], process_group: int | None
    ) -> bool:
        try:
            leader_running = process.poll() is None
        except (AttributeError, OSError):
            leader_running = False
        if process_group is None:
            return leader_running
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _terminate_processes(
        self,
        producers: Sequence[tuple[subprocess.Popen[bytes], int | None]],
        *,
        term_timeout_seconds: int = PRODUCER_TERM_TIMEOUT_SECONDS,
        kill_timeout_seconds: int = PRODUCER_KILL_TIMEOUT_SECONDS,
    ) -> bool:
        for process, process_group in producers:
            self._signal_process_group(process, process_group, signal.SIGTERM)
        self._wait_process_groups(producers, term_timeout_seconds)
        survivors = tuple(
            entry
            for entry in producers
            if self._process_group_exists(entry[0], entry[1])
        )
        for process, process_group in survivors:
            self._signal_process_group(process, process_group, signal.SIGKILL)
        return self._wait_process_groups(survivors, kill_timeout_seconds)

    def _terminate_registered(self) -> None:
        with self._lock:
            producers = tuple(self._producers.items())
        if not self._terminate_processes(producers):
            self._failed.set()

    def _trip(self) -> None:
        if not self._failed.is_set():
            self._failed.set()
            self._terminate_registered()

    @staticmethod
    def _allocated_bytes(metadata: os.stat_result) -> int:
        allocated = max(0, int(getattr(metadata, "st_blocks", 0)))
        return max(0, metadata.st_size, allocated * FILESYSTEM_BLOCK_BYTES)

    def _scan(self) -> bool:
        try:
            root_metadata = self._temp_root.lstat()
        except OSError:
            self._trip()
            return False
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or self._temp_root.is_symlink()
        ):
            self._trip()
            return False
        total = self._allocated_bytes(root_metadata)
        entry_count = 1
        stack = [self._temp_root]
        while stack:
            directory = stack.pop()
            try:
                directory_metadata = directory.lstat()
            except FileNotFoundError:
                # Concurrent deletion cannot increase retained run storage.
                continue
            except OSError:
                self._trip()
                return False
            if not stat.S_ISDIR(directory_metadata.st_mode) or directory.is_symlink():
                self._trip()
                return False
            if (
                total > self._maximum_bytes
                or entry_count > self._maximum_entries
            ):
                self._trip()
                return False
            try:
                with os.scandir(directory) as entries:
                    for item in entries:
                        try:
                            metadata = item.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except OSError:
                            self._trip()
                            return False
                        entry_count += 1
                        total += self._allocated_bytes(metadata)
                        if (
                            entry_count > self._maximum_entries
                            or total > self._maximum_bytes
                        ):
                            self._trip()
                            return False
                        # A symlink-to-directory is counted but never traversed.
                        if stat.S_ISDIR(metadata.st_mode) and not item.is_symlink():
                            stack.append(Path(item.path))
            except FileNotFoundError:
                continue
            except OSError:
                self._trip()
                return False
        return True

    def _watch(self) -> None:
        while True:
            stopping = self._stop.wait(self._poll_seconds)
            if not self._scan():
                return
            if stopping:
                return

    def start(self) -> None:
        if self._thread is not None:
            _fail("run-storage-limit-exceeded")
        if not self._scan():
            _fail("run-storage-limit-exceeded")
        thread = threading.Thread(
            target=self._watch,
            name="pq-postgres-run-storage-guard",
            daemon=True,
        )
        self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            self._trip()
            _fail("run-storage-limit-exceeded")

    def register(self, process: subprocess.Popen[bytes]) -> None:
        process_group: int | None = None
        if isinstance(process, subprocess.Popen):
            try:
                process_group = os.getpgid(process.pid)
            except OSError:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    _fail("producer-process-group-invalid")
            if process_group is not None and process_group != process.pid:
                try:
                    process.kill()
                except OSError:
                    pass
                _fail("producer-process-group-invalid")
        with self._lock:
            self._producers[process] = process_group
            failed = self._failed.is_set()
        if failed:
            self.terminate(process)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._producers.pop(process, None)

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        term_timeout_seconds: int = PRODUCER_TERM_TIMEOUT_SECONDS,
        kill_timeout_seconds: int = PRODUCER_KILL_TIMEOUT_SECONDS,
    ) -> None:
        with self._lock:
            process_group = self._producers.get(process)
        if not self._terminate_processes(
            ((process, process_group),),
            term_timeout_seconds=term_timeout_seconds,
            kill_timeout_seconds=kill_timeout_seconds,
        ):
            self._failed.set()

    def assert_within_limit(self) -> None:
        if self._failed.is_set() or not self._scan() or self._failed.is_set():
            _fail("run-storage-limit-exceeded")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(STORAGE_GUARD_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                self._failed.set()
        if self._failed.is_set():
            _fail("run-storage-limit-exceeded")


def _producer_command(
    command: Sequence[str], *, maximum_bytes: int | None = None
) -> list[str]:
    _require_absolute_executable(PRLIMIT_BINARY, "producer-file-limit-unavailable")
    if maximum_bytes is None:
        maximum_bytes = PRODUCER_FILE_MAX_BYTES
    if maximum_bytes not in {
        PRODUCER_FILE_MAX_BYTES,
        BROWSER_PRODUCER_FILE_MAX_BYTES,
    }:
        _fail("producer-file-limit-unavailable")
    return [
        PRLIMIT_BINARY,
        f"--fsize={maximum_bytes}:{maximum_bytes}",
        "--",
        *command,
    ]


def _producer_log_within_limit(
    path: Path, *, phase: str, reject_at_limit: bool = True
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{phase}-log-invalid")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(f"{phase}-log-invalid")
    if metadata.st_size > PRODUCER_LOG_MAX_BYTES or (
        reject_at_limit and metadata.st_size == PRODUCER_LOG_MAX_BYTES
    ):
        _fail("producer-log-limit-exceeded")


def _producer_file_limit_reached(returncode: int | None) -> bool:
    return returncode == -signal.SIGXFSZ


def _scoped_failure_code(path: Path) -> str | None:
    try:
        before = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or path.is_symlink()
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size <= 0
        or before.st_size > MAX_SCOPED_DIAGNOSTIC_BYTES
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        payload = os.read(descriptor, MAX_SCOPED_DIAGNOSTIC_BYTES + 1)
        after = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    identity = lambda value: (  # noqa: E731 - closed metadata tuple
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        return None
    match = SCOPED_FAILURE_LINE_RE.fullmatch(payload)
    if match is None:
        return None
    try:
        code = match.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None
    return code if code in SAFE_SCOPED_FAILURE_CODES else None


def _private_session_bytes(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _fail("bootstrap-session-invalid")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size <= 0
        or before.st_size > MAX_SESSION_BYTES
    ):
        _fail("bootstrap-session-invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_uid,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            _fail("bootstrap-session-invalid")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                _fail("bootstrap-session-invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("bootstrap-session-invalid")
        after = os.fstat(descriptor)
    except OSError:
        _fail("bootstrap-session-invalid")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_uid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_nlink,
        opened.st_uid,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ):
        _fail("bootstrap-session-invalid")
    data = b"".join(chunks)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("bootstrap-session-invalid")
    if (
        type(document) is not dict
        or document.get("contract_name")
        != "propertyquarry.postgres_browser_internal_session"
        or document.get("status") != "pass"
        or not str(document.get("access_token") or "").strip()
    ):
        _fail("bootstrap-session-invalid")
    return data


def _write_private_bytes(path: Path, data: bytes) -> None:
    try:
        descriptor = _open_private_log(path)
    except IsolatedPostgresError:
        _fail("bootstrap-session-copy-failed")
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("bootstrap-session-copy-failed")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        _fail("bootstrap-session-copy-failed")
    finally:
        os.close(descriptor)


def _run_output(
    command: Sequence[str],
    *,
    phase: str,
    environment: Mapping[str, str],
    accepted: frozenset[int] = frozenset({0}),
    timeout_seconds: int = 60,
) -> bytes:
    if phase not in COMMAND_PHASES:
        _fail("diagnostic-phase-invalid")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        _fail("command-timeout-invalid")
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            cwd="/",
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _fail(f"{phase}-timeout")
    except OSError:
        _fail(f"{phase}-execution-failed")
    if completed.returncode not in accepted:
        _fail(f"{phase}-exit-nonzero")
    if completed.stderr:
        _fail(f"{phase}-stderr-not-empty")
    if len(completed.stdout) > 65_536:
        _fail(f"{phase}-stdout-too-large")
    return completed.stdout


def _ascii_phase_output(payload: bytes, failure_code: str) -> str:
    if failure_code not in PHASE_SEMANTIC_FAILURE_CODES:
        _fail("diagnostic-phase-invalid")
    try:
        return payload.decode("ascii").strip()
    except UnicodeDecodeError:
        _fail(failure_code)


def _pump_browser_log(process: subprocess.Popen[bytes], descriptor: int) -> None:
    stream = process.stdout
    if stream is None:
        _fail("browser-test-log-invalid")
    observed = 0
    try:
        while True:
            chunk = os.read(stream.fileno(), 64 * 1024)
            if not chunk:
                break
            allowed = min(len(chunk), PRODUCER_LOG_MAX_BYTES - observed)
            view = memoryview(chunk)[:allowed]
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("browser-test-log-invalid")
                observed += written
                view = view[written:]
            if allowed != len(chunk):
                _fail("producer-log-limit-exceeded")
        os.fsync(descriptor)
    except OSError:
        _fail("browser-test-log-invalid")
    finally:
        stream.close()


def _run_host(
    command: Sequence[str],
    *,
    phase: str,
    repo_root: Path,
    environment: Mapping[str, str],
    log_path: Path,
    storage_guard: _RunStorageGuard,
) -> None:
    if phase not in COMMAND_PHASES:
        _fail("diagnostic-phase-invalid")
    try:
        descriptor = _open_private_log(log_path)
    except IsolatedPostgresError:
        _fail(f"{phase}-log-invalid")
    browser_phase = phase == "browser-test"
    file_limit = (
        BROWSER_PRODUCER_FILE_MAX_BYTES
        if browser_phase
        else PRODUCER_FILE_MAX_BYTES
    )
    try:
        process = subprocess.Popen(
            _producer_command(command, maximum_bytes=file_limit),
            cwd=repo_root,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if browser_phase else descriptor,
            stderr=subprocess.STDOUT if browser_phase else descriptor,
            start_new_session=True,
        )
    except OSError:
        os.close(descriptor)
        _fail(f"{phase}-execution-failed")
    storage_guard.register(process)
    try:
        try:
            if browser_phase:
                _pump_browser_log(process, descriptor)
            returncode = process.wait()
        except OSError:
            _fail(f"{phase}-execution-failed")
        storage_guard.assert_within_limit()
        _producer_log_within_limit(
            log_path,
            phase=phase,
            reject_at_limit=not browser_phase,
        )
        if _producer_file_limit_reached(returncode):
            _fail("producer-file-limit-exceeded")
        if returncode != 0:
            _fail(f"{phase}-exit-nonzero")
    except BaseException:
        # This also handles the lifecycle signal/watchdog exception raised while
        # waiting: the whole isolated producer session is stopped before it is
        # removed from the guard registry.
        storage_guard.terminate(process)
        raise
    finally:
        storage_guard.unregister(process)
        os.close(descriptor)


def _docker_environment(temp_root: Path) -> dict[str, str]:
    docker_config = temp_root / "docker-config"
    docker_config.mkdir(mode=0o700)
    return {
        "DOCKER_CONFIG": str(docker_config),
        "HOME": str(temp_root),
        "PATH": "/usr/bin:/bin",
    }


def _assert_no_collision(
    commands: Sequence[Sequence[str]],
    environment: Mapping[str, str],
    *,
    phase_prefix: str,
    timeout_seconds: int = 60,
) -> None:
    if len(commands) != len(COLLISION_PHASE_SUFFIXES):
        _fail("diagnostic-phase-invalid")
    for command, suffix in zip(commands, COLLISION_PHASE_SUFFIXES, strict=True):
        if _run_output(
            command,
            phase=f"{phase_prefix}-{suffix}",
            environment=environment,
            timeout_seconds=timeout_seconds,
        ).strip():
            _fail(f"{phase_prefix}-{suffix}-collision")


def _inspect_local_image(docker_binary: str, environment: Mapping[str, str]) -> str:
    output = _run_output(
        _docker_base(docker_binary)
        + ["image", "inspect", "--format", "{{.Id}}", POSTGRES_IMAGE],
        phase="docker-image-inspect",
        environment=environment,
    )
    image_id = _ascii_phase_output(output, "docker-image-inspect-output-invalid")
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        _fail("docker-image-inspect-output-invalid")
    return image_id


def _create_resources(
    *,
    docker_binary: str,
    names: ResourceNames,
    run_id: str,
    image_id: str,
    db_env_file: Path,
    environment: Mapping[str, str],
    created: set[str],
) -> str:
    base = _docker_base(docker_binary)
    label = _label(run_id)
    created.add("network")
    network = _ascii_phase_output(
        _run_output(
            base
            + [
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--label",
                label,
                names.network,
            ],
            phase="docker-network-create",
            environment=environment,
        ),
        "docker-network-create-output-invalid",
    )
    if NETWORK_ID_RE.fullmatch(network) is None:
        _fail("docker-network-create-output-invalid")
    observed_network_label = _ascii_phase_output(
        _run_output(
            _resource_label_command(docker_binary, "network", names.network),
            phase="docker-network-label",
            environment=environment,
        ),
        "docker-network-label-mismatch",
    )
    if observed_network_label != run_id:
        _fail("docker-network-label-mismatch")
    created.add("volume")
    volume = _ascii_phase_output(
        _run_output(
            build_volume_create_command(
                docker_binary=docker_binary, names=names, run_id=run_id
            ),
            phase="docker-volume-create",
            environment=environment,
        ),
        "docker-volume-create-output-invalid",
    )
    if volume != names.volume:
        _fail("docker-volume-create-output-invalid")
    observed_volume_label = _ascii_phase_output(
        _run_output(
            _resource_label_command(docker_binary, "volume", names.volume),
            phase="docker-volume-label",
            environment=environment,
        ),
        "docker-volume-label-mismatch",
    )
    if observed_volume_label != run_id:
        _fail("docker-volume-label-mismatch")
    created.add("container")
    container = _ascii_phase_output(
        _run_output(
            build_postgres_run_command(
                docker_binary=docker_binary,
                names=names,
                run_id=run_id,
                image_id=image_id,
                db_env_file=str(db_env_file),
            ),
            phase="docker-container-create",
            environment=environment,
        ),
        "docker-container-create-output-invalid",
    )
    if CONTAINER_ID_RE.fullmatch(container) is None:
        _fail("docker-container-create-output-invalid")
    observed_container_label = _ascii_phase_output(
        _run_output(
            _resource_label_command(docker_binary, "container", names.container),
            phase="docker-container-label",
            environment=environment,
        ),
        "docker-container-label-mismatch",
    )
    if observed_container_label != run_id:
        _fail("docker-container-label-mismatch")
    return network


def _wait_for_postgres(
    *,
    docker_binary: str,
    names: ResourceNames,
    expected_network_id: str,
    environment: Mapping[str, str],
) -> str:
    if NETWORK_ID_RE.fullmatch(expected_network_id) is None:
        _fail("docker-address-output-invalid")
    base = _docker_base(docker_binary)
    state_command = base + [
        "container",
        "inspect",
        "--format",
        "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
        names.container,
    ]
    for attempt in range(MAX_READY_ATTEMPTS):
        state = _ascii_phase_output(
            _run_output(
                state_command,
                phase="docker-health-inspect",
                environment=environment,
            ),
            "docker-health-output-invalid",
        )
        if state == "running healthy":
            break
        if state.startswith("exited ") or state.startswith("dead "):
            _fail("docker-health-container-exited")
        if attempt + 1 < MAX_READY_ATTEMPTS:
            time.sleep(1)
    else:
        _fail("docker-health-timeout")
    attachment = _run_output(
        build_container_address_inspect_command(
            docker_binary=docker_binary, names=names
        ),
        phase="docker-address-inspect",
        environment=environment,
    )
    text = _ascii_phase_output(attachment, "docker-address-output-invalid")
    fields = text.split()
    if len(fields) != 2 or fields[1] != expected_network_id:
        _fail("docker-address-output-invalid")
    return _private_container_ipv4(fields[0])


def _provision_admission_capacity_owner_role(
    *,
    docker_binary: str,
    names: ResourceNames,
    role_name: str,
    environment: Mapping[str, str],
) -> None:
    """Provision the migration prerequisite only inside the disposable cluster."""
    create_command, verify_command = build_admission_capacity_owner_commands(
        docker_binary=docker_binary,
        names=names,
        role_name=role_name,
    )
    create_output = _ascii_phase_output(
        _run_output(
            create_command,
            phase="docker-role-bootstrap",
            environment=environment,
        ),
        "docker-role-bootstrap-output-invalid",
    )
    if create_output != "CREATE ROLE":
        _fail("docker-role-bootstrap-output-invalid")
    verification = _ascii_phase_output(
        _run_output(
            verify_command,
            phase="docker-role-verify",
            environment=environment,
        ),
        "docker-role-verification-mismatch",
    )
    if verification != "f|f|f|f|f|f|f|0":
        _fail("docker-role-verification-mismatch")


def _validate_disposable_admission_dsns(
    *,
    admin_database_url: str,
    admission_database_url: str,
    admission_password: str,
) -> None:
    raw_urls = (admin_database_url, admission_database_url)
    if (
        DISPOSABLE_DATABASE_PASSWORD_RE.fullmatch(admission_password) is None
        or any(not value.isascii() for value in raw_urls)
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F
            for value in raw_urls
            for character in value
        )
    ):
        _fail("api-admission-role-dsn-invalid")
    try:
        from psycopg.conninfo import conninfo_to_dict

        admin = urllib.parse.urlsplit(admin_database_url)
        admission = urllib.parse.urlsplit(admission_database_url)
        admin_port = admin.port
        admission_port = admission.port
        admin_password = str(admin.password or "")
        parsed_admin = conninfo_to_dict(admin_database_url)
        parsed_admission = conninfo_to_dict(admission_database_url)
    except Exception:
        _fail("api-admission-role-dsn-invalid")
    expected_admin = (
        f"postgresql://postgres:{admin_password}@127.0.0.1:{admin_port}/postgres"
    )
    expected_admission = (
        "postgresql://propertyquarry_api_admission:"
        f"{admission_password}@127.0.0.1:{admission_port}/postgres"
    )
    if (
        admin.scheme != "postgresql"
        or admission.scheme != "postgresql"
        or admin.hostname != "127.0.0.1"
        or admission.hostname != "127.0.0.1"
        or admin_port is None
        or admission_port != admin_port
        or admin.username != "postgres"
        or admission.username != DISPOSABLE_API_ADMISSION_ROLE
        or DISPOSABLE_DATABASE_PASSWORD_RE.fullmatch(admin_password) is None
        or admission.password != admission_password
        or admin.path != "/postgres"
        or admission.path != "/postgres"
        or admin.query
        or admission.query
        or admin.fragment
        or admission.fragment
        or admin_database_url == admission_database_url
        or admin_database_url != expected_admin
        or admission_database_url != expected_admission
        or parsed_admin
        != {
            "user": "postgres",
            "password": admin_password,
            "dbname": "postgres",
            "host": "127.0.0.1",
            "port": str(admin_port),
        }
        or parsed_admission
        != {
            "user": DISPOSABLE_API_ADMISSION_ROLE,
            "password": admission_password,
            "dbname": "postgres",
            "host": "127.0.0.1",
            "port": str(admission_port),
        }
    ):
        _fail("api-admission-role-dsn-invalid")


def _clear_libpq_environment(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Remove every present and future libpq-style PG* environment override."""
    target = environ if environ is not None else os.environ
    keys = tuple(sorted(key for key in target if key.startswith("PG")))
    for key in keys:
        target.pop(key, None)
    return keys


def _require_closed_libpq_environment(
    environ: Mapping[str, str] | None = None,
) -> None:
    target = environ if environ is not None else os.environ
    if any(key.startswith("PG") for key in target):
        _fail("libpq-environment-not-closed")


def _postgres_scram_verifier(
    password: str,
    *,
    salt: bytes | None = None,
) -> str:
    """Derive a PostgreSQL SCRAM verifier without putting cleartext in SQL."""
    if DISPOSABLE_DATABASE_PASSWORD_RE.fullmatch(password) is None:
        _fail("api-admission-role-dsn-invalid")
    chosen_salt = salt if salt is not None else secrets.token_bytes(
        POSTGRES_SCRAM_SALT_BYTES
    )
    if type(chosen_salt) is not bytes or len(chosen_salt) != POSTGRES_SCRAM_SALT_BYTES:
        _fail("api-admission-role-provision-failed")
    salted_password = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("ascii"),
        chosen_salt,
        POSTGRES_SCRAM_ITERATIONS,
    )
    client_key = hmac.digest(salted_password, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted_password, b"Server Key", "sha256")
    encode = lambda value: base64.b64encode(value).decode("ascii")
    verifier = (
        f"SCRAM-SHA-256${POSTGRES_SCRAM_ITERATIONS}:"
        f"{encode(chosen_salt)}${encode(stored_key)}:{encode(server_key)}"
    )
    if POSTGRES_SCRAM_VERIFIER_RE.fullmatch(verifier) is None:
        _fail("api-admission-role-provision-failed")
    return verifier


def _provision_api_admission_role(
    *,
    admin_database_url: str,
    admission_database_url: str,
    admission_password: str,
    connect: Callable[..., object] | None = None,
) -> None:
    """Install the exact API admission login in the disposable database only."""
    _validate_disposable_admission_dsns(
        admin_database_url=admin_database_url,
        admission_database_url=admission_database_url,
        admission_password=admission_password,
    )
    _require_closed_libpq_environment()
    admission_verifier = _postgres_scram_verifier(admission_password)
    try:
        import psycopg
        from psycopg import sql

        connector = connect or psycopg.connect
        with connector(
            admin_database_url,
            autocommit=False,
            connect_timeout=5,
            hostaddr="127.0.0.1",
            sslmode="disable",
            options="",
            application_name="propertyquarry-isolated-admission-provision",
            target_session_attrs="read-write",
        ) as connection:
            with connection.cursor() as cursor:  # type: ignore[attr-defined]
                cursor.execute(
                    """
                    SELECT current_database(), current_user,
                           role.rolcanlogin, role.rolsuper
                    FROM pg_catalog.pg_roles AS role
                    WHERE role.rolname = current_user
                    """
                )
                if cursor.fetchone() != ("postgres", "postgres", True, True):
                    _fail("api-admission-role-provision-failed")
                cursor.execute("SET LOCAL log_statement = 'none'")
                cursor.execute("SET LOCAL log_min_error_statement = 'panic'")
                cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (DISPOSABLE_API_ADMISSION_ROLE,),
                )
                if cursor.fetchone() is not None:
                    _fail("api-admission-role-collision")
                cursor.execute(
                    sql.SQL(
                        'CREATE ROLE "propertyquarry_api_admission" WITH '
                        "LOGIN PASSWORD {} NOINHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Literal(admission_verifier))
                )
                cursor.execute(
                    """
                    SELECT namespace.nspname
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE relation.oid IN (
                        'propertyquarry_admission_quota_buckets'::regclass,
                        'propertyquarry_admission_leases'::regclass,
                        'propertyquarry_admission_capacity_state'::regclass
                    )
                    GROUP BY namespace.nspname
                    HAVING COUNT(*) = 3
                    """
                )
                if cursor.fetchall() != [("public",)]:
                    _fail("api-admission-role-provision-failed")
                for statement in (
                    "REVOKE ALL PRIVILEGES ON DATABASE postgres FROM PUBLIC",
                    "GRANT CONNECT ON DATABASE postgres TO propertyquarry_api_admission",
                    "REVOKE CREATE, TEMPORARY ON DATABASE postgres FROM propertyquarry_api_admission",
                    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
                    "GRANT USAGE ON SCHEMA public TO propertyquarry_api_admission",
                    "ALTER ROLE propertyquarry_api_admission IN DATABASE postgres "
                    "SET search_path TO public, pg_catalog",
                    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC",
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC",
                    "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC",
                    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                    "FROM propertyquarry_api_admission",
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public "
                    "FROM propertyquarry_api_admission",
                    "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public "
                    "FROM propertyquarry_api_admission",
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                    "propertyquarry_admission_quota_buckets, "
                    "propertyquarry_admission_leases TO propertyquarry_api_admission",
                    "GRANT SELECT ON TABLE propertyquarry_admission_capacity_state "
                    "TO propertyquarry_api_admission",
                    "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
                    "ON TABLE propertyquarry_admission_capacity_state "
                    "FROM propertyquarry_api_admission",
                    "REVOKE EXECUTE ON FUNCTION "
                    "propertyquarry_admission_capacity_after_insert(), "
                    "propertyquarry_admission_capacity_after_delete(), "
                    "propertyquarry_admission_capacity_after_truncate() "
                    "FROM propertyquarry_api_admission",
                ):
                    cursor.execute(statement)
                cursor.execute(
                    """
                    SELECT role.rolcanlogin, role.rolinherit, role.rolsuper,
                           role.rolcreaterole, role.rolcreatedb,
                           role.rolreplication, role.rolbypassrls,
                           (SELECT COUNT(*)
                            FROM pg_catalog.pg_auth_members AS membership
                            WHERE membership.member = role.oid)
                    FROM pg_catalog.pg_roles AS role
                    WHERE role.rolname = %s
                    """,
                    (DISPOSABLE_API_ADMISSION_ROLE,),
                )
                if cursor.fetchone() != (
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    0,
                ):
                    _fail("api-admission-role-provision-failed")
            connection.commit()  # type: ignore[attr-defined]
    except IsolatedPostgresError:
        raise
    except Exception:
        _fail("api-admission-role-provision-failed")


def _verify_api_admission_role(
    *,
    admission_database_url: str,
    connect: Callable[..., object] | None = None,
    probe: Callable[..., None] | None = None,
) -> None:
    _require_closed_libpq_environment()
    try:
        import psycopg
        from app.services.admission_control import probe_admission_cursor

        connector = connect or psycopg.connect
        verifier = probe or probe_admission_cursor
        with connector(
            admission_database_url,
            autocommit=True,
            connect_timeout=5,
            hostaddr="127.0.0.1",
            sslmode="disable",
            options="",
            application_name="propertyquarry-isolated-api-admission-proof",
            target_session_attrs="read-write",
        ) as connection:
            with connection.cursor() as cursor:  # type: ignore[attr-defined]
                verifier(cursor, require_least_privilege=True)
    except IsolatedPostgresError:
        raise
    except Exception:
        _fail("api-admission-role-verification-failed")


def _resource_label_command(
    docker_binary: str, kind: str, name: str
) -> list[str]:
    field = ".Config.Labels" if kind == "container" else ".Labels"
    return _docker_base(docker_binary) + [
        kind,
        "inspect",
        "--format",
        f'{{{{ index {field} "{RUN_LABEL_KEY}" }}}}',
        name,
    ]


def _cleanup_resources(
    *,
    docker_binary: str,
    names: ResourceNames,
    run_id: str,
    created: set[str],
    environment: Mapping[str, str],
) -> None:
    base = _docker_base(docker_binary)
    failures: list[str] = []
    exact_queries = build_collision_preflight_commands(
        docker_binary=docker_binary, names=names, run_id=run_id
    )
    for kind, name, remove, exact_query in (
        (
            "container",
            names.container,
            ["container", "rm", "--force", names.container],
            exact_queries[0],
        ),
        ("network", names.network, ["network", "rm", names.network], exact_queries[2]),
        ("volume", names.volume, ["volume", "rm", names.volume], exact_queries[4]),
    ):
        if kind not in created:
            continue
        try:
            if not _run_output(
                exact_query,
                phase=f"cleanup-{kind}-presence",
                environment=environment,
                timeout_seconds=CLEANUP_COMMAND_TIMEOUT_SECONDS,
            ).strip():
                continue
            try:
                observed = _run_output(
                    _resource_label_command(docker_binary, kind, name),
                    phase=f"cleanup-{kind}-label",
                    environment=environment,
                    timeout_seconds=CLEANUP_COMMAND_TIMEOUT_SECONDS,
                ).decode("ascii").strip()
            except UnicodeDecodeError:
                failures.append(f"cleanup-{kind}-label-mismatch")
                continue
            if observed != run_id:
                failures.append(f"cleanup-{kind}-label-mismatch")
                continue
            _run_output(
                base + remove,
                phase=f"cleanup-{kind}-remove",
                environment=environment,
                timeout_seconds=CLEANUP_COMMAND_TIMEOUT_SECONDS,
            )
        except IsolatedPostgresError as error:
            failures.append(error.code)
    leftovers = build_collision_preflight_commands(
        docker_binary=docker_binary, names=names, run_id=run_id
    )
    try:
        _assert_no_collision(
            leftovers,
            environment,
            phase_prefix="cleanup-inventory",
            timeout_seconds=CLEANUP_COMMAND_TIMEOUT_SECONDS,
        )
    except IsolatedPostgresError as error:
        failures.append(error.code)
    if failures:
        _fail(failures[0])


def _raise_post_cleanup_errors(
    *,
    cleanup_error: IsolatedPostgresError | None,
    dependency_error: IsolatedPostgresError | None,
    storage_error: IsolatedPostgresError | None,
    relay_error: IsolatedPostgresError | None,
) -> None:
    for error in (cleanup_error, dependency_error, storage_error, relay_error):
        if error is not None:
            raise error


def _wait_api(
    base_url: str,
    expected_reason: str,
    process: subprocess.Popen[bytes],
    *,
    log_path: Path,
    storage_guard: _RunStorageGuard,
) -> None:
    for attempt in range(MAX_READY_ATTEMPTS):
        storage_guard.assert_within_limit()
        _producer_log_within_limit(log_path, phase="candidate-api")
        returncode = process.poll()
        if _producer_file_limit_reached(returncode):
            _fail("run-storage-limit-exceeded")
        if returncode is not None:
            _fail("candidate-api-exited")
        try:
            with urllib.request.urlopen(f"{base_url}/health/ready", timeout=3) as response:
                document = json.loads(response.read(65_536).decode("utf-8"))
            if isinstance(document, dict) and document.get("reason") == expected_reason:
                return
        except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError):
            pass
        if attempt + 1 < MAX_READY_ATTEMPTS:
            time.sleep(1)
    _fail("candidate-api-readiness-timeout")


def _start_api(
    *,
    python: str,
    repo_root: Path,
    environment: Mapping[str, str],
    log_path: Path,
    storage_guard: _RunStorageGuard,
) -> tuple[subprocess.Popen[bytes], int]:
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.set_inheritable(True)
        port = int(listener.getsockname()[1])
    except OSError:
        if "listener" in locals():
            listener.close()
        _fail("candidate-api-start-failed")
    try:
        descriptor = _open_private_log(log_path)
    except IsolatedPostgresError:
        listener.close()
        _fail("candidate-api-log-invalid")
    try:
        process = subprocess.Popen(
            _producer_command(
                [python, str(Path(__file__).resolve()), "--serve-fd", str(listener.fileno())]
            ),
            cwd=repo_root,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.STDOUT,
            pass_fds=(listener.fileno(),),
            start_new_session=True,
        )
    except OSError:
        os.close(descriptor)
        listener.close()
        _fail("candidate-api-start-failed")
    os.close(descriptor)
    listener.close()
    storage_guard.register(process)
    return process, port


def _stop_api(
    process: subprocess.Popen[bytes] | None,
    storage_guard: _RunStorageGuard | None = None,
) -> None:
    if process is None:
        return
    if storage_guard is not None:
        try:
            storage_guard.terminate(
                process,
                term_timeout_seconds=API_TERM_TIMEOUT_SECONDS,
                kill_timeout_seconds=API_KILL_TIMEOUT_SECONDS,
            )
        finally:
            storage_guard.unregister(process)
        return
    if process.poll() is not None:
        return
    try:
        try:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=API_TERM_TIMEOUT_SECONDS)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=API_KILL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            # The enclosing systemd scope remains the final child-process reaper;
            # Docker cleanup must still run even if the API refuses termination.
            pass
    finally:
        if storage_guard is not None:
            storage_guard.unregister(process)


def _runtime_environment(
    *,
    repo_root: Path,
    temp_root: Path,
    database_url: str,
    admission_database_url: str,
    api_token: str,
    signing_secret: str,
    erasure_secret: str,
    chromium_headless_shell: str,
    dependency_overlay_base: Path,
) -> dict[str, str]:
    dependency_overlay_site = (
        dependency_overlay_base
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    paths = {
        "artifacts": temp_root / "artifacts",
        "public-tours": temp_root / "public-tours",
        "incoming-tours": temp_root / "incoming-tours",
        "provider-ledger": temp_root / "provider-ledger",
        "writer-heartbeats": temp_root / "writer-heartbeats",
        "home": temp_root / "home",
        "subscribr": temp_root / "subscribr",
    }
    for path in paths.values():
        path.mkdir(mode=0o700)
    values = {
        "DATABASE_URL": database_url,
        "EA_ALLOW_LOOPBACK_NO_AUTH": "0",
        "EA_API_HEARTBEAT_PATH": str(paths["artifacts"] / "api-heartbeat.json"),
        "EA_API_TOKEN": api_token,
        "EA_ARTIFACTS_DIR": str(paths["artifacts"]),
        "EA_HOST": "127.0.0.1",
        "EA_LOG_LEVEL": "WARNING",
        "EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR": str(paths["writer-heartbeats"]),
        "EA_PUBLIC_TOUR_DIR": str(paths["public-tours"]),
        "EA_RESPONSES_PROVIDER_LEDGER_DIR": str(paths["provider-ledger"]),
        "EA_ROLE": "api",
        "EA_RUNTIME_MODE": "prod",
        "EA_SIGNING_SECRET": signing_secret,
        "EA_STORAGE_BACKEND": "postgres",
        "EMAILIT_API_KEY": "",
        "HOME": str(paths["home"]),
        "PATH": "/usr/bin:/bin",
        "PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES": "0",
        "PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS": "0",
        "PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES": "0",
        "PROPERTYQUARRY_ENABLE_PUBLIC_TOURS": "0",
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": admission_database_url,
        "PROPERTYQUARRY_POSTGRES_BROWSER_E2E": "1",
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": erasure_secret,
        CHROMIUM_EXECUTABLE_ENV: chromium_headless_shell,
        "PROPERTYQUARRY_REPO_ROOT": str(repo_root),
        "PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR": str(paths["subscribr"]),
        "PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR": str(paths["incoming-tours"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Candidate source stays first, followed by the private dependency
        # snapshot.  The latter must precede the complete venv so the packages
        # we hash before and after the run are the packages Python executes.
        "PYTHONPATH": os.pathsep.join(
            (str(repo_root / "ea"), str(dependency_overlay_site))
        ),
        "PYTHONUSERBASE": str(dependency_overlay_base),
        "TMPDIR": str(temp_root),
    }
    return values


def _execute_guarded(
    *,
    repo_root: Path,
    python: str,
    docker_binary: str,
    run_id: str,
    chromium_headless_shell: str,
    lifecycle: _LifecycleGuard,
) -> None:
    names = ResourceNames(run_id)
    if any(RESOURCE_NAME_RE.fullmatch(name) is None for name in names):
        _fail("disposable-resource-name-invalid")
    created: set[str] = set()
    api_process: subprocess.Popen[bytes] | None = None
    database_relay: _LoopbackDatabaseRelay | None = None
    storage_guard: _RunStorageGuard | None = None
    dependency_snapshot: _DependencySnapshot | None = None
    dependency_overlay_site: Path | None = None
    session_path = Path(f"/tmp/propertyquarry-postgres-browser-session-{run_id}.json")
    if os.path.lexists(session_path):
        _fail("temporary-session-collision")
    with tempfile.TemporaryDirectory(prefix=f"pq-pg-e2e-{run_id}-") as temp_text:
        temp_root = Path(temp_text)
        temp_root.chmod(0o700)
        docker_environment = _docker_environment(temp_root)
        _assert_no_collision(
            build_collision_preflight_commands(
                docker_binary=docker_binary, names=names, run_id=run_id
            ),
            docker_environment,
            phase_prefix="docker-preflight",
        )
        image_id = _inspect_local_image(docker_binary, docker_environment)
        dependency_snapshot = _dependency_source_snapshot()
        dependency_overlay_base, dependency_overlay_site = _copy_dependency_overlay(
            dependency_snapshot,
            temp_root=temp_root,
        )
        storage_guard = _RunStorageGuard(temp_root)
        storage_guard.start()
        try:
            password = secrets.token_urlsafe(32)
            db_env_file = temp_root / "postgres.env"
            _write_env_file(
                db_env_file,
                {"POSTGRES_DB": "postgres", "POSTGRES_PASSWORD": password},
            )
            network_id = _create_resources(
                docker_binary=docker_binary,
                names=names,
                run_id=run_id,
                image_id=image_id,
                db_env_file=db_env_file,
                environment=docker_environment,
                created=created,
            )
            container_ipv4 = _wait_for_postgres(
                docker_binary=docker_binary,
                names=names,
                expected_network_id=network_id,
                environment=docker_environment,
            )
            sys.path.insert(0, str(repo_root / "ea"))
            from app.services.admission_control import (  # noqa: PLC0415
                ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
            )

            _provision_admission_capacity_owner_role(
                docker_binary=docker_binary,
                names=names,
                role_name=ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
                environment=docker_environment,
            )
            database_relay = _LoopbackDatabaseRelay(container_ipv4)
            db_port = database_relay.start()
            database_relay.assert_healthy()
            database_url = f"postgresql://postgres:{password}@127.0.0.1:{db_port}/postgres"
            admission_password = secrets.token_urlsafe(32)
            admission_database_url = (
                "postgresql://propertyquarry_api_admission:"
                f"{admission_password}@127.0.0.1:{db_port}/postgres"
            )
            runtime_values = _runtime_environment(
                repo_root=repo_root,
                temp_root=temp_root,
                database_url=database_url,
                admission_database_url=admission_database_url,
                api_token=secrets.token_urlsafe(32),
                signing_secret=secrets.token_urlsafe(48),
                erasure_secret=secrets.token_urlsafe(48),
                chromium_headless_shell=chromium_headless_shell,
                dependency_overlay_base=dependency_overlay_base,
            )
            runtime_env_file = temp_root / "runtime.env"
            _write_env_file(runtime_env_file, runtime_values)
            runtime_environment = _read_env_file(runtime_env_file)
            _run_host(
                [python, "-m", "app.product.propertyquarry_schema", "migrate"],
                phase="schema-migrate",
                repo_root=repo_root,
                environment=runtime_environment,
                log_path=temp_root / "schema-migrate.log",
                storage_guard=storage_guard,
            )
            database_relay.assert_healthy()
            _provision_api_admission_role(
                admin_database_url=database_url,
                admission_database_url=admission_database_url,
                admission_password=admission_password,
            )
            database_relay.assert_healthy()
            _verify_api_admission_role(
                admission_database_url=admission_database_url,
            )
            database_relay.assert_healthy()
            _run_host(
                [python, "-m", "app.product.propertyquarry_schema", "check"],
                phase="schema-check",
                repo_root=repo_root,
                environment=runtime_environment,
                log_path=temp_root / "schema-check.log",
                storage_guard=storage_guard,
            )
            database_relay.assert_healthy()
            from app.product.property_search_schema import (  # noqa: PLC0415
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            )

            expected_reason = (
                f"postgres_ready:property_search_schema_v{LATEST_PROPERTY_SEARCH_SCHEMA_VERSION}"
            )
            api_process, api_port = _start_api(
                python=python,
                repo_root=repo_root,
                environment=runtime_environment,
                log_path=temp_root / "candidate-api.log",
                storage_guard=storage_guard,
            )
            base_url = f"http://127.0.0.1:{api_port}"
            _wait_api(
                base_url,
                expected_reason,
                api_process,
                log_path=temp_root / "candidate-api.log",
                storage_guard=storage_guard,
            )
            database_relay.assert_healthy()
            _run_host(
                [
                    python,
                    str(repo_root / "scripts" / "propertyquarry_postgres_browser_bootstrap.py"),
                    "--write",
                    str(session_path),
                ],
                phase="session-bootstrap",
                repo_root=repo_root,
                environment=runtime_environment,
                log_path=temp_root / "session-bootstrap.log",
                storage_guard=storage_guard,
            )
            database_relay.assert_healthy()
            protected_session_path = temp_root / "browser-session.json"
            _write_private_bytes(
                protected_session_path,
                _private_session_bytes(session_path),
            )
            try:
                session_path.unlink()
            except OSError:
                _fail("bootstrap-session-source-cleanup-failed")
            _private_session_bytes(protected_session_path)
            browser_environment = dict(runtime_environment)
            browser_environment.update(
                {
                    "PROPERTYQUARRY_POSTGRES_BROWSER_BASE_URL": base_url,
                    "PROPERTYQUARRY_POSTGRES_BROWSER_EXPECTED_READY_REASON": expected_reason,
                    "PROPERTYQUARRY_POSTGRES_BROWSER_SESSION_FILE": str(
                        protected_session_path
                    ),
                }
            )
            _run_host(
                [
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/e2e/test_propertyquarry_postgres_browser.py",
                    "-p",
                    "no:cacheprovider",
                ],
                phase="browser-test",
                repo_root=repo_root,
                environment=browser_environment,
                log_path=temp_root / "browser-pytest.log",
                storage_guard=storage_guard,
            )
            database_relay.assert_healthy()
        finally:
            lifecycle.begin_cleanup()
            _stop_api(api_process, storage_guard)
            storage_error: IsolatedPostgresError | None = None
            if storage_guard is not None:
                try:
                    storage_guard.stop()
                except IsolatedPostgresError as error:
                    storage_error = error
            relay_error: IsolatedPostgresError | None = None
            if database_relay is not None:
                try:
                    database_relay.stop()
                except IsolatedPostgresError as error:
                    relay_error = error
            try:
                session_path.unlink(missing_ok=True)
            except OSError:
                pass
            cleanup_error: IsolatedPostgresError | None = None
            try:
                _cleanup_resources(
                    docker_binary=docker_binary,
                    names=names,
                    run_id=run_id,
                    created=created,
                    environment=docker_environment,
                )
            except IsolatedPostgresError as error:
                cleanup_error = error
            # Evidence re-hashing runs only after the loopback relay is closed
            # and exact Docker inventory/removal has completed. It can never
            # delay cleanup of the critical disposable resources.
            dependency_error: IsolatedPostgresError | None = None
            if dependency_snapshot is not None and dependency_overlay_site is not None:
                try:
                    current_snapshot = _dependency_source_snapshot()
                    if current_snapshot != dependency_snapshot:
                        _fail("dependency-snapshot-invalid")
                    _verify_dependency_overlay(
                        dependency_snapshot,
                        overlay_site=dependency_overlay_site,
                    )
                except IsolatedPostgresError as error:
                    dependency_error = error
            _raise_post_cleanup_errors(
                cleanup_error=cleanup_error,
                dependency_error=dependency_error,
                storage_error=storage_error,
                relay_error=relay_error,
            )


def _execute_inside_scope(
    *,
    repo_root: Path,
    python: str,
    docker_binary: str,
    run_id: str,
    chromium_headless_shell: str,
) -> None:
    _clear_libpq_environment()
    _require_closed_libpq_environment()
    require_cgroup_limits()
    with _LifecycleGuard() as lifecycle:
        _execute_guarded(
            repo_root=repo_root,
            python=python,
            docker_binary=docker_binary,
            run_id=run_id,
            chromium_headless_shell=chromium_headless_shell,
            lifecycle=lifecycle,
        )


def _serve_fd(fd: int) -> int:
    require_cgroup_limits()
    if fd < 3:
        _fail("candidate-api-fd-invalid")
    try:
        import uvicorn
    except ImportError:
        _fail("candidate-api-runtime-missing")
    uvicorn.run(
        "app.main:app",
        fd=fd,
        log_level="warning",
        log_config=None,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated, host-capped PropertyQuarry PostgreSQL browser lane."
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--venv", default=".venv")
    parser.add_argument("--docker-binary", default="/usr/bin/docker")
    parser.add_argument("--systemd-run", default="/usr/bin/systemd-run")
    parser.add_argument(
        "--chromium-headless-shell",
        default="",
        help=(
            "absolute canonical Playwright Chromium headless-shell executable; "
            "installing or falling back to full Chrome is forbidden"
        ),
    )
    parser.add_argument("--inside-systemd-scope", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--serve-fd", type=int, default=-1, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.serve_fd >= 0:
            return _serve_fd(arguments.serve_fd)
        chromium_headless_shell = _validate_chromium_headless_shell(
            arguments.chromium_headless_shell
        )
        repo_root, python = _validate_worktree(arguments.repo_root, arguments.venv)
        docker_binary = _require_absolute_executable(
            arguments.docker_binary, "docker-client-invalid"
        )
        systemd_run = _require_absolute_executable(
            arguments.systemd_run, "systemd-run-invalid"
        )
        if arguments.inside_systemd_scope:
            if RUN_ID_RE.fullmatch(arguments.run_id) is None:
                _fail("run-id-invalid")
            _execute_inside_scope(
                repo_root=repo_root,
                python=python,
                docker_binary=docker_binary,
                run_id=arguments.run_id,
                chromium_headless_shell=chromium_headless_shell,
            )
            print(json.dumps({"status": "pass", "scope": "isolated-postgres-browser"}))
            return 0
        run_id = secrets.token_hex(8)
        command = build_systemd_scope_command(
            systemd_run=systemd_run,
            python=python,
            script=str(Path(__file__).resolve()),
            repo_root=str(repo_root),
            venv=str(Path(python).parents[1]),
            chromium_headless_shell=chromium_headless_shell,
            docker_binary=docker_binary,
            run_id=run_id,
        )
        with tempfile.TemporaryDirectory(prefix=f"pq-pg-e2e-scope-{run_id}-") as text:
            diagnostic_root = Path(text)
            diagnostic_root.chmod(0o700)
            diagnostic_path = diagnostic_root / "scoped-stderr.log"
            descriptor = _open_private_log(diagnostic_path)
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=descriptor,
                )
            except OSError:
                _fail("scoped-run-failed")
            finally:
                os.close(descriptor)
            if completed.returncode != 0:
                _fail(_scoped_failure_code(diagnostic_path) or "scoped-run-failed")
        print(json.dumps({"status": "pass", "scope": "isolated-postgres-browser"}))
        return 0
    except IsolatedPostgresError as error:
        print(f"isolated PostgreSQL browser gate failed: {error.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
