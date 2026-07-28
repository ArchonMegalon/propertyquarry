from __future__ import annotations

import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterator
import uuid
from xml.etree import ElementTree

import yaml


ROOT = Path(__file__).resolve().parents[1]
GATES_DIR = ROOT / "docs" / "exit_gates"

COMMON_STATUSES = ["pass", "fail", "watch", "blocked"]
PHASE_KEYS = [
    "phase",
    "name",
    "objective",
    "status_values",
    "required_test_modules",
    "required_contract_coverage",
    "required_browser_workflows",
    "required_persistence_assertions",
    "required_ui_affordances",
    "fail_closed_conditions",
    "exit_criteria",
    "evidence_artifacts",
]

_PYTEST_CHILD_SEQUENCE = itertools.count(1)
_DEFAULT_CHILD_TIMEOUT_SECONDS = 15 * 60
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5 * 60
_DEFAULT_MIN_FREE_BYTES = 4 * 1024 * 1024 * 1024
_DEFAULT_ARTIFACT_MIN_FREE_BYTES = 512 * 1024 * 1024
_DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS = 5
_FAILURE_LOG_TAIL_BYTES = 64 * 1024
_PROCESS_GROUP_GRACE_SECONDS = 5.0
_MAX_BROWSER_TMP_PATH_BYTES = 64
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class _GateLock:
    path: Path
    fd: int
    inherited: bool


def load_gate(filename: str) -> dict[str, object]:
    path = GATES_DIR / filename
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(payload, dict), f"{path} must parse to a mapping"
    return payload


def assert_test_modules_exist(paths: object) -> list[str]:
    assert isinstance(paths, list) and paths, "test module list must be a non-empty list"
    resolved: list[str] = []
    for item in paths:
        assert isinstance(item, str) and item.startswith("tests/"), f"invalid test module path: {item!r}"
        path = ROOT / item
        assert path.exists(), f"missing test module: {item}"
        resolved.append(item)
    return resolved


def run_pytest_modules(paths: list[str]) -> None:
    assert paths, "expected at least one pytest module"
    run_pytest_args(paths)


def _positive_int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AssertionError(f"{name} must be a positive integer") from exc
    assert value > 0, f"{name} must be a positive integer"
    return value


def _gate_tmp_root() -> tuple[Path, int]:
    configured = str(os.environ.get("PROPERTYQUARRY_GATE_TMP_ROOT") or "").strip()
    root = _absolute_path(Path(configured) if configured else Path(tempfile.gettempdir()))
    root.mkdir(parents=True, exist_ok=True)
    free_bytes = int(shutil.disk_usage(root).free)
    required_bytes = _positive_int_env(
        "PROPERTYQUARRY_GATE_MIN_FREE_BYTES",
        _DEFAULT_MIN_FREE_BYTES,
    )
    assert free_bytes >= required_bytes, (
        "propertyquarry_gate_insufficient_temp_space:"
        f"root={root}:free_bytes={free_bytes}:required_bytes={required_bytes}"
    )
    return root, free_bytes


def _secure_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode), (
        f"propertyquarry_gate_artifact_path_not_directory:path={path}"
    )
    path.chmod(0o700)
    return path


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else (Path.cwd() / expanded).absolute()


def _gate_run_id() -> str:
    configured = str(os.environ.get("PROPERTYQUARRY_GATE_RUN_ID") or "").strip()
    if configured:
        assert _RUN_ID_PATTERN.fullmatch(configured), (
            "PROPERTYQUARRY_GATE_RUN_ID must contain only letters, digits, '.', '_', or '-'"
        )
        return configured
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _gate_receipt_root(run_id: str) -> Path:
    configured = str(os.environ.get("PROPERTYQUARRY_GATE_RECEIPT_DIR") or "").strip()
    path = Path(configured) if configured else ROOT / "_completion" / "propertyquarry-exit-gate" / run_id
    return _secure_directory(_absolute_path(path))


def _required_artifact_free_bytes() -> int:
    return _positive_int_env(
        "PROPERTYQUARRY_GATE_ARTIFACT_MIN_FREE_BYTES",
        _DEFAULT_ARTIFACT_MIN_FREE_BYTES,
    )


def _assert_free_space(path: Path, *, required_bytes: int, label: str) -> int:
    free_bytes = int(shutil.disk_usage(path).free)
    assert free_bytes >= required_bytes, (
        "propertyquarry_gate_insufficient_space:"
        f"label={label}:root={path}:free_bytes={free_bytes}:required_bytes={required_bytes}"
    )
    return free_bytes


def _lock_path() -> Path:
    configured = str(os.environ.get("PROPERTYQUARRY_GATE_LOCK_PATH") or "").strip()
    path = Path(configured) if configured else Path(f"/tmp/propertyquarry-exit-gate-{os.getuid()}.lock")
    return _absolute_path(path)


def _open_lock_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        assert stat.S_ISREG(metadata.st_mode), f"propertyquarry_gate_lock_not_regular:path={path}"
        assert metadata.st_uid == os.getuid(), f"propertyquarry_gate_lock_wrong_owner:path={path}"
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _inherited_lock_fd(path: Path) -> int | None:
    raw = str(os.environ.get("PROPERTYQUARRY_GATE_LOCK_FD") or "").strip()
    if not raw:
        return None
    try:
        fd = int(raw)
    except ValueError as exc:
        raise AssertionError("PROPERTYQUARRY_GATE_LOCK_FD must be an open integer fd") from exc
    assert fd > 2, "PROPERTYQUARRY_GATE_LOCK_FD must be greater than 2"
    try:
        fd_metadata = os.fstat(fd)
        path_metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AssertionError("PROPERTYQUARRY_GATE_LOCK_FD is not open for the configured lock") from exc
    assert (fd_metadata.st_dev, fd_metadata.st_ino) == (path_metadata.st_dev, path_metadata.st_ino), (
        "PROPERTYQUARRY_GATE_LOCK_FD does not identify PROPERTYQUARRY_GATE_LOCK_PATH"
    )
    assert stat.S_ISREG(fd_metadata.st_mode), "PROPERTYQUARRY_GATE_LOCK_FD must identify a regular file"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise AssertionError("PROPERTYQUARRY_GATE_LOCK_FD is not part of the held gate lock") from exc
    os.set_inheritable(fd, False)
    return fd


@contextmanager
def _pytest_child_lock() -> Iterator[_GateLock]:
    lock_path = _lock_path()
    inherited_fd = _inherited_lock_fd(lock_path)
    if inherited_fd is not None:
        yield _GateLock(path=lock_path, fd=inherited_fd, inherited=True)
        return
    timeout_seconds = _positive_int_env(
        "PROPERTYQUARRY_GATE_LOCK_TIMEOUT_SECONDS",
        _DEFAULT_LOCK_TIMEOUT_SECONDS,
    )
    deadline = time.monotonic() + timeout_seconds
    fd = _open_lock_file(lock_path)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "propertyquarry_gate_lock_timeout:"
                        f"path={lock_path}:timeout_seconds={timeout_seconds}"
                    )
                time.sleep(0.1)
        try:
            yield _GateLock(path=lock_path, fd=fd, inherited=False)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _exclusive_text_file(path: Path) -> Iterator[object]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()


def _install_fd_bootstrap(child_temp: Path, environment: dict[str, str]) -> tuple[Path, Path]:
    bootstrap_root = _secure_directory(child_temp / "py")
    bootstrap_path = bootstrap_root / "sitecustomize.py"
    with _exclusive_text_file(bootstrap_path) as handle:
        handle.write(
            "import os\n"
            "_raw = os.environ.get('PROPERTYQUARRY_GATE_LOCK_FD', '').strip()\n"
            "if _raw:\n"
            "    try:\n"
            "        os.set_inheritable(int(_raw), False)\n"
            "    except (OSError, ValueError):\n"
            "        pass\n"
        )
    launcher_path = bootstrap_root / "gate_launch.py"
    with _exclusive_text_file(launcher_path) as handle:
        handle.write(
            "import os\n"
            "import sys\n"
            "_start_fd = int(os.environ.pop('PROPERTYQUARRY_GATE_START_FD'))\n"
            "try:\n"
            "    _start = os.read(_start_fd, 1)\n"
            "finally:\n"
            "    os.close(_start_fd)\n"
            "if _start != b'1':\n"
            "    raise SystemExit(97)\n"
            "_lock_fd = int(os.environ['PROPERTYQUARRY_GATE_LOCK_FD'])\n"
            "os.set_inheritable(_lock_fd, True)\n"
            "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
        )
    existing = str(environment.get("PYTHONPATH") or "").strip()
    environment["PYTHONPATH"] = (
        f"{bootstrap_root}{os.pathsep}{existing}" if existing else str(bootstrap_root)
    )
    return bootstrap_path, launcher_path


def _create_short_temp_alias(child_temp: Path) -> Path:
    alias_root = Path("/tmp")
    assert alias_root.is_dir(), "propertyquarry_gate_short_temp_alias_root_missing:/tmp"
    alias = alias_root / f"pqg-{uuid.uuid4().hex[:12]}"
    os.symlink(child_temp, alias, target_is_directory=True)
    assert alias.is_symlink() and alias.resolve() == child_temp.resolve(), (
        f"propertyquarry_gate_short_temp_alias_invalid:path={alias}"
    )
    assert len(os.fsencode(alias)) <= _MAX_BROWSER_TMP_PATH_BYTES, (
        "propertyquarry_gate_short_temp_alias_too_long:"
        f"path={alias}:bytes={len(os.fsencode(alias))}:limit={_MAX_BROWSER_TMP_PATH_BYTES}"
    )
    return alias


def _acquire_registry_flock(fd: int, operation: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "propertyquarry_gate_process_registry_lock_timeout:"
                    f"timeout_seconds={timeout_seconds}"
                )
            time.sleep(0.05)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    _secure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _process_registry_path(
    *,
    receipt_root: Path,
    run_id: str,
    inherited_lock: bool,
) -> Path:
    configured = str(os.environ.get("PROPERTYQUARRY_GATE_PROCESS_REGISTRY") or "").strip()
    if configured:
        path = _absolute_path(Path(configured))
    else:
        assert not inherited_lock, (
            "propertyquarry_gate_inherited_lock_missing_process_registry"
        )
        path = receipt_root / f".process-groups-{run_id}-{uuid.uuid4().hex[:12]}.jsonl"
    _secure_directory(path.parent)
    return path


def _register_process_group(
    path: Path,
    *,
    process_group_id: int,
    process_start_ticks: int,
    invocation_id: str,
    lock_timeout_seconds: float,
) -> None:
    assert process_start_ticks > 0, (
        "propertyquarry_gate_process_registry_missing_start_identity:"
        f"pgid={process_group_id}"
    )
    payload = json.dumps(
        {
            "invocation_id": invocation_id,
            "owner_pid": os.getpid(),
            "process_group_id": process_group_id,
            "process_start_ticks": process_start_ticks,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        assert stat.S_ISREG(metadata.st_mode), (
            f"propertyquarry_gate_process_registry_not_regular:path={path}"
        )
        assert metadata.st_uid == os.getuid(), (
            f"propertyquarry_gate_process_registry_wrong_owner:path={path}"
        )
        os.fchmod(fd, 0o600)
        _acquire_registry_flock(
            fd,
            fcntl.LOCK_EX,
            timeout_seconds=lock_timeout_seconds,
        )
        try:
            written = os.write(fd, payload)
            assert written == len(payload), (
                f"propertyquarry_gate_process_registry_short_write:path={path}"
            )
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _registered_process_groups(path: Path) -> list[tuple[int, int]]:
    if not path.exists():
        return []
    groups: list[tuple[int, int]] = []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid(), (
            f"propertyquarry_gate_process_registry_invalid_file:path={path}"
        )
        handle = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    with handle:
        _acquire_registry_flock(
            handle.fileno(),
            fcntl.LOCK_SH,
            timeout_seconds=_positive_int_env(
                "PROPERTYQUARRY_GATE_REGISTRY_LOCK_TIMEOUT_SECONDS",
                _DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS,
            ),
        )
        try:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    payload = json.loads(raw)
                    process_group_id = int(payload["process_group_id"])
                    process_start_ticks = int(payload.get("process_start_ticks") or 0)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AssertionError(
                        "propertyquarry_gate_process_registry_invalid:"
                        f"path={path}:line={line_number}"
                    ) from exc
                assert process_group_id > 1, (
                    "propertyquarry_gate_process_registry_invalid_pgid:"
                    f"path={path}:line={line_number}:pgid={process_group_id}"
                )
                identity = (process_group_id, process_start_ticks)
                if identity not in groups:
                    groups.append(identity)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return groups


def _process_start_ticks(process_id: int) -> int:
    path = Path(f"/proc/{process_id}/stat")
    try:
        raw = path.read_text(encoding="utf-8")
        closing_parenthesis = raw.rfind(")")
        assert closing_parenthesis > 0
        fields_after_command = raw[closing_parenthesis + 2 :].split()
        return int(fields_after_command[19])
    except (AssertionError, IndexError, OSError, ValueError):
        return 0


def _file_evidence(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": 0, "sha256": ""}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return {
        "path": str(path),
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _junit_evidence(path: Path) -> tuple[dict[str, object], str]:
    evidence = _file_evidence(path)
    if not evidence["exists"] or int(evidence["bytes"]) <= 0:
        return evidence, f"propertyquarry_gate_junit_missing_or_empty:path={path}"
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError:
        return evidence, f"propertyquarry_gate_junit_invalid_xml:path={path}"
    root_tag = root.tag.rsplit("}", 1)[-1]
    if root_tag not in {"testsuite", "testsuites"}:
        return evidence, f"propertyquarry_gate_junit_invalid_root:path={path}:root={root_tag}"
    test_cases = root.findall(".//testcase")
    if not test_cases:
        return evidence, f"propertyquarry_gate_junit_has_no_tests:path={path}"
    evidence.update(
        {
            "testcases": len(test_cases),
            "failures": len(root.findall(".//failure")),
            "errors": len(root.findall(".//error")),
            "skipped": len(root.findall(".//skipped")),
        }
    )
    return evidence, ""


def _log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        size = path.stat().st_size
        if size > _FAILURE_LOG_TAIL_BYTES:
            handle.seek(-_FAILURE_LOG_TAIL_BYTES, os.SEEK_END)
        return handle.read().decode("utf-8", errors="replace").strip()


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise AssertionError(
            f"propertyquarry_gate_process_group_permission_denied:pgid={process_group_id}"
        ) from exc


def _wait_for_process_group_exit(process_group_id: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _terminate_process_group(process: subprocess.Popen[object]) -> dict[str, bool]:
    process_group_id = process.pid
    term_sent = False
    kill_sent = False
    if _process_group_exists(process_group_id):
        os.killpg(process_group_id, signal.SIGTERM)
        term_sent = True
    try:
        process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if not _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_GRACE_SECONDS,
    ):
        os.killpg(process_group_id, signal.SIGKILL)
        kill_sent = True
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    assert _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_GRACE_SECONDS,
    ), f"propertyquarry_gate_process_group_survived_sigkill:pgid={process_group_id}"
    return {"sigterm_sent": term_sent, "sigkill_sent": kill_sent}


def _terminate_registered_process_group(process_group_id: int) -> dict[str, bool]:
    term_sent = False
    kill_sent = False
    if _process_group_exists(process_group_id):
        os.killpg(process_group_id, signal.SIGTERM)
        term_sent = True
    if not _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_GRACE_SECONDS,
    ):
        os.killpg(process_group_id, signal.SIGKILL)
        kill_sent = True
    assert _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=_PROCESS_GROUP_GRACE_SECONDS,
    ), f"propertyquarry_gate_registered_process_group_survived_sigkill:pgid={process_group_id}"
    return {"sigterm_sent": term_sent, "sigkill_sent": kill_sent}


def _cleanup_registered_process_groups(
    path: Path,
    *,
    exclude: set[int] | None = None,
) -> list[dict[str, object]]:
    excluded = set(exclude or ())
    excluded.add(os.getpgrp())
    cleanup: list[dict[str, object]] = []
    for process_group_id, expected_start_ticks in reversed(_registered_process_groups(path)):
        if process_group_id in excluded or not _process_group_exists(process_group_id):
            continue
        observed_start_ticks = _process_start_ticks(process_group_id)
        assert expected_start_ticks > 0 and observed_start_ticks == expected_start_ticks, (
            "propertyquarry_gate_process_registry_identity_mismatch:"
            f"pgid={process_group_id}:expected_start_ticks={expected_start_ticks}:"
            f"observed_start_ticks={observed_start_ticks}"
        )
        cleanup.append(
            {
                "process_group_id": process_group_id,
                "process_start_ticks": expected_start_ticks,
                **_terminate_registered_process_group(process_group_id),
            }
        )
    return cleanup


def run_pytest_args(args: list[str]) -> None:
    assert args, "expected at least one pytest argument"
    assert not any(
        str(item) in {"--junitxml", "--junit-xml"}
        or str(item).startswith(("--junitxml=", "--junit-xml="))
        for item in args
    ), "run_pytest_args owns --junitxml so child evidence cannot be redirected"
    timeout_seconds = _positive_int_env(
        "PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS",
        _DEFAULT_CHILD_TIMEOUT_SECONDS,
    )
    artifact_required_bytes = _required_artifact_free_bytes()
    temp_root, free_bytes_before = _gate_tmp_root()
    run_id = _gate_run_id()
    receipt_root = _gate_receipt_root(run_id)
    artifact_free_bytes_before = _assert_free_space(
        receipt_root,
        required_bytes=artifact_required_bytes,
        label="artifacts_before_lock",
    )
    sequence = next(_PYTEST_CHILD_SEQUENCE)
    digest = hashlib.sha256("\0".join(args).encode("utf-8")).hexdigest()[:12]
    stem = f"pytest-child-{os.getpid()}-{sequence:02d}-{digest}-{uuid.uuid4().hex[:12]}"
    # Chromium creates additional profile/socket paths below TMPDIR. Keep this
    # ephemeral component short enough for Linux AF_UNIX limits; durable
    # artifacts retain the fully descriptive collision-resistant stem.
    child_temp = Path(
        tempfile.mkdtemp(
            prefix=f"pqg-{uuid.uuid4().hex[:8]}-",
            dir=temp_root,
        )
    )
    child_temp_alias = _create_short_temp_alias(child_temp)
    stdout_path = receipt_root / f"{stem}.stdout.log"
    stderr_path = receipt_root / f"{stem}.stderr.log"
    junit_path = receipt_root / f"{stem}.junit.xml"
    receipt_path = receipt_root / f"{stem}.receipt.json"
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *args,
    ]
    command.append(f"--junitxml={junit_path}")
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("TMPDIR", "TMP", "TEMP"):
        environment[name] = str(child_temp_alias)
    environment["PYTEST_DEBUG_TEMPROOT"] = str(child_temp_alias)
    environment["PROPERTYQUARRY_GATE_RUN_ID"] = run_id
    environment["PROPERTYQUARRY_GATE_RECEIPT_DIR"] = str(receipt_root)
    started_at = datetime.now(timezone.utc)
    status = "error"
    return_code: int | None = None
    timed_out = False
    residual_process_group = False
    process_id = 0
    process: subprocess.Popen[object] | None = None
    lock_path = _lock_path()
    lock_inherited = False
    process_cleanup: dict[str, bool] = {"sigterm_sent": False, "sigkill_sent": False}
    registered_process_cleanup: list[dict[str, object]] = []
    process_registry_path: Path | None = None
    bootstrap_path: Path | None = None
    launcher_path: Path | None = None
    launch_command = list(command)
    junit_evidence: dict[str, object] = {
        "path": str(junit_path),
        "exists": False,
        "bytes": 0,
        "sha256": "",
    }
    junit_validation_error = ""
    caught: BaseException | None = None
    cleanup_errors: list[str] = []
    temp_cleanup_error = ""
    receipt_error = ""
    receipt_written = False

    def _remember_cleanup_error(exc: BaseException, *, operation: str) -> None:
        nonlocal caught
        cleanup_errors.append(f"{operation}:{type(exc).__name__}:{exc}")
        if caught is None:
            caught = exc

    def _remember_finalization_exception(exc: BaseException, *, operation: str) -> str:
        nonlocal caught
        detail = f"{operation}:{type(exc).__name__}:{exc}"
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and caught is None:
            caught = exc
        return detail

    def _cleanup_direct_process() -> None:
        nonlocal process_cleanup, return_code
        if process is None:
            return
        try:
            if _process_group_exists(process.pid):
                process_cleanup = _terminate_process_group(process)
        except BaseException as exc:
            _remember_cleanup_error(exc, operation="direct_process_cleanup")
        finally:
            return_code = process.returncode

    def _cleanup_registry() -> None:
        nonlocal registered_process_cleanup, residual_process_group
        if lock_inherited or process_registry_path is None:
            return
        try:
            cleanup = _cleanup_registered_process_groups(process_registry_path)
            if cleanup:
                residual_process_group = True
                registered_process_cleanup.extend(cleanup)
        except BaseException as exc:
            _remember_cleanup_error(exc, operation="registered_process_cleanup")

    try:
        with _pytest_child_lock() as gate_lock:
            lock_path = gate_lock.path
            lock_inherited = gate_lock.inherited
            process_registry_path = _process_registry_path(
                receipt_root=receipt_root,
                run_id=run_id,
                inherited_lock=gate_lock.inherited,
            )
            environment["PROPERTYQUARRY_GATE_LOCK_PATH"] = str(gate_lock.path)
            environment["PROPERTYQUARRY_GATE_LOCK_FD"] = str(gate_lock.fd)
            environment["PROPERTYQUARRY_GATE_PROCESS_REGISTRY"] = str(process_registry_path)
            bootstrap_path, launcher_path = _install_fd_bootstrap(child_temp, environment)
            free_bytes_before = _assert_free_space(
                temp_root,
                required_bytes=_positive_int_env(
                    "PROPERTYQUARRY_GATE_MIN_FREE_BYTES",
                    _DEFAULT_MIN_FREE_BYTES,
                ),
                label="temp_after_lock",
            )
            artifact_free_bytes_before = _assert_free_space(
                receipt_root,
                required_bytes=artifact_required_bytes,
                label="artifacts_after_lock",
            )
            with _exclusive_text_file(stdout_path) as stdout_handle, _exclusive_text_file(
                stderr_path
            ) as stderr_handle:
                start_read_fd = -1
                start_write_fd = -1
                try:
                    start_read_fd, start_write_fd = os.pipe()
                    os.set_inheritable(start_read_fd, False)
                    os.set_inheritable(start_write_fd, False)
                    child_deadline = time.monotonic() + timeout_seconds
                    environment["PROPERTYQUARRY_GATE_START_FD"] = str(start_read_fd)
                    launch_command = [sys.executable, str(launcher_path), *command[1:]]
                    try:
                        process = subprocess.Popen(
                            launch_command,
                            cwd=ROOT,
                            env=environment,
                            text=True,
                            stdout=stdout_handle,
                            stderr=stderr_handle,
                            start_new_session=True,
                            pass_fds=(gate_lock.fd, start_read_fd),
                        )
                        process_id = process.pid
                    finally:
                        os.close(start_read_fd)
                        start_read_fd = -1
                    try:
                        remaining_seconds = max(0.0, child_deadline - time.monotonic())
                        _register_process_group(
                            process_registry_path,
                            process_group_id=process.pid,
                            process_start_ticks=_process_start_ticks(process.pid),
                            invocation_id=stem,
                            lock_timeout_seconds=min(
                                remaining_seconds,
                                float(
                                    _positive_int_env(
                                        "PROPERTYQUARRY_GATE_REGISTRY_LOCK_TIMEOUT_SECONDS",
                                        _DEFAULT_REGISTRY_LOCK_TIMEOUT_SECONDS,
                                    )
                                ),
                            ),
                        )
                        written = os.write(start_write_fd, b"1")
                        assert written == 1, "propertyquarry_gate_start_barrier_short_write"
                    finally:
                        os.close(start_write_fd)
                        start_write_fd = -1
                    try:
                        return_code = process.wait(
                            timeout=max(0.001, child_deadline - time.monotonic())
                        )
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        _cleanup_direct_process()
                    except BaseException as exc:
                        caught = exc
                        _cleanup_direct_process()
                    else:
                        if _process_group_exists(process.pid):
                            residual_process_group = True
                            _cleanup_direct_process()
                except BaseException:
                    _cleanup_direct_process()
                    raise
                finally:
                    for fd in (start_read_fd, start_write_fd):
                        if fd >= 0:
                            with contextlib.suppress(OSError):
                                os.close(fd)
            _cleanup_registry()
            if caught is None:
                try:
                    junit_evidence, junit_validation_error = _junit_evidence(junit_path)
                except BaseException as exc:
                    caught = exc
                status = (
                    "pass"
                    if caught is None
                    and return_code == 0
                    and not timed_out
                    and not residual_process_group
                    and not junit_validation_error
                    and int(junit_evidence.get("failures") or 0) == 0
                    and int(junit_evidence.get("errors") or 0) == 0
                    else "fail"
                )
    except BaseException as exc:
        if caught is None:
            caught = exc
        status = "error"
        _cleanup_direct_process()
        _cleanup_registry()
    finally:
        try:
            return_code = process.returncode if process is not None else return_code
            temp_cleanup_errors: list[str] = []
            try:
                child_temp_alias.unlink()
            except FileNotFoundError:
                pass
            except BaseException as exc:
                temp_cleanup_errors.append(
                    _remember_finalization_exception(exc, operation="temp_alias_cleanup")
                )
            if child_temp_alias.exists() or child_temp_alias.is_symlink():
                temp_cleanup_errors.append(
                    f"propertyquarry_gate_temp_alias_cleanup_incomplete:path={child_temp_alias}"
                )
            try:
                shutil.rmtree(child_temp)
            except BaseException as exc:
                temp_cleanup_errors.append(
                    _remember_finalization_exception(exc, operation="temp_tree_cleanup")
                )
            if child_temp.exists():
                temp_cleanup_errors.append(
                    f"propertyquarry_gate_temp_cleanup_incomplete:path={child_temp}"
                )
            temp_cleanup_error = ";".join(temp_cleanup_errors)
            if temp_cleanup_error:
                status = "error"

            evidence_errors: list[str] = []

            def _safe_file_evidence(path: Path) -> dict[str, object]:
                try:
                    return _file_evidence(path)
                except BaseException as exc:
                    detail = _remember_finalization_exception(
                        exc,
                        operation=f"file_evidence:{path}",
                    )
                    evidence_errors.append(detail)
                    return {
                        "path": str(path),
                        "exists": path.exists(),
                        "bytes": 0,
                        "sha256": "",
                        "evidence_error": f"{type(exc).__name__}:{exc}",
                    }

            def _safe_free_bytes(path: Path) -> int:
                try:
                    return int(shutil.disk_usage(path).free)
                except BaseException as exc:
                    evidence_errors.append(
                        _remember_finalization_exception(
                            exc,
                            operation=f"disk_usage:{path}",
                        )
                    )
                    return -1

            finished_at = datetime.now(timezone.utc)
            payload: dict[str, object] = {
                "schema": "propertyquarry.pytest_child.v3",
                "run_id": run_id,
                "status": status,
                "command": command,
                "launch_command": launch_command,
                "args": list(args),
                "pid": process_id,
                "return_code": return_code,
                "timed_out": timed_out,
                "residual_process_group": residual_process_group,
                "process_cleanup": process_cleanup,
                "registered_process_cleanup": registered_process_cleanup,
                "process_registry": str(process_registry_path or ""),
                "timeout_seconds": timeout_seconds,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
                "temp_root": str(temp_root),
                "temp_dir": str(child_temp),
                "temp_alias": str(child_temp_alias),
                "temp_free_bytes_before_launch": free_bytes_before,
                "temp_free_bytes_after": _safe_free_bytes(temp_root),
                "temp_cleanup_error": temp_cleanup_error,
                "artifact_root": str(receipt_root),
                "artifact_free_bytes_before_launch": artifact_free_bytes_before,
                "artifact_free_bytes_after": _safe_free_bytes(receipt_root),
                "lock_path": str(lock_path),
                "lock_inherited": lock_inherited,
                "lock_fd_bootstrap": str(bootstrap_path or ""),
                "stdout": _safe_file_evidence(stdout_path),
                "stderr": _safe_file_evidence(stderr_path),
                "junit": junit_evidence,
                "junit_validation_error": junit_validation_error,
                "cleanup_errors": cleanup_errors,
                "evidence_errors": evidence_errors,
                "error_type": type(caught).__name__ if caught is not None else "",
                "error": str(caught) if caught is not None else "",
            }
            try:
                _atomic_write_json(receipt_path, payload)
                receipt_written = True
            except BaseException as exc:
                receipt_error = _remember_finalization_exception(
                    exc,
                    operation="receipt_write",
                )
        except BaseException as exc:
            receipt_error = _remember_finalization_exception(
                exc,
                operation="receipt_finalization",
            )

    if caught is not None:
        for note in [*cleanup_errors, temp_cleanup_error, receipt_error]:
            if note:
                caught.add_note(note)
        if isinstance(caught, (KeyboardInterrupt, SystemExit)):
            raise caught
        raise AssertionError(
            "pytest child orchestration failed:"
            f"error={type(caught).__name__}:{caught}:receipt={receipt_path}:"
            f"cleanup_errors={cleanup_errors}:temp_cleanup_error={temp_cleanup_error}:"
            f"receipt_error={receipt_error}"
        ) from caught
    if receipt_error:
        raise AssertionError(
            "propertyquarry_gate_receipt_write_failed:"
            f"path={receipt_path}:error={receipt_error}"
        )
    if status != "pass":
        timeout_detail = f"pytest child timed out after {timeout_seconds}s" if timed_out else ""
        residual_detail = "pytest child left a residual process group" if residual_process_group else ""
        junit_detail = junit_validation_error
        message = "\n".join(
            part
            for part in (
                f"pytest command failed: {shlex.join(command)}",
                f"return_code={return_code}",
                timeout_detail,
                residual_detail,
                junit_detail,
                temp_cleanup_error,
                "; ".join(cleanup_errors),
                f"pytest child receipt: {receipt_path}" if receipt_written else "pytest child receipt unavailable",
                _log_tail(stdout_path),
                _log_tail(stderr_path),
            )
            if part
        )
        raise AssertionError(message)


def assert_contains_strings(values: object, expected: list[str], *, field_name: str) -> None:
    assert isinstance(values, list) and values, f"{field_name} must be a non-empty list"
    missing = [item for item in expected if item not in values]
    assert not missing, f"{field_name} is missing required items: {missing}"


def assert_workflow_checks(
    payload: dict[str, object], *, workflow_name: str, expected_checks: list[str]
) -> None:
    workflows = payload["required_browser_workflows"]
    assert isinstance(workflows, list)
    workflow = next((row for row in workflows if isinstance(row, dict) and row.get("name") == workflow_name), None)
    assert workflow is not None, f"required_browser_workflows is missing {workflow_name}"
    assert_contains_strings(workflow.get("checks"), expected_checks, field_name=f"{workflow_name}.checks")


def assert_phase_gate_shape(payload: dict[str, object], *, phase: int) -> None:
    assert sorted(PHASE_KEYS) == sorted(payload.keys())
    assert payload["phase"] == phase
    assert payload["status_values"] == COMMON_STATUSES
    required_test_modules = payload["required_test_modules"]
    assert isinstance(required_test_modules, dict)
    assert sorted(required_test_modules.keys()) == ["browser", "contract", "gate"]
    for key, value in required_test_modules.items():
        assert isinstance(value, list) and value, f"{key} test modules must be a non-empty list"
        assert all(isinstance(item, str) and item.startswith("tests/") for item in value)
    for list_key in (
        "required_contract_coverage",
        "required_persistence_assertions",
        "required_ui_affordances",
        "fail_closed_conditions",
        "exit_criteria",
        "evidence_artifacts",
    ):
        value = payload[list_key]
        assert isinstance(value, list) and value, f"{list_key} must be a non-empty list"
        assert all(isinstance(item, str) and item.strip() for item in value)
    workflows = payload["required_browser_workflows"]
    assert isinstance(workflows, list) and workflows
    for row in workflows:
        assert isinstance(row, dict)
        assert isinstance(row.get("name"), str) and str(row["name"]).strip()
        checks = row.get("checks")
        assert isinstance(checks, list) and checks
        assert all(isinstance(item, str) and item.strip() for item in checks)


def assert_master_gate_shape(payload: dict[str, object]) -> None:
    assert sorted(payload.keys()) == sorted(
        ["name", "objective", "required_test_modules", "required_browser_workflows", "fail_closed_conditions", "exit_criteria"]
    )
    assert isinstance(payload["name"], str) and str(payload["name"]).strip()
    assert isinstance(payload["objective"], str) and str(payload["objective"]).strip()
    test_modules = payload["required_test_modules"]
    assert isinstance(test_modules, list) and test_modules
    assert all(isinstance(item, str) and item.startswith("tests/") for item in test_modules)
    for list_key in ("required_browser_workflows", "fail_closed_conditions", "exit_criteria"):
        value = payload[list_key]
        assert isinstance(value, list) and value
        assert all(isinstance(item, str) and item.strip() for item in value)
