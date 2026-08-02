from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "scripts" / "codexpure"


def _source_home(tmp_path: Path) -> Path:
    source = tmp_path / "canonical-codex"
    source.mkdir(mode=0o700)
    (source / "auth.json").write_text('{"token":"fixture"}\n', encoding="utf-8")
    (source / "config.toml").write_text("model = \"fixture\"\n", encoding="utf-8")
    (source / "skills").mkdir()
    (source / "skills" / "fixture.txt").write_text("skill\n", encoding="utf-8")
    return source


def _mock_codex(tmp_path: Path) -> Path:
    launcher = tmp_path / "mock-codex"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$CODEX_HOME" >> "$CODEX_PURE_TEST_RECORD"
/usr/bin/python3 -c 'import os, sqlite3; path=os.path.join(os.environ["CODEX_HOME"], "state_5.sqlite"); connection=sqlite3.connect(path, timeout=0.05); connection.execute("CREATE TABLE IF NOT EXISTS shim_probe(value TEXT)"); connection.commit(); connection.close()'
sleep "${CODEX_PURE_TEST_HOLD_SECONDS:-0}"
""",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher


def _environment(
    tmp_path: Path,
    *,
    source: Path,
    launcher: Path,
) -> tuple[dict[str, str], Path, Path]:
    state_root = tmp_path / "codexpure-state"
    record = tmp_path / "homes.txt"
    environment = {
        **os.environ,
        "CODEX_PURE_BASE_CODEX": str(launcher),
        "CODEX_PURE_SOURCE_HOME": str(source),
        "CODEX_PURE_STATE_ROOT": str(state_root),
        "CODEX_PURE_TEST_RECORD": str(record),
    }
    return environment, state_root, record


def test_codexpure_uses_private_sqlite_while_canonical_databases_are_locked(
    tmp_path: Path,
) -> None:
    source = _source_home(tmp_path)
    launcher = _mock_codex(tmp_path)
    environment, state_root, record = _environment(
        tmp_path,
        source=source,
        launcher=launcher,
    )
    environment["CODEX_PURE_INSTANCE"] = "locked-canonical-e2e"
    canonical_state = sqlite3.connect(source / "state_5.sqlite")
    canonical_logs = sqlite3.connect(source / "logs_2.sqlite")
    canonical_state.execute("CREATE TABLE canonical_state(value TEXT)")
    canonical_logs.execute("CREATE TABLE canonical_logs(value TEXT)")
    canonical_state.execute("BEGIN EXCLUSIVE")
    canonical_logs.execute("BEGIN EXCLUSIVE")
    try:
        completed = subprocess.run(
            [str(SHIM), "--version"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        canonical_logs.rollback()
        canonical_logs.close()
        canonical_state.rollback()
        canonical_state.close()

    assert completed.returncode == 0, completed.stderr
    pure_home = Path(record.read_text(encoding="utf-8").strip())
    assert pure_home == state_root / "instances" / "locked-canonical-e2e"
    assert pure_home != source
    assert (pure_home / "state_5.sqlite").is_file()
    assert not (pure_home / "logs_2.sqlite").exists()
    assert (pure_home / "auth.json").read_text(encoding="utf-8") == (
        source / "auth.json"
    ).read_text(encoding="utf-8")
    assert stat.S_IMODE((pure_home / "auth.json").stat().st_mode) == 0o600
    assert (pure_home / "skills").is_symlink()
    assert (pure_home / "skills").resolve() == (source / "skills").resolve()


def test_noninteractive_sibling_launches_receive_distinct_private_homes(
    tmp_path: Path,
) -> None:
    source = _source_home(tmp_path)
    launcher = _mock_codex(tmp_path)
    environment, _state_root, record = _environment(
        tmp_path,
        source=source,
        launcher=launcher,
    )
    environment["CODEX_PURE_TEST_HOLD_SECONDS"] = "0.2"

    first = subprocess.Popen(
        [str(SHIM), "--version"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [str(SHIM), "--version"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)
    second_stdout, second_stderr = second.communicate(timeout=10)

    assert first.returncode == 0, (first_stdout, first_stderr)
    assert second.returncode == 0, (second_stdout, second_stderr)
    homes = [
        Path(line)
        for line in record.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(homes) == 2
    assert len(set(homes)) == 2
    assert all(path.name.startswith("process-") for path in homes)


def test_explicit_instance_collision_fails_before_sqlite_startup(
    tmp_path: Path,
) -> None:
    source = _source_home(tmp_path)
    launcher = _mock_codex(tmp_path)
    environment, _state_root, record = _environment(
        tmp_path,
        source=source,
        launcher=launcher,
    )
    environment.update(
        {
            "CODEX_PURE_INSTANCE": "shared-instance",
            "CODEX_PURE_TEST_HOLD_SECONDS": "2",
        }
    )
    first = subprocess.Popen(
        [str(SHIM)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if record.exists() and record.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.02)
    else:
        first.terminate()
        raise AssertionError("first shim never entered the mock Codex runtime")

    second = subprocess.run(
        [str(SHIM)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode == 0, (first_stdout, first_stderr)
    assert second.returncode == 75
    assert "private Codex instance is already active: shared-instance" in second.stderr
    assert "database is locked" not in second.stderr.lower()
    assert len(record.read_text(encoding="utf-8").splitlines()) == 1


def test_codexpure_rejects_canonical_or_nested_explicit_home(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    launcher = _mock_codex(tmp_path)
    environment, _state_root, _record = _environment(
        tmp_path,
        source=source,
        launcher=launcher,
    )
    for forbidden_home in (source, source / "nested"):
        completed = subprocess.run(
            [str(SHIM), "--version"],
            env={**environment, "CODEX_PURE_HOME": str(forbidden_home)},
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 1
        assert "must be outside the canonical Codex home" in completed.stderr
