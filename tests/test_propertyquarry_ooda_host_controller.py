from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_host_controller as controller


NOW = datetime(2026, 8, 28, 0, 15, tzinfo=timezone.utc)


def _false_authority() -> dict[str, object]:
    return {
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _ready_tick(*, interrupt: bool = True) -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_safe_tick.v1",
        "status": "ready",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "next_action": "authorize the exact staged configuration merge",
        "action_required": True,
        "interrupt_operator": interrupt,
        "components": {
            "runtime_control": {
                "status": "verified",
                "current_evidence_verified": True,
                "receipt_persisted": True,
                **_false_authority(),
            }
        },
        **_false_authority(),
    }


def test_host_controller_runs_without_scheduler_handoff_and_never_presents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def run_tick(**kwargs):
        observed.update(kwargs)
        return _ready_tick(interrupt=True)

    monkeypatch.setattr(controller.safe_tick, "run_safe_tick", run_tick)
    receipt = tmp_path / "private" / "host-controller-latest.json"

    result = controller.run_host_controller_once(
        handoff_path=tmp_path / "missing-handoff.json",
        receipt_path=receipt,
        now=NOW,
    )

    assert observed["now"] == NOW
    assert result["status"] == "verified"
    assert result["scheduler_handoff"]["status"] == "absent"
    assert result["host_safe_tick_completed"] is True
    assert result["operator_presentation_required"] is True
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["presentation_recorded"] is False
    assert result["current_evidence_verified"] is True
    assert result["receipt_persisted"] is True
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    assert json.loads(receipt.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_host_controller_consumes_current_handoff_as_advisory_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller.safe_tick,
        "run_safe_tick",
        lambda **_kwargs: _ready_tick(interrupt=False),
    )
    handoff_path = tmp_path / "private" / "host-handoff.json"
    handoff = {
        "schema": controller.runtime_control.HOST_HANDOFF_SCHEMA,
        "status": "host_review_required",
        "updated_at": NOW.isoformat(),
        "handoff_id": "pqrh_" + "a" * 24,
        "host_review_required": True,
        "host_runtime_observation_verified": False,
        "action_required": True,
        "interrupt_operator": False,
        "receipt_persisted": True,
        **_false_authority(),
    }
    controller.atomic_write_bytes(
        handoff_path,
        controller.runtime_control.runtime_review._canonical(handoff),
        overwrite=True,
    )

    result = controller.run_host_controller_once(
        handoff_path=handoff_path,
        receipt_path=tmp_path / "private" / "controller.json",
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["scheduler_handoff"]["status"] == "host_review_required"
    assert result["scheduler_handoff"]["host_review_required"] is True
    assert result["scheduler_handoff"]["handoff_confers_authority"] is False
    assert result["operator_presentation_required"] is False
    assert result["interrupt_operator"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False


def test_host_controller_fails_closed_when_safe_tick_is_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller.safe_tick,
        "run_safe_tick",
        lambda **_kwargs: {
            "status": "blocked",
            "blocking_reason": "safe_tick_already_running",
            "action_required": False,
            "interrupt_operator": False,
            **_false_authority(),
        },
    )

    result = controller.run_host_controller_once(
        handoff_path=tmp_path / "missing.json",
        receipt_path=tmp_path / "private" / "controller.json",
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "host_safe_tick_not_ready"
    assert result["host_safe_tick_completed"] is False
    assert result["operator_presentation_required"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["receipt_persisted"] is True
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_user_systemd_sources_are_staged_but_do_not_install_or_deliver() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (
        root
        / "config/systemd/propertyquarry-ooda-host-controller.service"
    ).read_text(encoding="utf-8")
    timer = (
        root / "config/systemd/propertyquarry-ooda-host-controller.timer"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert (
        "ExecStart=/usr/bin/python3 /docker/property/scripts/"
        "propertyquarry_ooda_host_controller.py"
    ) in service
    assert "UMask=0077" in service
    assert "NoNewPrivileges=true" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    combined = service + timer
    assert "propertyquarry_ooda_action_only.py" not in combined
    assert "systemctl" not in combined
    assert "docker compose" not in combined
    assert "deploy" not in combined.lower()
    assert "restart" not in combined.lower()
