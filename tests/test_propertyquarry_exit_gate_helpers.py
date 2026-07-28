from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import sys
from types import SimpleNamespace

import pytest

from tests import propertyquarry_exit_gate_helpers as helpers


_PASSING_JUNIT = (
    '<testsuite tests="1" failures="0" errors="0" skipped="0">'
    '<testcase classname="gate" name="passes"/></testsuite>\n'
)
_NONEXISTENT_PROCESS_GROUP_ID = (1 << 31) - 1


class _FakeProcess:
    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        stdout,
        stderr,
        return_code: int = 0,
        first_wait_error: BaseException | None = None,
        write_junit: bool = True,
    ) -> None:
        self.command = list(command)
        self.environment = dict(env)
        # Linux limits pid_max to well below signed pid_t's upper bound. Using a
        # real related PID made this fake depend on whether the parent test
        # runner happened to be a process-group leader.
        self.pid = _NONEXISTENT_PROCESS_GROUP_ID
        self.returncode: int | None = None
        self._return_code = return_code
        self._first_wait_error = first_wait_error
        self._wait_calls = 0
        raw_barrier_fd = str(env.get("PROPERTYQUARRY_GATE_START_FD") or "").strip()
        self._barrier_fd = os.dup(int(raw_barrier_fd)) if raw_barrier_fd else -1
        stdout.write("child stdout\n")
        stderr.write("child stderr\n")
        if write_junit:
            for item in command:
                if item.startswith("--junitxml="):
                    Path(item.split("=", 1)[1]).write_text(_PASSING_JUNIT, encoding="utf-8")

    def wait(self, timeout: float) -> int:
        self._wait_calls += 1
        if self._barrier_fd >= 0:
            os.close(self._barrier_fd)
            self._barrier_fd = -1
        if self._wait_calls == 1 and self._first_wait_error is not None:
            raise self._first_wait_error
        self.returncode = self._return_code
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode


def _configure_fake_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    real_process_start_ticks = helpers._process_start_ticks

    def _fake_process_start_ticks(pid: int) -> int:
        if pid == _NONEXISTENT_PROCESS_GROUP_ID:
            return 123_456_789
        return real_process_start_ticks(pid)

    monkeypatch.setattr(
        helpers,
        "_process_start_ticks",
        _fake_process_start_ticks,
    )


def _configure_gate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _configure_fake_process_identity(monkeypatch)
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setenv("PROPERTYQUARRY_GATE_TMP_ROOT", str(tmp_path))
    monkeypatch.setenv("PROPERTYQUARRY_GATE_RECEIPT_DIR", str(receipt_dir))
    monkeypatch.setenv("PROPERTYQUARRY_GATE_LOCK_PATH", str(tmp_path / "gate.lock"))
    monkeypatch.setenv("PROPERTYQUARRY_GATE_RUN_ID", "unit-gate-run")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_MIN_FREE_BYTES", "1")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_ARTIFACT_MIN_FREE_BYTES", "1")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_LOCK_TIMEOUT_SECONDS", "2")
    monkeypatch.delenv("PROPERTYQUARRY_GATE_LOCK_FD", raising=False)
    return receipt_dir


def _receipts(receipt_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(receipt_dir.iterdir())
        if path.name.endswith(".receipt.json")
    ]


def test_gate_tmp_root_fails_closed_when_space_is_below_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_GATE_TMP_ROOT", str(tmp_path))
    monkeypatch.setenv("PROPERTYQUARRY_GATE_MIN_FREE_BYTES", "10")
    monkeypatch.setattr(helpers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=9))

    with pytest.raises(AssertionError, match="propertyquarry_gate_insufficient_temp_space"):
        helpers._gate_tmp_root()


def test_invalid_timeout_is_rejected_before_temp_allocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS", "invalid")
    before = {path.name for path in tmp_path.iterdir()}

    with pytest.raises(AssertionError, match="PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS"):
        helpers.run_pytest_args(["tests/example.py"])

    assert {path.name for path in tmp_path.iterdir()} == before


def test_run_pytest_args_isolates_child_and_writes_private_durable_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    observed: list[tuple[_FakeProcess, dict[str, object]]] = []

    def _popen(command, *, env, stdout, stderr, **kwargs):
        process = _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)
        observed.append((process, kwargs))
        return process

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)

    helpers.run_pytest_args(["tests/example.py", "-k", "launch"])
    helpers.run_pytest_args(["tests/example.py", "-k", "launch"])

    assert len(observed) == 2
    process, kwargs = observed[0]
    assert process.command[0] == sys.executable
    assert Path(process.command[1]).name == "gate_launch.py"
    assert process.command[2:8] == [
        "-B",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    assert process.environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert process.environment["TMPDIR"] == process.environment["TMP"] == process.environment["TEMP"]
    child_temp_alias = Path(process.environment["TMPDIR"])
    assert process.environment["PYTEST_DEBUG_TEMPROOT"] == str(
        child_temp_alias
    )
    assert child_temp_alias.parent == Path("/tmp")
    assert len(os.fsencode(child_temp_alias)) <= helpers._MAX_BROWSER_TMP_PATH_BYTES
    assert not child_temp_alias.exists()
    assert kwargs["start_new_session"] is True
    assert kwargs["pass_fds"][0] == int(process.environment["PROPERTYQUARRY_GATE_LOCK_FD"])
    assert len(kwargs["pass_fds"]) == 2
    receipts = _receipts(receipt_dir)
    assert len(receipts) == 2
    assert {receipt["status"] for receipt in receipts} == {"pass"}
    assert all(receipt["schema"] == "propertyquarry.pytest_child.v3" for receipt in receipts)
    assert all(receipt["return_code"] == 0 for receipt in receipts)
    assert all(Path(str(receipt["temp_dir"])).parent == tmp_path for receipt in receipts)
    assert all(not Path(str(receipt["temp_dir"])).exists() for receipt in receipts)
    assert all(receipt["junit"]["testcases"] == 1 for receipt in receipts)
    assert stat.S_IMODE(receipt_dir.stat().st_mode) == 0o700
    for path in receipt_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_run_pytest_args_terminates_process_group_and_receipts_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    group_alive = True
    killed: list[tuple[int, int]] = []

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(
            command,
            env=env,
            stdout=stdout,
            stderr=stderr,
            return_code=-signal.SIGTERM,
            first_wait_error=helpers.subprocess.TimeoutExpired(command, 7),
        )

    def _killpg(pid: int, sig: int) -> None:
        nonlocal group_alive
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            group_alive = False

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(helpers, "_process_group_exists", lambda _pid: group_alive)
    monkeypatch.setattr(helpers.os, "killpg", _killpg)

    with pytest.raises(AssertionError, match="timed out after 7s"):
        helpers.run_pytest_args(["tests/slow.py"])

    assert killed == [(_NONEXISTENT_PROCESS_GROUP_ID, signal.SIGTERM)]
    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "fail"
    assert receipt["timed_out"] is True
    assert receipt["return_code"] == -signal.SIGTERM
    assert receipt["process_cleanup"] == {"sigkill_sent": False, "sigterm_sent": True}


def test_terminate_process_group_escalates_after_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_alive = True
    killed: list[int] = []
    process = _FakeProcess(
        ["pytest"],
        env={},
        stdout=SimpleNamespace(write=lambda _value: None),
        stderr=SimpleNamespace(write=lambda _value: None),
        return_code=0,
        write_junit=False,
    )
    process.returncode = 0

    def _killpg(_pid: int, sig: int) -> None:
        nonlocal group_alive
        killed.append(sig)
        if sig == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(helpers, "_PROCESS_GROUP_GRACE_SECONDS", 0.001)
    monkeypatch.setattr(helpers, "_process_group_exists", lambda _pid: group_alive)
    monkeypatch.setattr(helpers.os, "killpg", _killpg)

    result = helpers._terminate_process_group(process)

    assert killed == [signal.SIGTERM, signal.SIGKILL]
    assert result == {"sigterm_sent": True, "sigkill_sent": True}


def test_unexpected_wait_error_cleans_group_and_writes_error_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    group_alive = True
    killed: list[int] = []

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(
            command,
            env=env,
            stdout=stdout,
            stderr=stderr,
            return_code=-signal.SIGTERM,
            first_wait_error=RuntimeError("wait failed"),
        )

    def _killpg(_pid: int, sig: int) -> None:
        nonlocal group_alive
        killed.append(sig)
        group_alive = False

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(helpers, "_process_group_exists", lambda _pid: group_alive)
    monkeypatch.setattr(helpers.os, "killpg", _killpg)

    with pytest.raises(AssertionError, match="RuntimeError:wait failed"):
        helpers.run_pytest_args(["tests/wait-error.py"])

    assert killed == [signal.SIGTERM]
    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "error"
    assert receipt["error_type"] == "RuntimeError"
    assert receipt["return_code"] == -signal.SIGTERM
    assert receipt["process_cleanup"]["sigterm_sent"] is True


def test_zero_exit_without_junit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(
            command,
            env=env,
            stdout=stdout,
            stderr=stderr,
            write_junit=False,
        )

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)

    with pytest.raises(AssertionError, match="propertyquarry_gate_junit_missing_or_empty"):
        helpers.run_pytest_args(["tests/no-junit.py"])

    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "fail"
    assert receipt["return_code"] == 0
    assert receipt["junit"]["exists"] is False


def test_custom_junit_argument_is_rejected_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_gate_environment(monkeypatch, tmp_path)

    with pytest.raises(AssertionError, match="owns --junitxml"):
        helpers.run_pytest_args(["tests/example.py", "--junitxml=elsewhere.xml"])

    assert not (tmp_path / "receipts").exists()


def test_nested_real_helpers_reuse_inherited_lock_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS", "30")
    leaf = tmp_path / "test_gate_leaf.py"
    nested = tmp_path / "test_gate_nested.py"
    leaf.write_text("def test_leaf():\n    assert True\n", encoding="utf-8")
    nested.write_text(
        "import os\n"
        "from tests import propertyquarry_exit_gate_helpers as helpers\n"
        "def test_nested():\n"
        "    helpers.run_pytest_args([os.environ['PQ_GATE_LEAF_TEST'], '-p', 'no:cacheprovider'])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PQ_GATE_LEAF_TEST", str(leaf))

    helpers.run_pytest_args([str(nested), "-p", "no:cacheprovider"])

    receipts = _receipts(receipt_dir)
    assert len(receipts) == 2
    assert {receipt["status"] for receipt in receipts} == {"pass"}
    assert {receipt["lock_inherited"] for receipt in receipts} == {False, True}
    assert all(receipt["junit"]["testcases"] == 1 for receipt in receipts)


def test_malformed_junit_retains_artifact_hash_in_failure_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        process = _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)
        junit_path = next(Path(item.split("=", 1)[1]) for item in command if item.startswith("--junitxml="))
        junit_path.write_text("<broken", encoding="utf-8")
        return process

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)

    with pytest.raises(AssertionError, match="propertyquarry_gate_junit_invalid_xml"):
        helpers.run_pytest_args(["tests/malformed-junit.py"])

    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "fail"
    assert receipt["junit"]["exists"] is True
    assert receipt["junit"]["bytes"] == len("<broken")
    assert len(receipt["junit"]["sha256"]) == 64


def test_temp_cleanup_failure_changes_pass_to_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        helpers.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )

    with pytest.raises(AssertionError, match="propertyquarry_gate_temp_cleanup_incomplete"):
        helpers.run_pytest_args(["tests/temp-cleanup.py"])

    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "error"
    assert "PermissionError:cleanup denied" in receipt["temp_cleanup_error"]


def test_relative_gate_paths_are_normalized_before_child_cwd_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_fake_process_identity(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_TMP_ROOT", "scratch")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_RECEIPT_DIR", "receipts")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_LOCK_PATH", "locks/gate.lock")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_RUN_ID", "relative-path-run")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_MIN_FREE_BYTES", "1")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_ARTIFACT_MIN_FREE_BYTES", "1")
    observed: list[_FakeProcess] = []

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        process = _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)
        observed.append(process)
        return process

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)

    helpers.run_pytest_args(["tests/relative.py"])

    assert Path(observed[0].environment["TMPDIR"]).is_absolute()
    assert Path(observed[0].environment["PROPERTYQUARRY_GATE_RECEIPT_DIR"]).is_absolute()
    assert Path(observed[0].environment["PROPERTYQUARRY_GATE_LOCK_PATH"]).is_absolute()
    assert (tmp_path / "receipts").is_dir()


def test_child_bootstrap_prevents_lock_fd_leak_to_close_fds_false_grandchild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    leaf = tmp_path / "test_lock_fd_inheritance.py"
    leaf.write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "def test_lock_fd_is_not_inheritable():\n"
        "    fd = int(os.environ['PROPERTYQUARRY_GATE_LOCK_FD'])\n"
        "    assert os.get_inheritable(fd) is False\n"
        "    code = (\"import os,sys; fd=int(os.environ['PROPERTYQUARRY_GATE_LOCK_FD']); \"\n"
        "            \"sys.exit(7 if os.path.exists('/proc/self/fd/' + str(fd)) else 0)\")\n"
        "    completed = subprocess.run([sys.executable, '-c', code], close_fds=False, env=os.environ.copy())\n"
        "    assert completed.returncode == 0\n",
        encoding="utf-8",
    )

    helpers.run_pytest_args([str(leaf), "-p", "no:cacheprovider"])

    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "pass"
    assert receipt["junit"]["testcases"] == 1


def test_nested_timeout_registry_kills_grandchild_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS", "3")
    marker = tmp_path / "leaf-pgid.txt"
    leaf = tmp_path / "test_slow_gate_leaf.py"
    nested = tmp_path / "test_slow_gate_nested.py"
    leaf.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import time\n"
        "def test_slow_leaf():\n"
        "    Path(os.environ['PQ_GATE_LEAF_MARKER']).write_text(str(os.getpgrp()), encoding='utf-8')\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    nested.write_text(
        "import os\n"
        "from tests import propertyquarry_exit_gate_helpers as helpers\n"
        "def test_nested_timeout():\n"
        "    helpers.run_pytest_args([os.environ['PQ_GATE_SLOW_LEAF'], '-p', 'no:cacheprovider'])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PQ_GATE_SLOW_LEAF", str(leaf))
    monkeypatch.setenv("PQ_GATE_LEAF_MARKER", str(marker))

    with pytest.raises(AssertionError, match="timed out after 3s"):
        helpers.run_pytest_args([str(nested), "-p", "no:cacheprovider"])

    assert marker.exists(), "nested leaf must have started before the outer timeout"
    leaf_process_group = int(marker.read_text(encoding="utf-8"))
    assert not helpers._process_group_exists(leaf_process_group)
    receipt = _receipts(receipt_dir)[0]
    assert receipt["timed_out"] is True
    assert any(
        row["process_group_id"] == leaf_process_group
        for row in receipt["registered_process_cleanup"]
    )


def test_keyboard_interrupt_survives_cleanup_and_receipt_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_gate_environment(monkeypatch, tmp_path)
    group_alive = True

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(
            command,
            env=env,
            stdout=stdout,
            stderr=stderr,
            first_wait_error=KeyboardInterrupt(),
        )

    def _group_exists(_pid: int) -> bool:
        return group_alive

    def _cleanup_failure(_process) -> dict[str, bool]:
        nonlocal group_alive
        group_alive = False
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(helpers, "_process_group_exists", _group_exists)
    monkeypatch.setattr(helpers, "_terminate_process_group", _cleanup_failure)
    monkeypatch.setattr(
        helpers,
        "_atomic_write_json",
        lambda _path, _payload: (_ for _ in ()).throw(OSError("receipt failed")),
    )

    with pytest.raises(KeyboardInterrupt):
        helpers.run_pytest_args(["tests/interrupted.py"])


def test_launch_barrier_prevents_child_entry_before_durable_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    marker = tmp_path / "child-entered.txt"
    leaf = tmp_path / "test_barrier_leaf.py"
    leaf.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['PQ_GATE_BARRIER_MARKER']).write_text('entered', encoding='utf-8')\n"
        "def test_leaf():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PQ_GATE_BARRIER_MARKER", str(marker))
    monkeypatch.setattr(
        helpers,
        "_register_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registration deliberately failed")
        ),
    )

    with pytest.raises(AssertionError, match="registration deliberately failed"):
        helpers.run_pytest_args([str(leaf), "-p", "no:cacheprovider"])

    assert not marker.exists()
    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "error"
    assert not helpers._process_group_exists(int(receipt["pid"]))


def test_registry_lock_contention_is_bounded_and_leaves_no_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_dir = _configure_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_CHILD_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_GATE_REGISTRY_LOCK_TIMEOUT_SECONDS", "1")
    registry = receipt_dir / "contended-registry.jsonl"
    receipt_dir.mkdir(mode=0o700)
    registry.write_text("", encoding="utf-8")
    registry.chmod(0o600)
    monkeypatch.setenv("PROPERTYQUARRY_GATE_PROCESS_REGISTRY", str(registry))
    leaf = tmp_path / "test_contended_registry_leaf.py"
    leaf.write_text("def test_leaf():\n    assert True\n", encoding="utf-8")
    holder_fd = os.open(registry, os.O_RDWR)
    helpers.fcntl.flock(holder_fd, helpers.fcntl.LOCK_EX)
    started = helpers.time.monotonic()
    try:
        with pytest.raises(AssertionError, match="process_registry_lock_timeout"):
            helpers.run_pytest_args([str(leaf), "-p", "no:cacheprovider"])
    finally:
        helpers.fcntl.flock(holder_fd, helpers.fcntl.LOCK_UN)
        os.close(holder_fd)

    assert helpers.time.monotonic() - started < 4.0
    receipt = _receipts(receipt_dir)[0]
    assert receipt["status"] == "error"
    assert not helpers._process_group_exists(int(receipt["pid"]))


def test_fresh_keyboard_interrupt_during_temp_cleanup_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_gate_environment(monkeypatch, tmp_path)

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        helpers.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        helpers.run_pytest_args(["tests/temp-interrupt.py"])


def test_fresh_keyboard_interrupt_during_receipt_write_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_gate_environment(monkeypatch, tmp_path)

    def _popen(command, *, env, stdout, stderr, **_kwargs):
        return _FakeProcess(command, env=env, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(helpers.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        helpers,
        "_atomic_write_json",
        lambda _path, _payload: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        helpers.run_pytest_args(["tests/receipt-interrupt.py"])


def test_registry_cleanup_refuses_to_signal_without_exact_start_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.jsonl"
    registry.write_text(
        json.dumps(
            {
                "invocation_id": "identity-test",
                "owner_pid": os.getpid(),
                "process_group_id": 999999,
                "process_start_ticks": 123456,
                "registered_at": "2026-07-17T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o600)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(helpers, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(helpers, "_process_start_ticks", lambda _pid: 0)
    monkeypatch.setattr(helpers.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(AssertionError, match="process_registry_identity_mismatch"):
        helpers._cleanup_registered_process_groups(registry)

    assert killed == []
