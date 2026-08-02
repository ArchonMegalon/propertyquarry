from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import provision_propertyquarry_runtime_database as runtime_database
from scripts import smoke_property_postgres_isolated as harness


RUN_ID = "0123456789abcdef"
HEADLESS_SHELL = (
    "/home/operator/.cache/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-linux64/chrome-headless-shell"
)


def test_scope_command_has_every_host_limit_and_no_uncapped_fallback() -> None:
    assert harness.PRODUCER_LOG_MAX_BYTES == 8 * 1024 * 1024
    assert harness.PRODUCER_FILE_MAX_BYTES == 8 * 1024 * 1024
    assert harness.BROWSER_PRODUCER_FILE_MAX_BYTES == 128 * 1024 * 1024
    assert harness.RUN_TEMP_MAX_BYTES == 512 * 1024 * 1024
    assert harness.RUN_TEMP_MAX_ENTRIES == 16_384
    command = harness.build_systemd_scope_command(
        systemd_run="/usr/bin/systemd-run",
        python="/docker/property/.venv/bin/python",
        script="/tmp/property-integration/scripts/smoke_property_postgres_isolated.py",
        repo_root="/tmp/property-integration",
        venv="/docker/property/.venv",
        chromium_headless_shell=HEADLESS_SHELL,
        docker_binary="/usr/bin/docker",
        run_id=RUN_ID,
    )
    assert command[:6] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=propertyquarry-postgres-browser-{RUN_ID}",
    ]
    assert f"--property=MemoryMax={1024 * 1024 * 1024}" in command
    assert "--property=MemorySwapMax=0" in command
    assert "--property=TasksMax=128" in command
    assert "--property=CPUQuota=100%" in command
    assert "--property=RuntimeMaxSec=1200s" in command
    assert command[command.index("--") + 1 : command.index("--") + 3] == [
        "/docker/property/.venv/bin/python",
        "/tmp/property-integration/scripts/smoke_property_postgres_isolated.py",
    ]
    assert command[command.index("--venv") + 1] == "/docker/property/.venv"
    assert command[command.index("--chromium-headless-shell") + 1] == HEADLESS_SHELL


def _fake_headless_shell(tmp_path: Path) -> Path:
    executable = (
        tmp_path
        / "ms-playwright"
        / "chromium_headless_shell-1223"
        / "chrome-headless-shell-linux64"
        / "chrome-headless-shell"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\x7fELF" + b"headless-shell-fixture" * 8)
    executable.chmod(0o755)
    return executable


def _assert_invalid_headless_shell(path: Path | str) -> None:
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="chromium-headless-shell-invalid",
    ):
        harness._validate_chromium_headless_shell(str(path))


def test_chromium_override_accepts_only_canonical_playwright_headless_shell(
    tmp_path: Path,
) -> None:
    executable = _fake_headless_shell(tmp_path)
    assert harness._validate_chromium_headless_shell(str(executable)) == str(executable)

    _assert_invalid_headless_shell(tmp_path / "missing" / "chrome-headless-shell")
    _assert_invalid_headless_shell("relative/chrome-headless-shell")

    full_chrome = (
        tmp_path
        / "ms-playwright"
        / "chromium-1223"
        / "chrome-linux64"
        / "chrome"
    )
    full_chrome.parent.mkdir(parents=True)
    full_chrome.write_bytes(b"\x7fELFfull-chromium")
    full_chrome.chmod(0o755)
    _assert_invalid_headless_shell(full_chrome)

    non_executable = _fake_headless_shell(tmp_path / "non-executable")
    non_executable.chmod(0o644)
    _assert_invalid_headless_shell(non_executable)

    script_disguised_as_shell = _fake_headless_shell(tmp_path / "script")
    script_disguised_as_shell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script_disguised_as_shell.chmod(0o755)
    _assert_invalid_headless_shell(script_disguised_as_shell)


def test_chromium_override_rejects_symlinked_leaf_and_ancestor(tmp_path: Path) -> None:
    executable = _fake_headless_shell(tmp_path / "real")

    linked_leaf = (
        tmp_path
        / "leaf-link"
        / "chromium_headless_shell-1223"
        / "chrome-headless-shell-linux64"
        / "chrome-headless-shell"
    )
    linked_leaf.parent.mkdir(parents=True)
    linked_leaf.symlink_to(executable)
    _assert_invalid_headless_shell(linked_leaf)

    linked_cache = tmp_path / "linked-cache"
    linked_cache.symlink_to(executable.parents[2], target_is_directory=True)
    linked_ancestor = (
        linked_cache
        / "chromium_headless_shell-1223"
        / "chrome-headless-shell-linux64"
        / "chrome-headless-shell"
    )
    _assert_invalid_headless_shell(linked_ancestor)


def _fake_worktree_and_venv(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "candidate"
    (repo / "ea" / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    venv = tmp_path / "external-venv"
    (venv / "bin").mkdir(parents=True)
    venv.chmod(0o775)
    (venv / "bin").chmod(0o775)
    config = venv / "pyvenv.cfg"
    config.write_text("home = /usr/bin\ninclude-system-site-packages = false\n", encoding="utf-8")
    config.chmod(0o664)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return repo, venv


def test_external_absolute_venv_is_allowed_but_live_repo_root_remains_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, venv = _fake_worktree_and_venv(tmp_path)
    resolved_repo, python = harness._validate_worktree(str(repo), str(venv))
    assert resolved_repo == repo.resolve()
    assert python == str(venv.resolve() / "bin" / "python")

    monkeypatch.setattr(harness, "LIVE_REPOSITORY_ROOT", repo)
    with pytest.raises(harness.IsolatedPostgresError, match="live-worktree-forbidden"):
        harness._validate_worktree(str(repo), str(venv))


def test_external_venv_rejects_symlinked_root_or_unsafe_configuration(
    tmp_path: Path,
) -> None:
    repo, venv = _fake_worktree_and_venv(tmp_path)
    linked = tmp_path / "linked-venv"
    linked.symlink_to(venv, target_is_directory=True)
    with pytest.raises(harness.IsolatedPostgresError, match="worktree-venv-invalid"):
        harness._validate_worktree(str(repo), str(linked))

    (venv / "pyvenv.cfg").chmod(0o666)
    with pytest.raises(harness.IsolatedPostgresError, match="worktree-venv-invalid"):
        harness._validate_worktree(str(repo), str(venv))


def _minimal_dependency_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "1.0",
) -> tuple[Path, Path]:
    user_base = tmp_path / "operator-base"
    dependency_path = user_base / "lib" / "python3.12" / "site-packages"
    package = dependency_path / "demo"
    dist_info = dependency_path / f"demo-{version}.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    (package / "__init__.py").write_text("VALUE = 'bound'\n", encoding="utf-8")
    (package / "__init__.py").chmod(0o664)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: demo\nVersion: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(
        "demo/__init__.py,,\n"
        f"demo-{version}.dist-info/METADATA,,\n"
        f"demo-{version}.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(harness.site, "getuserbase", lambda: str(user_base))
    monkeypatch.setattr(
        harness.site, "getusersitepackages", lambda: str(dependency_path)
    )
    monkeypatch.setattr(harness, "DEPENDENCY_PROFILE", (("demo", version),))
    return user_base, dependency_path


def test_dependency_snapshot_is_exact_private_and_not_in_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_base, dependency_path = _minimal_dependency_site(tmp_path, monkeypatch)
    snapshot = harness._dependency_source_snapshot()
    assert snapshot.source_site == dependency_path
    assert snapshot.total_bytes > 0
    assert {entry.relative_path for entry in snapshot.files} == {
        "demo/__init__.py",
        "demo-1.0.dist-info/METADATA",
        "demo-1.0.dist-info/RECORD",
    }

    run_root = tmp_path / "run"
    run_root.mkdir()
    overlay_base, overlay_site = harness._copy_dependency_overlay(
        snapshot,
        temp_root=run_root,
    )
    copied = overlay_site / "demo" / "__init__.py"
    assert copied.read_text(encoding="utf-8") == "VALUE = 'bound'\n"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o400
    assert stat.S_IMODE(overlay_base.stat().st_mode) == 0o700
    harness._verify_dependency_overlay(snapshot, overlay_site=overlay_site)

    copied.chmod(0o620)
    with pytest.raises(harness.IsolatedPostgresError, match="dependency-snapshot-invalid"):
        harness._verify_dependency_overlay(snapshot, overlay_site=overlay_site)


def test_dependency_snapshot_rejects_wrong_version_and_raw_symlink_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_base, _dependency_path = _minimal_dependency_site(tmp_path, monkeypatch)
    monkeypatch.setattr(harness, "DEPENDENCY_PROFILE", (("demo", "1.1"),))
    with pytest.raises(harness.IsolatedPostgresError, match="dependency-snapshot-invalid"):
        harness._dependency_source_snapshot()

    linked_base = tmp_path / "linked-base"
    linked_base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (linked_base / "lib").symlink_to(outside, target_is_directory=True)
    raw_site = linked_base / "lib" / "python3.12" / "site-packages"
    monkeypatch.setattr(harness.site, "getuserbase", lambda: str(linked_base))
    monkeypatch.setattr(harness.site, "getusersitepackages", lambda: str(raw_site))
    with pytest.raises(harness.IsolatedPostgresError, match="dependency-snapshot-invalid"):
        harness._dependency_source_snapshot()


def _cgroup_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    cgroup_root = tmp_path / "cgroup"
    scope = cgroup_root / "user.slice" / "safe.scope"
    scope.mkdir(parents=True)
    proc_cgroup = tmp_path / "proc-self-cgroup"
    proc_cgroup.write_text("0::/user.slice/safe.scope\n", encoding="ascii")
    (scope / "memory.max").write_text(str(1024 * 1024 * 1024), encoding="ascii")
    (scope / "memory.swap.max").write_text("0", encoding="ascii")
    (scope / "pids.max").write_text("128", encoding="ascii")
    (scope / "cpu.max").write_text("100000 100000", encoding="ascii")
    return proc_cgroup, cgroup_root, scope


def test_inside_scope_rejects_any_looser_or_unbounded_cgroup_limit(tmp_path: Path) -> None:
    proc_cgroup, cgroup_root, scope = _cgroup_fixture(tmp_path)
    assert harness.require_cgroup_limits(
        proc_cgroup=proc_cgroup, cgroup_root=cgroup_root
    ) == {
        "memory_max_bytes": 1024 * 1024 * 1024,
        "memory_swap_max_bytes": 0,
        "tasks_max": 128,
        "cpu_quota_percent": 100.0,
    }

    for filename, loose_value in (
        ("memory.max", str(1024 * 1024 * 1024 + 1)),
        ("memory.swap.max", "1"),
        ("pids.max", "129"),
        ("cpu.max", "100001 100000"),
    ):
        original = (scope / filename).read_text(encoding="ascii")
        (scope / filename).write_text(loose_value, encoding="ascii")
        with pytest.raises(harness.IsolatedPostgresError, match="limits-too-loose"):
            harness.require_cgroup_limits(
                proc_cgroup=proc_cgroup, cgroup_root=cgroup_root
            )
        (scope / filename).write_text(original, encoding="ascii")

    (scope / "memory.max").write_text("max", encoding="ascii")
    with pytest.raises(harness.IsolatedPostgresError, match="host-memory-uncapped"):
        harness.require_cgroup_limits(proc_cgroup=proc_cgroup, cgroup_root=cgroup_root)


def test_temp_environment_is_exclusive_mode_0600_and_rejects_multiline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.env"
    harness._write_env_file(path, {"DATABASE_URL": "postgresql://loopback/test", "TOKEN": "x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert harness._read_env_file(path) == {
        "DATABASE_URL": "postgresql://loopback/test",
        "TOKEN": "x",
    }
    with pytest.raises(harness.IsolatedPostgresError, match="temporary-env-invalid"):
        harness._write_env_file(tmp_path / "bad.env", {"TOKEN": "one\ntwo"})


def test_command_runner_reports_phase_and_reason_without_command_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = b"postgresql://operator:never-print@127.0.0.1/private\n"

    outcomes = (
        (
            lambda: SimpleNamespace(returncode=9, stdout=b"", stderr=secret),
            "docker-image-inspect-exit-nonzero",
        ),
        (
            lambda: SimpleNamespace(returncode=0, stdout=b"", stderr=secret),
            "docker-image-inspect-stderr-not-empty",
        ),
        (
            lambda: SimpleNamespace(returncode=0, stdout=b"x" * 65_537, stderr=b""),
            "docker-image-inspect-stdout-too-large",
        ),
    )
    for result_factory, expected in outcomes:
        monkeypatch.setattr(
            harness.subprocess,
            "run",
            lambda *_args, _factory=result_factory, **_kwargs: _factory(),
        )
        with pytest.raises(harness.IsolatedPostgresError) as failure:
            harness._run_output(
                ["/usr/bin/docker", "image", "inspect", "private-value"],
                phase="docker-image-inspect",
                environment={},
            )
        assert str(failure.value) == expected
        assert "private" not in str(failure.value)

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("private-command", 60, stderr=secret)

    monkeypatch.setattr(harness.subprocess, "run", timeout)
    with pytest.raises(harness.IsolatedPostgresError) as timeout_failure:
        harness._run_output(
            ["/usr/bin/docker", "image", "inspect", "private-value"],
            phase="docker-image-inspect",
            environment={},
        )
    assert str(timeout_failure.value) == "docker-image-inspect-timeout"

    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private detail")),
    )
    with pytest.raises(harness.IsolatedPostgresError) as execution_failure:
        harness._run_output(
            ["/usr/bin/docker", "image", "inspect", "private-value"],
            phase="docker-image-inspect",
            environment={},
        )
    assert str(execution_failure.value) == "docker-image-inspect-execution-failed"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_collision_inventory_has_exact_preflight_phases_and_collision_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    commands = harness.build_collision_preflight_commands(
        docker_binary="/usr/bin/docker",
        names=names,
        run_id=RUN_ID,
    )
    phases: list[str] = []

    def empty(
        _command: list[str],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted, timeout_seconds
        phases.append(phase)
        return b""

    monkeypatch.setattr(harness, "_run_output", empty)
    harness._assert_no_collision(
        commands,
        {},
        phase_prefix="docker-preflight",
    )
    assert phases == [
        f"docker-preflight-{suffix}" for suffix in harness.COLLISION_PHASE_SUFFIXES
    ]

    def collision(
        _command: list[str],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted, timeout_seconds
        return b"owned\n" if phase == "docker-preflight-network-name" else b""

    monkeypatch.setattr(harness, "_run_output", collision)
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="docker-preflight-network-name-collision",
    ):
        harness._assert_no_collision(
            commands,
            {},
            phase_prefix="docker-preflight",
        )


def test_database_resources_are_randomly_namespaced_and_collision_checked() -> None:
    names = harness.ResourceNames(RUN_ID)
    assert tuple(names) == (
        f"pq-pg-e2e-{RUN_ID}-db",
        f"pq-pg-e2e-{RUN_ID}-net",
        f"pq-pg-e2e-{RUN_ID}-data",
    )
    commands = harness.build_collision_preflight_commands(
        docker_binary="/usr/bin/docker", names=names, run_id=RUN_ID
    )
    assert len(commands) == 6
    rendered = "\n".join(" ".join(command) for command in commands)
    assert rendered.count(f"{harness.RUN_LABEL_KEY}={RUN_ID}") == 3
    assert names.container in rendered
    assert names.network in rendered
    assert names.volume in rendered
    for forbidden in (
        "propertyquarry-api",
        "propertyquarry-scheduler",
        "propertyquarry-db-live",
        "property_propertyquarry",
    ):
        assert forbidden not in rendered


def test_postgres_command_stays_on_internal_network_and_is_strictly_capped(
    tmp_path: Path,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    command = harness.build_postgres_run_command(
        docker_binary="/usr/bin/docker",
        names=names,
        run_id=RUN_ID,
        image_id="sha256:" + "a" * 64,
        db_env_file=str(tmp_path / "postgres.env"),
    )
    assert command[:4] == [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "run",
    ]
    expected_pairs = {
        "--name": names.container,
        "--network": names.network,
        "--cpus": "1.0",
        "--memory": "512m",
        "--memory-swap": "512m",
        "--pids-limit": "128",
        "--restart": "no",
        "--pull": "never",
    }
    for option, value in expected_pairs.items():
        assert command[command.index(option) + 1] == value
    assert "--publish" not in command
    assert "-p" not in command
    assert command[-1] == "sha256:" + "a" * 64
    assert command[command.index("--mount") + 1] == (
        f"type=volume,source={names.volume},target=/var/lib/postgresql/data"
    )
    assert "--health-cmd" in command
    rendered = " ".join(command)
    for forbidden in (
        "latest",
        "compose",
        "--build",
        "prune",
        "scheduler",
        "incoming_property_tours",
        "propertyquarry-web-runtime",
    ):
        assert forbidden not in rendered


def test_container_address_inspection_is_bound_to_the_exact_internal_network() -> None:
    names = harness.ResourceNames(RUN_ID)
    command = harness.build_container_address_inspect_command(
        docker_binary="/usr/bin/docker", names=names
    )
    assert command == [
        "/usr/bin/docker",
        "--host",
        harness.DOCKER_HOST,
        "container",
        "inspect",
        "--format",
        (
            f'{{{{with index .NetworkSettings.Networks "{names.network}"}}}}'
            "{{.IPAddress}} {{.NetworkID}}{{end}}"
        ),
        names.container,
    ]


def test_admission_capacity_owner_bootstrap_is_fixed_argv_and_least_privilege() -> None:
    names = harness.ResourceNames(RUN_ID)
    role = "propertyquarry_admission_capacity_owner"
    create, verify = harness.build_admission_capacity_owner_commands(
        docker_binary="/usr/bin/docker",
        names=names,
        role_name=role,
    )
    expected_prefix = [
        "/usr/bin/docker",
        "--host",
        harness.DOCKER_HOST,
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
        "--command",
    ]
    assert create[:-1] == expected_prefix
    assert verify[:-1] == expected_prefix
    assert create[-1] == (
        'CREATE ROLE "propertyquarry_admission_capacity_owner" WITH '
        "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS"
    )
    assert "pg_catalog.pg_auth_members" in verify[-1]
    assert f"owner_role.rolname = '{role}'" in verify[-1]
    assert all(
        value not in create + verify
        for value in ("sh", "bash", "-c", "--env", "DATABASE_URL", "POSTGRES_PASSWORD")
    )

    for invalid in (
        "PropertyQuarryOwner",
        "owner; DROP ROLE postgres",
        "owner\npostgres",
        "a" * 64,
        "",
    ):
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="admission-capacity-owner-role-invalid",
        ):
            harness.build_admission_capacity_owner_commands(
                docker_binary="/usr/bin/docker",
                names=names,
                role_name=invalid,
            )


def test_admission_capacity_owner_bootstrap_verifies_exact_safe_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    observed: list[tuple[str, list[str]]] = []

    def successful(
        command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted, timeout_seconds
        observed.append((phase, list(command)))
        if phase == "docker-role-bootstrap":
            return b"CREATE ROLE\n"
        assert phase == "docker-role-verify"
        return b"f|f|f|f|f|f|f|0\n"

    monkeypatch.setattr(harness, "_run_output", successful)
    harness._provision_admission_capacity_owner_role(
        docker_binary="/usr/bin/docker",
        names=names,
        role_name="propertyquarry_admission_capacity_owner",
        environment={},
    )
    assert [phase for phase, _command in observed] == [
        "docker-role-bootstrap",
        "docker-role-verify",
    ]

    unsafe_rows = [
        "|".join("t" if index == unsafe_index else "f" for index in range(7))
        + "|0"
        for unsafe_index in range(7)
    ] + ["f|f|f|f|f|f|f|1", ""]
    for unsafe_row in unsafe_rows:
        def unsafe_role(
            _command: list[str] | tuple[str, ...],
            *,
            phase: str,
            environment: dict[str, str],
            accepted: frozenset[int] = frozenset({0}),
            timeout_seconds: int = 60,
            _unsafe_row: str = unsafe_row,
        ) -> bytes:
            del environment, accepted, timeout_seconds
            if phase == "docker-role-bootstrap":
                return b"CREATE ROLE\n"
            return (_unsafe_row + "\n").encode("ascii")

        monkeypatch.setattr(harness, "_run_output", unsafe_role)
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="docker-role-verification-mismatch",
        ):
            harness._provision_admission_capacity_owner_role(
                docker_binary="/usr/bin/docker",
                names=names,
                role_name="propertyquarry_admission_capacity_owner",
                environment={},
            )

    def unexpected_create(
        _command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del _command, phase, environment, accepted, timeout_seconds
        return b"unexpected\n"

    monkeypatch.setattr(harness, "_run_output", unexpected_create)
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="docker-role-bootstrap-output-invalid",
    ):
        harness._provision_admission_capacity_owner_role(
            docker_binary="/usr/bin/docker",
            names=names,
            role_name="propertyquarry_admission_capacity_owner",
            environment={},
        )


def test_schema_runtime_role_bootstrap_is_fixed_argv_and_least_privilege() -> None:
    names = harness.ResourceNames(RUN_ID)
    create, verify = harness.build_schema_runtime_role_commands(
        docker_binary="/usr/bin/docker",
        names=names,
    )
    expected_prefix = [
        "/usr/bin/docker",
        "--host",
        harness.DOCKER_HOST,
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
        "--command",
    ]
    assert create[:-1] == expected_prefix
    assert verify[:-1] == expected_prefix
    assert harness.DISPOSABLE_SCHEMA_RUNTIME_ROLES == (
        "propertyquarry_api",
        "propertyquarry_scheduler",
        "propertyquarry_worker",
    )
    assert set(harness.DISPOSABLE_SCHEMA_RUNTIME_ROLES) == set(
        runtime_database.RUNTIME_ROLES
    )
    safe_posture = (
        "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS"
    )
    assert create[-1] == "; ".join(
        f'CREATE ROLE "{role_name}" WITH {safe_posture}'
        for role_name in harness.DISPOSABLE_SCHEMA_RUNTIME_ROLES
    )
    assert "pg_catalog.pg_auth_members" in verify[-1]
    assert "membership.member = role.oid OR membership.roleid = role.oid" in verify[-1]
    assert verify[-1].endswith("ORDER BY role.rolname")
    for role_name in harness.DISPOSABLE_SCHEMA_RUNTIME_ROLES:
        assert create[-1].count(f'"{role_name}"') == 1
        assert verify[-1].count(f"'{role_name}'") == 1
    assert all(
        value not in create + verify
        for value in (
            "sh",
            "bash",
            "-c",
            "--env",
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
        )
    )


def test_schema_runtime_role_bootstrap_verifies_exact_safe_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    observed: list[tuple[str, list[str]]] = []
    safe_rows = "\n".join(
        f"{role_name}|f|f|f|f|f|f|f|0"
        for role_name in harness.DISPOSABLE_SCHEMA_RUNTIME_ROLES
    )

    def successful(
        command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted, timeout_seconds
        observed.append((phase, list(command)))
        if phase == "docker-schema-roles-bootstrap":
            return b"CREATE ROLE\nCREATE ROLE\nCREATE ROLE\n"
        assert phase == "docker-schema-roles-verify"
        return (safe_rows + "\n").encode("ascii")

    monkeypatch.setattr(harness, "_run_output", successful)
    harness._provision_schema_runtime_roles(
        docker_binary="/usr/bin/docker",
        names=names,
        environment={},
    )
    assert [phase for phase, _command in observed] == [
        "docker-schema-roles-bootstrap",
        "docker-schema-roles-verify",
    ]

    invalid_verification_rows = (
        safe_rows.replace("|f|f|f|f|f|f|f|0", "|t|f|f|f|f|f|f|0", 1),
        safe_rows.replace("|0", "|1", 1),
        "\n".join(safe_rows.splitlines()[:-1]),
        safe_rows + "\npropertyquarry_unexpected|f|f|f|f|f|f|f|0",
        "\n".join(reversed(safe_rows.splitlines())),
        "",
    )
    for invalid_rows in invalid_verification_rows:
        def invalid_verification(
            _command: list[str] | tuple[str, ...],
            *,
            phase: str,
            environment: dict[str, str],
            accepted: frozenset[int] = frozenset({0}),
            timeout_seconds: int = 60,
            _invalid_rows: str = invalid_rows,
        ) -> bytes:
            del _command, environment, accepted, timeout_seconds
            if phase == "docker-schema-roles-bootstrap":
                return b"CREATE ROLE\nCREATE ROLE\nCREATE ROLE\n"
            assert phase == "docker-schema-roles-verify"
            return (_invalid_rows + "\n").encode("ascii")

        monkeypatch.setattr(harness, "_run_output", invalid_verification)
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="docker-schema-roles-verification-mismatch",
        ):
            harness._provision_schema_runtime_roles(
                docker_binary="/usr/bin/docker",
                names=names,
                environment={},
            )

    def unexpected_create(
        _command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del _command, phase, environment, accepted, timeout_seconds
        return b"CREATE ROLE\nCREATE ROLE\n"

    monkeypatch.setattr(harness, "_run_output", unexpected_create)
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="docker-schema-roles-bootstrap-output-invalid",
    ):
        harness._provision_schema_runtime_roles(
            docker_binary="/usr/bin/docker",
            names=names,
            environment={},
        )


def test_disposable_api_admission_dsns_are_distinct_loopback_credentials() -> None:
    admission_password = "A" * 32
    ingress_password = "C" * 32
    admin = f"postgresql://postgres:{'B' * 32}@127.0.0.1:15432/postgres"
    admission = (
        "postgresql://propertyquarry_api_admission:"
        f"{admission_password}@127.0.0.1:15432/postgres"
    )
    ingress = (
        "postgresql://propertyquarry_api_ingress:"
        f"{ingress_password}@127.0.0.1:15432/postgres"
    )
    harness._validate_disposable_admission_dsns(
        admin_database_url=admin,
        admission_database_url=admission,
        admission_password=admission_password,
        ingress_database_url=ingress,
        ingress_password=ingress_password,
    )

    invalid = (
        (admin, admission, "short", ingress, ingress_password),
        (admin, admission, admission_password, ingress, "short"),
        ("\x00" + admin, admission, admission_password, ingress, ingress_password),
        (admin + "\t", admission, admission_password, ingress, ingress_password),
        (admin, "\r" + admission, admission_password, ingress, ingress_password),
        (admin, admission, admission_password, ingress.replace("@", "\n@"), ingress_password),
        (" " + admin, admission, admission_password, ingress, ingress_password),
        (admin.replace("127.0.0.1", "db.example"), admission, admission_password, ingress, ingress_password),
        (admin, admission.replace("127.0.0.1", "db.example"), admission_password, ingress, ingress_password),
        (admin, admission, admission_password, ingress.replace("127.0.0.1", "db.example"), ingress_password),
        (admin, admin, admission_password, ingress, ingress_password),
        (admin, admission, admission_password, admission, ingress_password),
        (admin, admission.replace(":15432", ":15433"), admission_password, ingress, ingress_password),
        (admin, admission, admission_password, ingress.replace(":15432", ":15433"), ingress_password),
        (admin, admission + "?sslmode=disable", admission_password, ingress, ingress_password),
        (admin, admission, admission_password, ingress + "?sslmode=disable", ingress_password),
    )
    for (
        admin_url,
        admission_url,
        supplied_admission_password,
        ingress_url,
        supplied_ingress_password,
    ) in invalid:
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="api-admission-role-dsn-invalid",
        ):
            harness._validate_disposable_admission_dsns(
                admin_database_url=admin_url,
                admission_database_url=admission_url,
                admission_password=supplied_admission_password,
                ingress_database_url=ingress_url,
                ingress_password=supplied_ingress_password,
            )


def test_postgres_scram_verifier_is_deterministic_and_never_contains_cleartext() -> None:
    password = "CleartextAdmissionPasswordValue01"
    salt = bytes(range(harness.POSTGRES_SCRAM_SALT_BYTES))
    verifier = harness._postgres_scram_verifier(password, salt=salt)
    assert verifier == harness._postgres_scram_verifier(password, salt=salt)
    assert harness.POSTGRES_SCRAM_VERIFIER_RE.fullmatch(verifier) is not None
    assert verifier.startswith("SCRAM-SHA-256$4096:AAECAwQFBgcICQoLDA0ODw==$")
    assert password not in verifier

    with pytest.raises(
        harness.IsolatedPostgresError,
        match="api-admission-role-provision-failed",
    ):
        harness._postgres_scram_verifier(password, salt=b"short")


def test_libpq_environment_is_closed_before_any_direct_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_environment = {
        "PATH": "/usr/bin:/bin",
        "PGHOSTADDR": "203.0.113.9",
        "PGOPTIONS": "-c search_path=attacker",
        "PGSERVICEFILE": "/tmp/attacker-service.conf",
        "PGFUTURE_OVERRIDE": "attacker",
    }
    assert harness._clear_libpq_environment(isolated_environment) == (
        "PGFUTURE_OVERRIDE",
        "PGHOSTADDR",
        "PGOPTIONS",
        "PGSERVICEFILE",
    )
    assert isolated_environment == {"PATH": "/usr/bin:/bin"}
    harness._require_closed_libpq_environment(isolated_environment)

    password = "E" * 32
    ingress_password = "G" * 32
    admin = f"postgresql://postgres:{'F' * 32}@127.0.0.1:15432/postgres"
    admission = (
        "postgresql://propertyquarry_api_admission:"
        f"{password}@127.0.0.1:15432/postgres"
    )
    ingress = (
        "postgresql://propertyquarry_api_ingress:"
        f"{ingress_password}@127.0.0.1:15432/postgres"
    )
    connected: list[bool] = []
    for key in tuple(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.9")
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="libpq-environment-not-closed",
    ):
        harness._provision_api_database_roles(
            admin_database_url=admin,
            admission_database_url=admission,
            admission_password=password,
            ingress_database_url=ingress,
            ingress_password=ingress_password,
            connect=lambda *_args, **_kwargs: connected.append(True),
        )
    assert connected == []
    assert harness._clear_libpq_environment() == ("PGHOSTADDR",)
    harness._require_closed_libpq_environment()


def test_disposable_api_admission_role_is_exactly_granted_and_strictly_probed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "C" * 32
    ingress_password = "E" * 32
    admin = f"postgresql://postgres:{'D' * 32}@127.0.0.1:15432/postgres"
    admission = (
        "postgresql://propertyquarry_api_admission:"
        f"{password}@127.0.0.1:15432/postgres"
    )
    ingress = (
        "postgresql://propertyquarry_api_ingress:"
        f"{ingress_password}@127.0.0.1:15432/postgres"
    )
    statements: list[object] = []

    class Cursor:
        row: object = None
        rows: list[tuple[object, ...]] = []

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, _params: object = ()) -> None:
            statements.append(statement)
            normalized = " ".join(statement.split()) if isinstance(statement, str) else ""
            self.row = None
            self.rows = []
            if normalized.startswith("SELECT current_database()"):
                self.row = ("postgres", "postgres", True, True)
            elif normalized.startswith("SELECT namespace.nspname"):
                self.rows = [("public",)]
            elif normalized.startswith("SELECT role.rolcanlogin"):
                self.row = (True, False, False, False, False, False, False, 0)

        def fetchone(self) -> object:
            return self.row

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

    class Connection:
        committed = False

        def __init__(self) -> None:
            self._cursor = Cursor()

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

    provision_connection = Connection()

    def connect_admin(database_url: str, **kwargs: object) -> Connection:
        assert database_url == admin
        assert kwargs == {
            "autocommit": False,
            "connect_timeout": 5,
            "hostaddr": "127.0.0.1",
            "sslmode": "disable",
            "options": "",
            "application_name": "propertyquarry-isolated-admission-provision",
            "target_session_attrs": "read-write",
        }
        return provision_connection

    harness._provision_api_database_roles(
        admin_database_url=admin,
        admission_database_url=admission,
        admission_password=password,
        ingress_database_url=ingress,
        ingress_password=ingress_password,
        connect=connect_admin,
    )
    assert provision_connection.committed is True

    migration_connection = Connection()

    def connect_migration(database_url: str, **kwargs: object) -> Connection:
        assert database_url == admin
        assert kwargs == {
            "autocommit": True,
            "connect_timeout": 5,
            "hostaddr": "127.0.0.1",
            "sslmode": "disable",
            "options": "",
            "application_name": "propertyquarry-isolated-ingress-schema-migrate",
            "target_session_attrs": "read-write",
        }
        return migration_connection

    harness._migrate_api_ingress_schema(
        admin_database_url=admin,
        connect=connect_migration,
    )
    rendered = "\n".join(statement for statement in statements if isinstance(statement, str))
    statement_reprs = "\n".join(repr(statement) for statement in statements)
    assert password not in statement_reprs
    assert ingress_password not in statement_reprs
    assert "SET LOCAL log_statement = 'none'" in rendered
    assert "SET LOCAL log_min_error_statement = 'panic'" in rendered
    assert "REVOKE ALL PRIVILEGES ON DATABASE postgres FROM PUBLIC" in rendered
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in rendered
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in rendered
    assert "GRANT SELECT ON TABLE propertyquarry_admission_capacity_state" in rendered
    assert (
        "ON TABLE public.propertyquarry_ingress_admission_capacity" in rendered
    )
    assert "CREATE TABLE propertyquarry_ingress_quota_buckets" in rendered
    assert "SET LOCAL ROLE postgres" in rendered
    assert "FROM propertyquarry_api_admission" in rendered
    assert "FROM propertyquarry_api_ingress" in rendered

    verify_connection = Connection()

    def connect_admission(database_url: str, **kwargs: object) -> Connection:
        assert database_url == admission
        assert kwargs == {
            "autocommit": True,
            "connect_timeout": 5,
            "hostaddr": "127.0.0.1",
            "sslmode": "disable",
            "options": "",
            "application_name": "propertyquarry-isolated-api-admission-proof",
            "target_session_attrs": "read-write",
        }
        return verify_connection

    probed: list[object] = []

    def probe(cursor: object, *, require_least_privilege: bool) -> None:
        assert cursor is verify_connection._cursor
        assert require_least_privilege is True
        probed.append(cursor)

    harness._verify_api_admission_role(
        admission_database_url=admission,
        connect=connect_admission,
        probe=probe,
    )
    assert probed == [verify_connection._cursor]

    class IngressCursor(Cursor):
        def execute(self, statement: object, _params: object = ()) -> None:
            normalized = " ".join(statement.split()) if isinstance(statement, str) else ""
            self.row = None
            self.rows = []
            if normalized.startswith("SELECT current_database(), current_user"):
                self.row = (
                    "postgres",
                    harness.DISPOSABLE_API_INGRESS_ROLE,
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    0,
                )
            elif normalized.startswith("SELECT has_database_privilege"):
                self.row = (True,) * 17
            elif normalized.startswith("SELECT capacity_key"):
                self.rows = [
                    ("lease", 0, 100_000, 1),
                    ("quota", 0, 1_000_000, 1),
                ]

    ingress_connection = Connection()
    ingress_connection._cursor = IngressCursor()

    def connect_ingress(database_url: str, **kwargs: object) -> Connection:
        assert database_url == ingress
        assert kwargs == {
            "autocommit": True,
            "connect_timeout": 5,
            "hostaddr": "127.0.0.1",
            "sslmode": "disable",
            "options": "",
            "application_name": "propertyquarry-isolated-api-ingress-proof",
            "target_session_attrs": "read-write",
        }
        return ingress_connection

    harness._verify_api_ingress_role(
        ingress_database_url=ingress,
        connect=connect_ingress,
    )

    def interrupted(_cursor: object, *, require_least_privilege: bool) -> None:
        assert require_least_privilege is True
        raise harness.IsolatedPostgresError("internal-watchdog-expired")

    with pytest.raises(
        harness.IsolatedPostgresError,
        match="internal-watchdog-expired",
    ):
        harness._verify_api_admission_role(
            admission_database_url=admission,
            connect=connect_admission,
            probe=interrupted,
        )
    ingress_connection._cursor = Cursor()
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="api-ingress-role-verification-failed",
    ):
        harness._verify_api_ingress_role(
            ingress_database_url=ingress,
            connect=connect_ingress,
        )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_internal_container_address_requires_exact_network_id_and_rfc1918_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    network_id = "a" * 64

    def healthy_attachment(
        _command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted, timeout_seconds
        if phase == "docker-health-inspect":
            return b"running healthy\n"
        assert phase == "docker-address-inspect"
        return f"172.28.0.2 {network_id}\n".encode("ascii")

    monkeypatch.setattr(harness, "_run_output", healthy_attachment)
    assert harness._wait_for_postgres(
        docker_binary="/usr/bin/docker",
        names=names,
        expected_network_id=network_id,
        environment={},
    ) == "172.28.0.2"

    for value in ("127.0.0.1", "169.254.1.1", "203.0.113.10", "not-an-ip"):
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="docker-address-output-invalid",
        ):
            harness._private_container_ipv4(value)

    def wrong_network(
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> bytes:
        if kwargs["phase"] == "docker-health-inspect":
            return b"running healthy\n"
        del command
        return f"172.28.0.2 {'b' * 64}\n".encode("ascii")

    monkeypatch.setattr(harness, "_run_output", wrong_network)
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="docker-address-output-invalid",
    ):
        harness._wait_for_postgres(
            docker_binary="/usr/bin/docker",
            names=names,
            expected_network_id=network_id,
            environment={},
        )


def test_loopback_database_relay_is_bounded_bidirectional_and_joins() -> None:
    echo_threads: list[threading.Thread] = []

    def connector(address: tuple[str, int], timeout: float) -> socket.socket:
        assert address == ("172.28.0.2", harness.POSTGRES_CONTAINER_PORT_NUMBER)
        assert timeout == harness.DATABASE_RELAY_CONNECT_TIMEOUT_SECONDS
        relay_side, echo_side = socket.socketpair()

        def echo() -> None:
            with echo_side:
                while True:
                    payload = echo_side.recv(harness.DATABASE_RELAY_BUFFER_BYTES)
                    if not payload:
                        return
                    echo_side.sendall(payload)

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        echo_threads.append(thread)
        return relay_side

    relay = harness._LoopbackDatabaseRelay("172.28.0.2", connector=connector)
    port = relay.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"propertyquarry-relay-probe")
            assert client.recv(64) == b"propertyquarry-relay-probe"
        relay.assert_healthy()
    finally:
        relay.stop()
    assert relay._accept_thread is not None
    assert not relay._accept_thread.is_alive()
    assert relay._workers == set()
    for thread in echo_threads:
        thread.join(2)
        assert not thread.is_alive()
    assert 1 <= harness.DATABASE_RELAY_MAX_CONNECTIONS <= 8
    assert harness.DATABASE_RELAY_BACKLOG <= 16


def test_loopback_database_relay_fails_closed_on_backend_connect_error() -> None:
    def connector(_address: tuple[str, int], _timeout: float) -> socket.socket:
        raise OSError("private backend detail")

    relay = harness._LoopbackDatabaseRelay("172.28.0.2", connector=connector)
    port = relay.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            deadline = time.monotonic() + 2
            while not relay._failed.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
        with pytest.raises(
            harness.IsolatedPostgresError,
            match="database-relay-runtime-failed",
        ):
            relay.assert_healthy()
    finally:
        relay.stop()


def test_database_volume_is_unique_labeled_tmpfs_with_256m_hard_cap() -> None:
    names = harness.ResourceNames(RUN_ID)
    command = harness.build_volume_create_command(
        docker_binary="/usr/bin/docker", names=names, run_id=RUN_ID
    )
    assert command == [
        "/usr/bin/docker",
        "--host",
        harness.DOCKER_HOST,
        "volume",
        "create",
        "--driver",
        "local",
        "--label",
        f"{harness.RUN_LABEL_KEY}={RUN_ID}",
        "--opt",
        "type=tmpfs",
        "--opt",
        "device=tmpfs",
        "--opt",
        "o=size=268435456,mode=0700,nosuid,nodev,noexec",
        names.volume,
    ]
    size_option = command[command.index("--opt", command.index("device=tmpfs")) + 1]
    assert f"size={harness.POSTGRES_VOLUME_MAX_BYTES}" in size_option
    assert harness.POSTGRES_VOLUME_MAX_BYTES <= 256 * 1024 * 1024


def test_exact_cleanup_checks_its_label_and_removes_only_owned_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    calls: list[tuple[str, ...]] = []
    phases: list[str] = []
    timeouts: list[int] = []
    existing = {"container", "network", "volume"}

    def fake_run(
        command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted
        command = tuple(command)
        calls.append(command)
        phases.append(phase)
        timeouts.append(timeout_seconds)
        if "inspect" in command:
            return f"{RUN_ID}\n".encode("ascii")
        kind = command[3] if len(command) > 3 else ""
        if len(command) > 4 and command[4] == "ls":
            return b"owned\n" if kind in existing else b""
        if kind == "container" and command[4:6] == ("rm", "--force"):
            existing.discard(kind)
        elif kind in {"network", "volume"} and command[4] == "rm":
            existing.discard(kind)
        return b""

    monkeypatch.setattr(harness, "_run_output", fake_run)
    harness._cleanup_resources(
        docker_binary="/usr/bin/docker",
        names=names,
        run_id=RUN_ID,
        created={"container", "network", "volume"},
        environment={},
    )
    removal_commands = [
        command
        for command in calls
        if command[3:6] in {
            ("container", "rm", "--force"),
            ("network", "rm", names.network),
            ("volume", "rm", names.volume),
        }
    ]
    assert (
        "/usr/bin/docker",
        "--host",
        harness.DOCKER_HOST,
        "container",
        "rm",
        "--force",
        names.container,
    ) in removal_commands
    assert any(command[3:] == ("network", "rm", names.network) for command in calls)
    assert any(command[3:] == ("volume", "rm", names.volume) for command in calls)
    assert all("prune" not in command for command in calls)
    assert all("propertyquarry-api" not in command for command in calls)
    assert harness.CLEANUP_COMMAND_TIMEOUT_SECONDS == 15
    assert set(timeouts) == {15}
    assert {
        "cleanup-container-presence",
        "cleanup-container-label",
        "cleanup-container-remove",
        "cleanup-network-presence",
        "cleanup-network-label",
        "cleanup-network-remove",
        "cleanup-volume-presence",
        "cleanup-volume-label",
        "cleanup-volume-remove",
    }.issubset(phases)
    assert all(
        phase.startswith("cleanup-") and phase in harness.COMMAND_PHASES
        for phase in phases
    )


def test_cleanup_attempts_full_inventory_then_preserves_first_exact_phase_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = harness.ResourceNames(RUN_ID)
    phases: list[str] = []

    def fake_run(
        _command: list[str] | tuple[str, ...],
        *,
        phase: str,
        environment: dict[str, str],
        accepted: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 60,
    ) -> bytes:
        del environment, accepted
        phases.append(phase)
        assert timeout_seconds == 15
        if phase == "cleanup-container-remove":
            raise harness.IsolatedPostgresError(
                "cleanup-container-remove-exit-nonzero"
            )
        if phase.startswith("cleanup-inventory-"):
            return b""
        if phase.endswith("-label"):
            return f"{RUN_ID}\n".encode("ascii")
        if phase.endswith("-presence"):
            return b"owned\n"
        return b""

    monkeypatch.setattr(harness, "_run_output", fake_run)
    with pytest.raises(
        harness.IsolatedPostgresError,
        match="cleanup-container-remove-exit-nonzero",
    ):
        harness._cleanup_resources(
            docker_binary="/usr/bin/docker",
            names=names,
            run_id=RUN_ID,
            created={"container", "network", "volume"},
            environment={},
        )
    assert "cleanup-network-remove" in phases
    assert "cleanup-volume-remove" in phases
    assert len(phases) == harness.CLEANUP_DOCKER_COMMAND_COUNT
    assert phases[-6:] == [
        f"cleanup-inventory-{suffix}"
        for suffix in harness.COLLISION_PHASE_SUFFIXES
    ]


def test_prod_runtime_has_fresh_product_secrets_and_only_temp_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/unsafe/inherited/cache")
    monkeypatch.setenv(
        harness.CHROMIUM_EXECUTABLE_ENV,
        "/unsafe/inherited/full-chromium",
    )
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    dependency_overlay = tmp_path / "dependency-overlay"
    dependency_overlay.mkdir()
    first = harness._runtime_environment(
        repo_root=tmp_path / "candidate",
        temp_root=tmp_path / "first",
        database_url="postgresql://loopback/first",
        admission_database_url="postgresql://admission/first",
        ingress_database_url="postgresql://ingress/first",
        api_token="api-one",
        signing_secret="sign-one",
        identity_session_secret="identity-one-" + "a" * 32,
        erasure_secret="erase-one",
        chromium_headless_shell=HEADLESS_SHELL,
        dependency_overlay_base=dependency_overlay,
    )
    second = harness._runtime_environment(
        repo_root=tmp_path / "candidate",
        temp_root=tmp_path / "second",
        database_url="postgresql://loopback/second",
        admission_database_url="postgresql://admission/second",
        ingress_database_url="postgresql://ingress/second",
        api_token="api-two",
        signing_secret="sign-two",
        identity_session_secret="identity-two-" + "b" * 32,
        erasure_secret="erase-two",
        chromium_headless_shell=HEADLESS_SHELL,
        dependency_overlay_base=dependency_overlay,
    )
    assert first["EA_RUNTIME_MODE"] == "prod"
    assert first["EA_STORAGE_BACKEND"] == "postgres"
    assert first["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"] == (
        "postgresql://admission/first"
    )
    assert second["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"] == (
        "postgresql://admission/second"
    )
    assert first["PROPERTYQUARRY_API_INGRESS_DATABASE_URL"] == (
        "postgresql://ingress/first"
    )
    assert second["PROPERTYQUARRY_API_INGRESS_DATABASE_URL"] == (
        "postgresql://ingress/second"
    )
    assert first["PROPERTYQUARRY_IDENTITY_SESSION_SECRET"] == (
        "identity-one-" + "a" * 32
    )
    assert second["PROPERTYQUARRY_IDENTITY_SESSION_SECRET"] == (
        "identity-two-" + "b" * 32
    )
    assert first["PROPERTYQUARRY_IDENTITY_SESSION_SECRET"] != second[
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET"
    ]
    assert first["PROPERTYQUARRY_IDENTITY_SESSION_SECRET"] not in {
        first["EA_API_TOKEN"],
        first["EA_SIGNING_SECRET"],
        first["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"],
    }
    assert first["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] == "erase-one"
    assert second["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] == "erase-two"
    assert first["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] != second[
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
    ]
    assert first[harness.CHROMIUM_EXECUTABLE_ENV] == HEADLESS_SHELL
    overlay_site = (
        dependency_overlay
        / "lib"
        / f"python{harness.sys.version_info.major}.{harness.sys.version_info.minor}"
        / "site-packages"
    )
    assert first["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path / "candidate" / "ea"),
        str(overlay_site),
    ]
    assert first["PYTHONUSERBASE"] == str(dependency_overlay)
    assert first["PYTHONPATH"].index(str(tmp_path / "candidate" / "ea")) < first[
        "PYTHONPATH"
    ].index(str(overlay_site))
    assert "PLAYWRIGHT_BROWSERS_PATH" not in first
    assert "PROPERTYQUARRY_PLAYWRIGHT_BROWSERS_PATH" not in first


def test_isolated_postgres_lane_uses_combined_schema_gate() -> None:
    source = Path("scripts/smoke_property_postgres_isolated.py").read_text(
        encoding="utf-8"
    )
    for action in ("migrate", "check"):
        assert source.count(
            f'[python, "-m", "app.product.propertyquarry_schema", "{action}"]'
        ) == 1
        assert (
            f'[python, "-m", "app.product.property_search_schema", "{action}"]'
            not in source
        )
    tree = ast.parse(source)
    execute_guarded = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_guarded"
    )
    calls = sorted(
        (node for node in ast.walk(execute_guarded) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    admission_owner_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_provision_admission_capacity_owner_role"
    )
    schema_roles_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_provision_schema_runtime_roles"
    )
    schema_migrate_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_run_host"
        and any(
            keyword.arg == "phase"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "schema-migrate"
            for keyword in node.keywords
        )
    )
    assert admission_owner_line < schema_roles_line < schema_migrate_line


def test_browser_launcher_has_one_exact_headless_shell_override_and_static_args() -> None:
    browser_test = Path("tests/e2e/test_propertyquarry_postgres_browser.py").read_text(
        encoding="utf-8"
    )
    assert browser_test.count("playwright.chromium.launch(") == 1
    assert "executable_path=chromium_headless_shell" in browser_test
    assert "args=list(POSTGRES_CHROMIUM_ARGS)" in browser_test
    for forbidden in (
        "playwright.chromium.executable_path",
        "playwright.firefox",
        "playwright.webkit",
        "channel=",
        "PROPERTYQUARRY_PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
    ):
        assert forbidden not in browser_test
    assert harness.POSTGRES_CHROMIUM_ARGS == (
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-proxy-server",
    )
    assert all("=" not in argument for argument in harness.POSTGRES_CHROMIUM_ARGS)


def test_operator_guide_requires_explicit_portable_browser_path_and_role_proof() -> None:
    guide = Path("docs/PROPERTYQUARRY_ISOLATED_POSTGRES_BROWSER_E2E.md").read_text(
        encoding="utf-8"
    )
    assert "--chromium-headless-shell" in guide
    assert "${PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE}" in guide
    assert "performs no browser discovery" in guide
    assert "propertyquarry_api_admission" in guide
    assert "strict least-privilege probe" in guide
    assert "selects the installed Playwright executable by default" not in guide


def test_every_subprocess_callsite_binds_an_allowlisted_observability_phase() -> None:
    source = Path(harness.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    required_keyword = {
        "_run_output": "phase",
        "_run_host": "phase",
        "_assert_no_collision": "phase_prefix",
    }
    missing: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        keyword = required_keyword.get(node.func.id)
        if keyword is None:
            continue
        if keyword not in {argument.arg for argument in node.keywords}:
            missing.append((node.lineno, node.func.id, keyword))
    assert missing == []
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    ]
    assert len(popen_calls) == 2
    for call in popen_calls:
        start_new_session = next(
            argument.value
            for argument in call.keywords
            if argument.arg == "start_new_session"
        )
        assert isinstance(start_new_session, ast.Constant)
        assert start_new_session.value is True
    assert {
        "docker-image-inspect",
        "docker-network-create",
        "docker-volume-create",
        "docker-container-create",
        "docker-health-inspect",
        "docker-address-inspect",
        "docker-role-bootstrap",
        "docker-role-verify",
        "docker-schema-roles-bootstrap",
        "docker-schema-roles-verify",
        "schema-migrate",
        "schema-check",
        "session-bootstrap",
        "browser-test",
        "cleanup-container-remove",
    }.issubset(harness.COMMAND_PHASES)
    assert 1 <= harness.CLEANUP_COMMAND_TIMEOUT_SECONDS <= 15


def test_scoped_diagnostic_allowlist_is_a_closed_phase_reason_schema() -> None:
    assert harness.OUTPUT_FAILURE_REASONS == frozenset(
        {
            "execution-failed",
            "timeout",
            "exit-nonzero",
            "stderr-not-empty",
            "stdout-too-large",
        }
    )
    assert harness.HOST_FAILURE_REASONS == frozenset(
        {"execution-failed", "exit-nonzero", "log-invalid"}
    )
    assert harness.COMMAND_FAILURE_REASONS == frozenset(
        {
            "execution-failed",
            "timeout",
            "exit-nonzero",
            "stderr-not-empty",
            "stdout-too-large",
            "collision",
            "log-invalid",
        }
    )
    assert harness.HOST_COMMAND_PHASES == frozenset(
        {"schema-migrate", "schema-check", "session-bootstrap", "browser-test"}
    )
    assert harness.OUTPUT_COMMAND_PHASES == (
        harness.COMMAND_PHASES - harness.HOST_COMMAND_PHASES
    )
    assert harness.COLLISION_COMMAND_PHASES == frozenset(
        {
            *(
                f"docker-preflight-{suffix}"
                for suffix in harness.COLLISION_PHASE_SUFFIXES
            ),
            *(
                f"cleanup-inventory-{suffix}"
                for suffix in harness.COLLISION_PHASE_SUFFIXES
            ),
        }
    )
    expected_semantic = frozenset(
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
            "docker-schema-roles-bootstrap-output-invalid",
            "docker-schema-roles-verification-mismatch",
            "api-admission-role-dsn-invalid",
            "api-admission-role-collision",
            "api-admission-role-provision-failed",
        "api-admission-role-verification-failed",
                "api-ingress-role-collision",
                "api-ingress-schema-migration-failed",
                "api-ingress-role-verification-failed",
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
    assert harness.PHASE_SEMANTIC_FAILURE_CODES == expected_semantic
    assert harness.SAFE_SCOPED_FAILURE_CODES == frozenset(
        {
            *(
                f"{phase}-{reason}"
                for phase in harness.OUTPUT_COMMAND_PHASES
                for reason in harness.OUTPUT_FAILURE_REASONS
            ),
            *(
                f"{phase}-{reason}"
                for phase in harness.HOST_COMMAND_PHASES
                for reason in harness.HOST_FAILURE_REASONS
            ),
            *(
                f"{phase}-collision"
                for phase in harness.COLLISION_COMMAND_PHASES
            ),
            *expected_semantic,
        }
    )
    for code in harness.SAFE_SCOPED_FAILURE_CODES:
        line = f"isolated PostgreSQL browser gate failed: {code}\n".encode("ascii")
        assert len(line) <= harness.MAX_SCOPED_DIAGNOSTIC_BYTES
        assert harness.SCOPED_FAILURE_LINE_RE.fullmatch(line) is not None
        for forbidden in ("token", "secret", "postgresql", "http", "/", "@"):
            assert forbidden not in code


def test_host_commands_capture_both_streams_in_private_logs_without_printing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = b"postgresql://user:super-secret@127.0.0.1/db\n"

    guard = harness._RunStorageGuard(tmp_path)
    guard.start()
    log = tmp_path / "migration.log"
    harness._run_host(
        [sys.executable, "-c", "import os; os.write(1, %r)" % secret],
        phase="schema-migrate",
        repo_root=tmp_path,
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
        log_path=log,
        storage_guard=guard,
    )
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.read_bytes() == secret
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""

    with pytest.raises(harness.IsolatedPostgresError) as failure:
        harness._run_host(
            [
                sys.executable,
                "-c",
                "import os,sys; os.write(1, %r); sys.exit(7)" % secret,
            ],
            phase="session-bootstrap",
            repo_root=tmp_path,
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            log_path=tmp_path / "bootstrap.log",
            storage_guard=guard,
        )
    assert str(failure.value) == "session-bootstrap-exit-nonzero"
    assert "super-secret" not in str(failure.value)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    guard.stop()


def test_per_producer_and_aggregate_storage_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer_root = tmp_path / "producer"
    producer_root.mkdir()
    producer_guard = harness._RunStorageGuard(producer_root, maximum_bytes=64 * 1024)
    producer_guard.start()
    monkeypatch.setattr(harness, "PRODUCER_FILE_MAX_BYTES", 4096)
    monkeypatch.setattr(harness, "PRODUCER_LOG_MAX_BYTES", 4096)
    with pytest.raises(harness.IsolatedPostgresError, match="producer-log-limit-exceeded"):
        harness._run_host(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 8192)"],
            phase="schema-check",
            repo_root=producer_root,
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            log_path=producer_root / "noisy.log",
            storage_guard=producer_guard,
        )
    producer_guard.stop()

    browser_root = tmp_path / "browser-producer"
    browser_root.mkdir()
    browser_guard = harness._RunStorageGuard(browser_root, maximum_bytes=64 * 1024)
    browser_guard.start()
    browser_log = browser_root / "browser.log"
    with pytest.raises(harness.IsolatedPostgresError, match="producer-log-limit-exceeded"):
        harness._run_host(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 8192)"],
            phase="browser-test",
            repo_root=browser_root,
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            log_path=browser_log,
            storage_guard=browser_guard,
        )
    assert browser_log.stat().st_size == 4096
    browser_guard.stop()
    browser_command = harness._producer_command(
        ["/usr/bin/true"],
        maximum_bytes=harness.BROWSER_PRODUCER_FILE_MAX_BYTES,
    )
    assert browser_command[1] == (
        f"--fsize={harness.BROWSER_PRODUCER_FILE_MAX_BYTES}:"
        f"{harness.BROWSER_PRODUCER_FILE_MAX_BYTES}"
    )

    aggregate_root = tmp_path / "aggregate"
    aggregate_root.mkdir()
    aggregate_guard = harness._RunStorageGuard(
        aggregate_root,
        maximum_bytes=64 * 1024,
        poll_seconds=0.01,
    )

    class FakeProducer:
        terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

    registered = FakeProducer()
    unregistered_docker_cli = FakeProducer()
    aggregate_guard.start()
    aggregate_guard.register(registered)  # type: ignore[arg-type]
    (aggregate_root / "nested").mkdir()
    (aggregate_root / "nested" / "noise.bin").write_bytes(b"x" * (128 * 1024))
    deadline = time.monotonic() + 1
    while not registered.terminated and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registered.terminated is True
    assert unregistered_docker_cli.terminated is False
    with pytest.raises(harness.IsolatedPostgresError, match="run-storage-limit-exceeded"):
        aggregate_guard.stop()

    entry_root = tmp_path / "entries"
    entry_root.mkdir()
    entry_guard = harness._RunStorageGuard(
        entry_root,
        maximum_bytes=1024 * 1024,
        maximum_entries=8,
        poll_seconds=0.01,
    )
    entry_producer = FakeProducer()
    entry_guard.start()
    entry_guard.register(entry_producer)  # type: ignore[arg-type]
    for index in range(8):
        (entry_root / f"zero-{index}").touch()
    deadline = time.monotonic() + 1
    while not entry_producer.terminated and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entry_producer.terminated is True
    with pytest.raises(harness.IsolatedPostgresError, match="run-storage-limit-exceeded"):
        entry_guard.stop()


def test_storage_guard_counts_symlink_entry_without_traversing_target(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * (1024 * 1024))
    (run_root / "outside-link").symlink_to(outside)
    guard = harness._RunStorageGuard(run_root, maximum_bytes=64 * 1024)
    guard.start()
    guard.stop()


def test_registered_producer_group_has_bounded_term_then_kill_fallback(
    tmp_path: Path,
) -> None:
    class StubbornProducer:
        terminated = False
        killed = False

        def poll(self) -> int | None:
            return -signal.SIGKILL if self.killed else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("producer", timeout)
            return -signal.SIGKILL

    guard = harness._RunStorageGuard(tmp_path)
    producer = StubbornProducer()
    guard.register(producer)  # type: ignore[arg-type]
    guard.terminate(  # type: ignore[arg-type]
        producer,
        term_timeout_seconds=0,
        kill_timeout_seconds=0,
    )
    assert producer.terminated is True
    assert producer.killed is True
    guard.unregister(producer)  # type: ignore[arg-type]


def test_producer_group_waits_for_descendant_after_leader_exits(
    tmp_path: Path,
) -> None:
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    leader_code = (
        "import subprocess,sys,time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(0.3)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(0.5)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    guard = harness._RunStorageGuard(tmp_path)
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().decode("ascii").strip())
        assert child_pid > 1
        guard.register(leader)
        assert leader.wait(timeout=2) == 0
        assert harness._RunStorageGuard._process_group_exists(leader, leader.pid)
        guard.terminate(
            leader,
            term_timeout_seconds=1,
            kill_timeout_seconds=2,
        )
        assert not harness._RunStorageGuard._process_group_exists(leader, leader.pid)
        assert guard._failed.is_set() is False
    finally:
        guard.unregister(leader)
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if leader.stdout is not None:
            leader.stdout.close()
        try:
            leader.wait(timeout=2)
        except subprocess.TimeoutExpired:
            leader.kill()
            leader.wait(timeout=2)
    guard.stop()


def _valid_session_bytes() -> bytes:
    return (
        json.dumps(
            {
                "contract_name": "propertyquarry.postgres_browser_internal_session",
                "status": "pass",
                "access_token": "private-session-token",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _session_file(path: Path, data: bytes | None = None) -> Path:
    path.write_bytes(data if data is not None else _valid_session_bytes())
    path.chmod(0o600)
    return path


def test_bootstrap_session_must_be_regular_owned_single_link_0600_and_bounded(
    tmp_path: Path,
) -> None:
    valid = _session_file(tmp_path / "valid.json")
    assert harness._private_session_bytes(valid) == _valid_session_bytes()

    wrong_mode = _session_file(tmp_path / "wrong-mode.json")
    wrong_mode.chmod(0o644)
    with pytest.raises(harness.IsolatedPostgresError, match="bootstrap-session-invalid"):
        harness._private_session_bytes(wrong_mode)

    target = _session_file(tmp_path / "target.json")
    symlink = tmp_path / "session-link.json"
    symlink.symlink_to(target)
    with pytest.raises(harness.IsolatedPostgresError, match="bootstrap-session-invalid"):
        harness._private_session_bytes(symlink)

    hardlink = tmp_path / "session-hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(harness.IsolatedPostgresError, match="bootstrap-session-invalid"):
        harness._private_session_bytes(target)

    oversized = _session_file(
        tmp_path / "oversized.json", b"x" * (harness.MAX_SESSION_BYTES + 1)
    )
    with pytest.raises(harness.IsolatedPostgresError, match="bootstrap-session-invalid"):
        harness._private_session_bytes(oversized)

    invalid = _session_file(tmp_path / "invalid.json", b'{"status":"pass"}\n')
    with pytest.raises(harness.IsolatedPostgresError, match="bootstrap-session-invalid"):
        harness._private_session_bytes(invalid)


def test_validated_session_copy_is_private_and_revalidated_before_browser(
    tmp_path: Path,
) -> None:
    source = _session_file(tmp_path / "source.json")
    protected = tmp_path / "protected.json"
    harness._write_private_bytes(protected, harness._private_session_bytes(source))
    assert stat.S_IMODE(protected.stat().st_mode) == 0o600
    assert harness._private_session_bytes(protected) == source.read_bytes()


def test_internal_watchdog_and_external_signals_enter_noninterruptible_unwind() -> None:
    events: list[tuple[object, ...]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, seconds: float, callback: object) -> None:
            self.seconds = seconds
            self.callback = callback

        def start(self) -> None:
            events.append(("timer-start", self.seconds))

        def cancel(self) -> None:
            events.append(("timer-cancel",))

    installed: dict[int, object] = {}

    def set_signal(signum: int, handler: object) -> object:
        installed[signum] = handler
        events.append(("signal", signum, handler))
        return handler

    guard = harness._LifecycleGuard(
        timeout_seconds=harness.INTERNAL_RUNTIME_MAX_SECONDS,
        timer_factory=FakeTimer,
        kill=lambda pid, signum: events.append(("kill", pid, signum)),
        signal_getter=lambda signum: f"previous-{signum}",
        signal_setter=set_signal,
    )
    guard.__enter__()
    assert signal.SIGTERM in installed and signal.SIGINT in installed
    reserve = harness.HOST_RUNTIME_MAX_SECONDS - harness.INTERNAL_RUNTIME_MAX_SECONDS
    assert reserve >= (
        harness.CLEANUP_WORST_CASE_SECONDS + harness.CLEANUP_SAFETY_MARGIN_SECONDS
    )
    assert harness.CLEANUP_WORST_CASE_SECONDS == (
        harness.CLEANUP_DOCKER_COMMAND_COUNT
        * harness.CLEANUP_COMMAND_TIMEOUT_SECONDS
        + harness.API_TERM_TIMEOUT_SECONDS
        + harness.API_KILL_TIMEOUT_SECONDS
        + harness.PRODUCER_TERM_TIMEOUT_SECONDS
        + harness.PRODUCER_KILL_TIMEOUT_SECONDS
        + 2 * harness.DATABASE_RELAY_JOIN_TIMEOUT_SECONDS
        + harness.STORAGE_GUARD_JOIN_TIMEOUT_SECONDS
    )
    timer = guard._timer
    assert isinstance(timer, FakeTimer)
    timer.callback()  # type: ignore[operator]
    assert any(event[0] == "kill" and event[2] == signal.SIGTERM for event in events)
    with pytest.raises(harness.IsolatedPostgresError, match="internal-watchdog-expired"):
        guard._handle(signal.SIGTERM, None)
    assert installed[signal.SIGTERM] is signal.SIG_IGN
    assert installed[signal.SIGINT] is signal.SIG_IGN
    guard.__exit__(None, None, None)
    assert ("timer-cancel",) in events


def test_api_stop_failure_is_contained_so_docker_cleanup_can_continue() -> None:
    class UnstoppableProcess:
        def poll(self) -> None:
            return None

        def send_signal(self, _signum: int) -> None:
            raise OSError("secret process detail")

        def kill(self) -> None:
            raise OSError("secret kill detail")

        def wait(self, _timeout: int) -> None:
            raise subprocess.TimeoutExpired("private-command", _timeout)

    harness._stop_api(UnstoppableProcess())  # type: ignore[arg-type]


def test_docker_cleanup_failure_outranks_every_post_cleanup_failure() -> None:
    errors = {
        "cleanup_error": harness.IsolatedPostgresError(
            "cleanup-container-remove-exit-nonzero"
        ),
        "dependency_error": harness.IsolatedPostgresError(
            "dependency-snapshot-invalid"
        ),
        "storage_error": harness.IsolatedPostgresError(
            "run-storage-limit-exceeded"
        ),
        "relay_error": harness.IsolatedPostgresError("database-relay-stop-failed"),
    }
    with pytest.raises(harness.IsolatedPostgresError) as failure:
        harness._raise_post_cleanup_errors(**errors)
    assert failure.value is errors["cleanup_error"]


def test_scoped_failure_diagnostic_is_private_exact_and_allowlisted(tmp_path: Path) -> None:
    diagnostic = tmp_path / "safe.log"
    descriptor = harness._open_private_log(diagnostic)
    os.write(
        descriptor,
        b"isolated PostgreSQL browser gate failed: "
        b"docker-image-inspect-stderr-not-empty\n",
    )
    os.close(descriptor)
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert (
        harness._scoped_failure_code(diagnostic)
        == "docker-image-inspect-stderr-not-empty"
    )

    unsafe = tmp_path / "unsafe.log"
    unsafe.write_bytes(
        b"isolated PostgreSQL browser gate failed: "
        b"postgresql-operator-secret-token\n"
    )
    unsafe.chmod(0o600)
    assert harness._scoped_failure_code(unsafe) is None

    trailing_secret = tmp_path / "trailing.log"
    trailing_secret.write_bytes(
        b"isolated PostgreSQL browser gate failed: "
        b"docker-image-inspect-exit-nonzero\nsecret\n"
    )
    trailing_secret.chmod(0o600)
    assert harness._scoped_failure_code(trailing_secret) is None

    truncated = tmp_path / "truncated.log"
    truncated.write_bytes(
        b"isolated PostgreSQL browser gate failed: "
        b"docker-image-inspect-exit-nonzero"
    )
    truncated.chmod(0o600)
    assert harness._scoped_failure_code(truncated) is None

    oversized = tmp_path / "oversized.log"
    oversized.write_bytes(b"x" * (harness.MAX_SCOPED_DIAGNOSTIC_BYTES + 1))
    oversized.chmod(0o600)
    assert harness._scoped_failure_code(oversized) is None

    wrong_mode = tmp_path / "wrong-mode.log"
    wrong_mode.write_bytes(diagnostic.read_bytes())
    wrong_mode.chmod(0o644)
    assert harness._scoped_failure_code(wrong_mode) is None

    linked = tmp_path / "linked.log"
    linked.symlink_to(diagnostic)
    assert harness._scoped_failure_code(linked) is None

    hardlinked = tmp_path / "hardlinked.log"
    os.link(diagnostic, hardlinked)
    assert harness._scoped_failure_code(diagnostic) is None
    assert harness._scoped_failure_code(hardlinked) is None


def _patch_outer_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harness,
        "_validate_worktree",
        lambda _repo, _venv: (
            Path("/tmp/property-e4c67fd-integration"),
            "/docker/property/.venv/bin/python",
        ),
    )
    monkeypatch.setattr(harness, "_require_absolute_executable", lambda path, _code: path)
    monkeypatch.setattr(
        harness,
        "_validate_chromium_headless_shell",
        lambda path: path if path == HEADLESS_SHELL else pytest.fail("unexpected shell"),
    )
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _size: RUN_ID)


def test_outer_launcher_fails_closed_before_worktree_or_docker_without_shell(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        harness,
        "_validate_worktree",
        lambda *_args: pytest.fail("worktree validation must not run"),
    )
    assert harness.main(["--chromium-headless-shell", ""]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "isolated PostgreSQL browser gate failed: "
        "chromium-headless-shell-invalid\n"
    )


def test_outer_launcher_suppresses_scoped_output_and_returns_only_generic_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_outer_prerequisites(monkeypatch)
    observed: dict[str, object] = {}

    def fake_scope(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed.update(kwargs)
        descriptor = kwargs["stderr"]
        assert isinstance(descriptor, int)
        observed["stderr_mode"] = stat.S_IMODE(os.fstat(descriptor).st_mode)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness.subprocess, "run", fake_scope)
    assert (
        harness.main(
            [
                "--repo-root",
                "/tmp/property-e4c67fd-integration",
                "--venv",
                "/docker/property/.venv",
                "--chromium-headless-shell",
                HEADLESS_SHELL,
            ]
        )
        == 0
    )
    assert observed["stdout"] == subprocess.DEVNULL
    assert isinstance(observed["stderr"], int)
    assert observed["stderr_mode"] == 0o600
    assert observed["stdin"] == subprocess.DEVNULL
    assert "--property=RuntimeMaxSec=1200s" in observed["command"]  # type: ignore[operator]
    assert observed["command"][  # type: ignore[index]
        observed["command"].index("--chromium-headless-shell") + 1  # type: ignore[union-attr]
    ] == HEADLESS_SHELL
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "status": "pass",
        "scope": "isolated-postgres-browser",
    }
    assert captured.err == ""


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    (
        (
            b"isolated PostgreSQL browser gate failed: "
            b"docker-image-inspect-stderr-not-empty\n",
            "docker-image-inspect-stderr-not-empty",
        ),
        (
            b"isolated PostgreSQL browser gate failed: "
            b"schema-migrate-exit-nonzero\n",
            "schema-migrate-exit-nonzero",
        ),
        (
            b"isolated PostgreSQL browser gate failed: "
            b"postgresql-operator-secret-token\n",
            "scoped-run-failed",
        ),
        (
            b"unexpected traceback with postgresql://operator:secret@host/db\n",
            "scoped-run-failed",
        ),
    ),
)
def test_outer_launcher_propagates_only_exact_allowlisted_phase_codes(
    diagnostic: bytes,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_outer_prerequisites(monkeypatch)

    def failed_scope(_command: list[str], **kwargs: object) -> SimpleNamespace:
        descriptor = kwargs["stderr"]
        assert isinstance(descriptor, int)
        os.write(descriptor, diagnostic)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(harness.subprocess, "run", failed_scope)
    assert (
        harness.main(
            [
                "--repo-root",
                "/tmp/property-e4c67fd-integration",
                "--venv",
                "/docker/property/.venv",
                "--chromium-headless-shell",
                HEADLESS_SHELL,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"isolated PostgreSQL browser gate failed: {expected}\n"
    assert "operator" not in captured.err
    assert "secret" not in captured.err


def test_harness_source_has_no_compose_build_live_stack_or_repo_env_path() -> None:
    source = Path(harness.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "docker compose",
        "docker-compose",
        "propertyquarry-api",
        "propertyquarry-scheduler",
        "propertyquarry-db-live",
        "property_propertyquarry",
        "incoming_property_tours",
        "--build",
        "prune",
        "propertyquarry-web-runtime:latest",
        'repo_root / ".env"',
    ):
        assert forbidden not in source
    assert harness.POSTGRES_IMAGE.endswith(
        "@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
    )
    for required in (
        'log_path=temp_root / "schema-migrate.log"',
        'log_path=temp_root / "schema-check.log"',
        'log_path=temp_root / "session-bootstrap.log"',
        'log_path=temp_root / "browser-pytest.log"',
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "_private_session_bytes(session_path)",
        "lifecycle.begin_cleanup()",
    ):
        assert required in source


def test_disposable_lane_runs_queue_erasure_order_contracts() -> None:
    source = Path(harness.__file__).read_text(encoding="utf-8")

    database_binding = '"EA_TEST_PROPERTY_DATABASE_URL": database_url'
    queue_contract = '"tests/test_property_search_work_queue_postgres.py"'
    assert database_binding in source
    assert queue_contract in source
    assert source.index(database_binding) < source.index(queue_contract)
