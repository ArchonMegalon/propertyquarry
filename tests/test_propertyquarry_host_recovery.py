from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts import propertyquarry_host_recovery as recovery


RELEASE_SHA = "a" * 40


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": RELEASE_SHA,
        "PROPERTYQUARRY_COMPOSE_PROJECT_NAME": "propertyquarry-production",
        "PROPERTYQUARRY_RECOVERY_TUNNEL_ID": "propertyquarry-production",
        "PROPERTYQUARRY_RECOVERY_ROUTE_HOST": "propertyquarry.com",
        "EA_HOST_PORT": "8090",
        "PROPERTYQUARRY_RECOVERY_COMMAND_TIMEOUT_SECONDS": "5",
        "PROPERTYQUARRY_RECOVERY_READY_TIMEOUT_SECONDS": "1",
        "PROPERTYQUARRY_RECOVERY_POLL_INTERVAL_SECONDS": "0.01",
    }
    values.update(overrides)
    return values


class NoMutationRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> recovery.CommandResult:
        self.calls.append(tuple(argv))
        raise AssertionError("candidate recovery commands must never execute")


def test_default_dry_run_is_command_free_atomic_and_non_authoritative(
    tmp_path: Path,
) -> None:
    runner = NoMutationRunner()
    receipt_path = tmp_path / "dry-run.json"
    receipt, exit_code = recovery.run_recovery(
        environ=_environment(),
        receipt_path=receipt_path,
        runner=runner,
    )

    assert exit_code == 0
    assert receipt["status"] == "dry_run"
    assert receipt["execution_ready"] is False
    assert runner.calls == []
    assert receipt["dedicated_boundary"]["credentials_owned_by_external_controller"] is True
    assert receipt["external_controller"] == {
        "required": True,
        "operation": "recovery-run",
        "source_execution_enabled": False,
        "authority": "installed_native_controller",
    }
    assert receipt["candidate_observations"]["authoritative"] is False
    assert receipt["candidate_observations"]["compose_contract"]["status"] == "observed"
    assert "propertyquarry-worker" in receipt["dedicated_boundary"]["steady_state_services"]
    assert recovery.CONTAINER_ENV["PROPERTYQUARRY_WORKER_CONTAINER_NAME"] == (
        "propertyquarry-worker"
    )
    assert receipt["steps"] == [
        {
            "name": "external_controller.recovery-run",
            "status": "planned",
            "owns": [
                "fixed_lock",
                "external_monotonic_state",
                "journal_containment",
                "database_fence",
                "canonical_compose_plan",
                "traffic_and_verification",
            ],
        }
    ]
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_candidate_compose_observation_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = NoMutationRunner()

    def report_candidate_issue() -> None:
        raise recovery.RecoveryValidationError("candidate Compose plan is incomplete")

    monkeypatch.setattr(recovery, "validate_static_compose_contract", report_candidate_issue)
    receipt, exit_code = recovery.run_recovery(
        environ=_environment(),
        receipt_path=tmp_path / "advisory-compose.json",
        runner=runner,
    )

    assert exit_code == 0
    assert receipt["status"] == "dry_run"
    assert receipt["candidate_observations"] == {
        "authoritative": False,
        "compose_contract": {
            "status": "advisory_issue",
            "issues": ["candidate Compose plan is incomplete"],
        },
    }
    assert receipt["steps"][0]["name"] == "external_controller.recovery-run"
    assert runner.calls == []


def test_candidate_compose_worker_boundary_fails_closed_when_missing_or_generic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = recovery.PROPERTY_COMPOSE.read_text(encoding="utf-8")
    worker_marker = "  propertyquarry-worker:\n"
    scheduler_marker = "  propertyquarry-scheduler:\n"
    prefix, worker_and_rest = original.split(worker_marker, 1)
    _worker, suffix = worker_and_rest.split(scheduler_marker, 1)

    missing = tmp_path / "missing-worker.yml"
    missing.write_text(prefix + scheduler_marker + suffix, encoding="utf-8")
    monkeypatch.setattr(recovery, "PROPERTY_COMPOSE", missing)
    with pytest.raises(recovery.RecoveryValidationError, match="propertyquarry-worker"):
        recovery.validate_static_compose_contract()

    generic = tmp_path / "generic-worker.yml"
    generic.write_text(
        original.replace(
            'PROPERTYQUARRY_WORKER_PROFILE: "property_only"',
            'PROPERTYQUARRY_WORKER_PROFILE: "generic"',
            1,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(recovery, "PROPERTY_COMPOSE", generic)
    with pytest.raises(recovery.RecoveryValidationError, match="worker Compose contract"):
        recovery.validate_static_compose_contract()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"PROPERTYQUARRY_RECOVERY_TUNNEL_ID": ""}, "TUNNEL_ID"),
        ({"PROPERTYQUARRY_RECOVERY_ROUTE_HOST": "example.com"}, "propertyquarry.com"),
        ({"PROPERTYQUARRY_RELEASE_COMMIT_SHA": "latest"}, "40-character Git SHA"),
        ({"PROPERTYQUARRY_COMPOSE_FILE": "/tmp/docker-compose.property.yml"}, "COMPOSE_FILE"),
        ({"DOCKER_HOST": "tcp://shared-docker.example:2376"}, "local system Docker socket"),
    ],
)
def test_invalid_dry_run_identity_is_rejected_without_commands(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    runner = NoMutationRunner()
    receipt, exit_code = recovery.run_recovery(
        environ=_environment(**overrides),
        receipt_path=tmp_path / f"invalid-{len(message)}.json",
        runner=runner,
    )

    assert exit_code == 2
    assert message in receipt["error"]["message"]
    assert runner.calls == []


def test_source_execute_fails_before_config_compose_or_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = NoMutationRunner()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("candidate config or Compose was inspected before containment")

    monkeypatch.setattr(recovery, "load_config", forbidden)
    monkeypatch.setattr(recovery, "validate_static_compose_contract", forbidden)
    receipt, exit_code = recovery.run_recovery(
        environ={
            "PROPERTYQUARRY_COMPOSE_FILE": "/candidate/hostile-compose.yml",
            "PROPERTYQUARRY_RECOVERY_SIGNED_AUTHORIZATION": "/candidate/fake.json",
        },
        receipt_path=tmp_path / "execute-disabled.json",
        execute=True,
        runner=runner,
    )

    assert exit_code == 2
    assert receipt["error"]["type"] == "SourceExecutionDisabled"
    assert "installed native controller" in receipt["error"]["message"]
    assert runner.calls == []
    with pytest.raises(recovery.RecoveryValidationError, match="source --execute is disabled"):
        recovery.SubprocessCommandRunner().run(
            ("docker", "compose", "up"), env={}, timeout_seconds=1
        )


def test_real_source_execute_ignores_hostile_guard_swap_and_missing_compose(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    scripts = candidate / "scripts"
    scripts.mkdir(parents=True)
    source = Path(recovery.__file__).resolve()
    entrypoint = scripts / source.name
    shutil.copyfile(source, entrypoint)
    entrypoint.chmod(0o755)
    marker = tmp_path / "candidate-authority-executed"
    hostile_bin = tmp_path / "hostile-bin"
    hostile_bin.mkdir()
    fake_python = hostile_bin / "python3"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' hostile-python >> '{marker}'\nexit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    swapped_controller = tmp_path / "controller"
    swapped_controller.write_text("original", encoding="utf-8")
    (scripts / "propertyquarry_deploy_controller_guard.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['HOSTILE_MARKER']).write_text('imported')\n"
        "Path(os.environ['HOSTILE_CONTROLLER']).write_text('swapped')\n",
        encoding="utf-8",
    )
    receipt_path = tmp_path / "real-execute-disabled.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(scripts),
            "PATH": str(hostile_bin),
            "HOSTILE_MARKER": str(marker),
            "HOSTILE_CONTROLLER": str(swapped_controller),
            "PROPERTYQUARRY_COMPOSE_FILE": str(candidate / "hostile-compose.yml"),
        }
    )

    completed = subprocess.run(
        [
            str(entrypoint),
            "--execute",
            "--receipt",
            str(receipt_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 2
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["error"]["type"] == "SourceExecutionDisabled"
    assert not marker.exists()
    assert swapped_controller.read_text(encoding="utf-8") == "original"
    assert not (candidate / "docker-compose.property.yml").exists()


def test_recovery_source_has_no_candidate_controller_authority() -> None:
    source = Path(recovery.__file__).read_text(encoding="utf-8")

    assert "propertyquarry_deploy_controller_guard" not in source
    assert "invoke_controller" not in source
    assert "controller_invoker" not in source
    assert source.startswith("#!/usr/bin/python3 -I\n")
