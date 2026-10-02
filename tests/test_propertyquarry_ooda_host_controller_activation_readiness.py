from __future__ import annotations

import json
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts import (
    propertyquarry_ooda_host_controller_activation_readiness as readiness,
)


NOW = datetime(2026, 8, 28, 0, 45, tzinfo=timezone.utc)


def _runner(
    calls: list[list[str]],
    *,
    enabled: str = "not-found",
    active: str = "inactive",
    syntax_returncode: int = 0,
):
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[0] == "/usr/bin/systemd-analyze":
            return subprocess.CompletedProcess(
                argv,
                syntax_returncode,
                stdout="" if syntax_returncode == 0 else "invalid unit\n",
            )
        if argv[-2:] == ["is-enabled", readiness.TIMER_UNIT]:
            return subprocess.CompletedProcess(argv, 1, stdout=enabled + "\n")
        if argv[-2:] == ["is-active", readiness.TIMER_UNIT]:
            return subprocess.CompletedProcess(argv, 1, stdout=active + "\n")
        raise AssertionError(f"unexpected command: {argv!r}")

    return run


def test_activation_readiness_stages_exact_plan_without_running_it(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    unit_dir = tmp_path / "user-units"
    receipt = tmp_path / "private" / "readiness.json"

    result = readiness.stage_activation_readiness(
        user_unit_dir=unit_dir,
        receipt_path=receipt,
        now=NOW,
        command_runner=_runner(calls),
    )

    assert result["status"] == "verified"
    assert result["readiness_state"] == "ready_for_authorization"
    assert result["plan_id"].startswith("pqhar_")
    assert len(result["semantic_plan_sha256"]) == 64
    assert result["observed_state"]["service_target"]["status"] == "absent"
    assert result["observed_state"]["timer_target"]["status"] == "absent"
    assert result["observed_state"]["enabled_state"] == "not-found"
    assert result["observed_state"]["active_state"] == "inactive"
    assert result["activation_plan"]["manual_invocation_required"] is True
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["authorization_required"] is True
    assert result["authorization_recorded"] is False
    assert result["installation_authorized"] is False
    assert result["activation_authorized"] is False
    assert result["installation_performed"] is False
    assert result["daemon_reload_performed"] is False
    assert result["timer_enablement_performed"] is False
    assert result["timer_activation_performed"] is False
    assert result["current_evidence_verified"] is True
    assert result["receipt_persisted"] is True
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    assert len(calls) == 3
    assert all(
        call[0] in {"/usr/bin/systemd-analyze", "/usr/bin/systemctl"}
        for call in calls
    )
    assert all("install" not in call[0] for call in calls)
    assert json.loads(receipt.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_activation_readiness_reports_exact_active_install_as_not_required(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "user-units"
    for source, name in (
        (readiness.DEFAULT_SERVICE_SOURCE, readiness.SERVICE_UNIT),
        (readiness.DEFAULT_TIMER_SOURCE, readiness.TIMER_UNIT),
    ):
        readiness.atomic_write_bytes(
            unit_dir / name,
            source.read_bytes(),
            overwrite=True,
        )
    calls: list[list[str]] = []

    result = readiness.stage_activation_readiness(
        user_unit_dir=unit_dir,
        receipt_path=tmp_path / "private" / "readiness.json",
        now=NOW,
        command_runner=_runner(calls, enabled="enabled", active="active"),
    )

    assert result["status"] == "verified"
    assert result["readiness_state"] == "not_required"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["authorization_required"] is False
    assert result["installation_authorized"] is False
    assert result["activation_authorized"] is False


def test_activation_readiness_blocks_mixed_or_drifted_target_state(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "user-units"
    readiness.atomic_write_bytes(
        unit_dir / readiness.SERVICE_UNIT,
        b"[Unit]\nDescription=drifted\n",
        overwrite=True,
    )
    calls: list[list[str]] = []

    result = readiness.stage_activation_readiness(
        user_unit_dir=unit_dir,
        receipt_path=tmp_path / "private" / "readiness.json",
        now=NOW,
        command_runner=_runner(calls),
    )

    assert result["status"] == "blocked"
    assert result["readiness_state"] == "blocked"
    assert result["blocking_reason"] == (
        "host_controller_activation_state_not_admissible"
    )
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["installation_authorized"] is False
    assert result["activation_authorized"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False


def test_activation_readiness_blocks_invalid_unit_syntax(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    result = readiness.stage_activation_readiness(
        user_unit_dir=tmp_path / "user-units",
        receipt_path=tmp_path / "private" / "readiness.json",
        now=NOW,
        command_runner=_runner(calls, syntax_returncode=1),
    )

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "host_controller_activation_observation_failed"
    )
    assert result["current_evidence_verified"] is False
    assert result["installation_performed"] is False
    assert result["timer_activation_performed"] is False
    assert result["receipt_persisted"] is True
