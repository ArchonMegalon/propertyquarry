#!/usr/bin/env python3
"""Stage exact user-systemd activation readiness without installing anything."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_host_controller as host_controller
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_host_controller_activation_readiness.v1"
DEFAULT_SERVICE_SOURCE = (
    ROOT / "config/systemd/propertyquarry-ooda-host-controller.service"
)
DEFAULT_TIMER_SOURCE = (
    ROOT / "config/systemd/propertyquarry-ooda-host-controller.timer"
)
DEFAULT_USER_UNIT_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    / "systemd/user"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "host-controller-activation-readiness.json"
)
SERVICE_UNIT = "propertyquarry-ooda-host-controller.service"
TIMER_UNIT = "propertyquarry-ooda-host-controller.timer"
MAX_UNIT_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 15
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _trusted_unit(path: Path, *, field: str) -> tuple[bytes, str, str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not 0 < metadata.st_size <= MAX_UNIT_BYTES
    ):
        raise ValueError(f"{field}_not_admissible")
    raw = target.read_bytes()
    after = target.lstat()
    if (
        len(raw) != metadata.st_size
        or (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        )
        != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
    ):
        raise ValueError(f"{field}_changed_while_read")
    return raw, _sha256(raw), oct(stat.S_IMODE(metadata.st_mode))


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    runtime_dir = f"/run/user/{os.geteuid()}"
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C",
            "XDG_RUNTIME_DIR": runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
        },
    )


def _normalized_state(
    result: subprocess.CompletedProcess[str],
    *,
    allowed: set[str],
) -> str:
    value = str(result.stdout or "").strip().splitlines()
    normalized = value[-1].strip().lower() if value else ""
    return normalized if normalized in allowed else "unknown"


def _target_projection(
    path: Path,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    target = Path(path).absolute()
    try:
        raw, digest, mode = _trusted_unit(target, field="installed_unit")
    except FileNotFoundError:
        return {
            "path": str(target),
            "status": "absent",
            "sha256": "",
            "mode": "",
            "matches_source": False,
        }
    except Exception:
        return {
            "path": str(target),
            "status": "not_admissible",
            "sha256": "",
            "mode": "",
            "matches_source": False,
        }
    return {
        "path": str(target),
        "status": "installed",
        "sha256": digest,
        "bytes": len(raw),
        "mode": mode,
        "matches_source": digest == expected_sha256,
    }


def _persist(result: dict[str, Any], *, path: Path) -> dict[str, Any]:
    persisted = {**result, "receipt_persisted": True}
    try:
        target = host_controller.runtime_control.runtime_review._rooted(path)
        atomic_write_bytes(
            target,
            host_controller.runtime_control.runtime_review._canonical(
                persisted
            ),
            overwrite=True,
        )
        observed, _raw, _digest = load_strict_json_object_snapshot(
            target,
            field="host controller activation readiness",
            maximum_bytes=512 * 1024,
        )
        if observed != persisted:
            raise ValueError("activation_readiness_write_not_verified")
    except Exception:
        return {**result, "receipt_persisted": False}
    return persisted


def _blocked(reason: str, *, now: datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "readiness_state": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": "repair and restage the private activation plan",
        "action_required": False,
        "interrupt_operator": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "installation_authorized": False,
        "activation_authorized": False,
        "installation_performed": False,
        "daemon_reload_performed": False,
        "timer_enablement_performed": False,
        "timer_activation_performed": False,
        "current_evidence_verified": False,
        "receipt_persisted": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def stage_activation_readiness(
    *,
    service_source: Path = DEFAULT_SERVICE_SOURCE,
    timer_source: Path = DEFAULT_TIMER_SOURCE,
    user_unit_dir: Path = DEFAULT_USER_UNIT_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    command_runner: CommandRunner = _run,
) -> dict[str, Any]:
    """Observe exact unit state and stage a non-executing activation plan."""

    observed_now = _now(now)
    try:
        service_raw, service_sha256, service_mode = _trusted_unit(
            service_source,
            field="service_unit_source",
        )
        timer_raw, timer_sha256, timer_mode = _trusted_unit(
            timer_source,
            field="timer_unit_source",
        )
        syntax = command_runner(
            [
                "/usr/bin/systemd-analyze",
                "verify",
                str(Path(service_source).absolute()),
                str(Path(timer_source).absolute()),
            ]
        )
        if syntax.returncode != 0:
            raise ValueError("host_controller_unit_syntax_not_verified")
        enabled_result = command_runner(
            ["/usr/bin/systemctl", "--user", "is-enabled", TIMER_UNIT]
        )
        active_result = command_runner(
            ["/usr/bin/systemctl", "--user", "is-active", TIMER_UNIT]
        )
        enabled_state = _normalized_state(
            enabled_result,
            allowed={"enabled", "disabled", "static", "not-found"},
        )
        active_state = _normalized_state(
            active_result,
            allowed={"active", "inactive", "failed", "unknown"},
        )
        unit_root = Path(user_unit_dir).absolute()
        service_target = unit_root / SERVICE_UNIT
        timer_target = unit_root / TIMER_UNIT
        service_installed = _target_projection(
            service_target,
            expected_sha256=service_sha256,
        )
        timer_installed = _target_projection(
            timer_target,
            expected_sha256=timer_sha256,
        )
    except Exception:
        return _persist(
            _blocked(
                "host_controller_activation_observation_failed",
                now=observed_now,
            ),
            path=receipt_path,
        )

    targets_absent = (
        service_installed["status"] == "absent"
        and timer_installed["status"] == "absent"
    )
    installed_exact = (
        service_installed["status"] == "installed"
        and service_installed["matches_source"] is True
        and timer_installed["status"] == "installed"
        and timer_installed["matches_source"] is True
    )
    already_active = (
        installed_exact
        and enabled_state == "enabled"
        and active_state == "active"
    )
    ready = (
        targets_absent
        and enabled_state in {"not-found", "disabled"}
        and active_state == "inactive"
    )
    if not (ready or already_active):
        result = _blocked(
            "host_controller_activation_state_not_admissible",
            now=observed_now,
        )
        result["observed_state"] = {
            "service_target": service_installed,
            "timer_target": timer_installed,
            "enabled_state": enabled_state,
            "active_state": active_state,
        }
        return _persist(result, path=receipt_path)

    service_target = str((Path(user_unit_dir).absolute() / SERVICE_UNIT))
    timer_target = str((Path(user_unit_dir).absolute() / TIMER_UNIT))
    semantic_plan = {
        "source_units": {
            "service": {
                "path": str(Path(service_source).absolute()),
                "sha256": service_sha256,
            },
            "timer": {
                "path": str(Path(timer_source).absolute()),
                "sha256": timer_sha256,
            },
        },
        "target_units": {
            "service": service_target,
            "timer": timer_target,
        },
        "protected_operations": [
            "user_systemd_unit_install",
            "user_systemd_daemon_reload",
            "user_systemd_timer_enablement",
            "user_systemd_timer_activation",
        ],
    }
    plan_digest = _sha256(
        host_controller.runtime_control.runtime_review._canonical(
            semantic_plan
        )
    )
    result = {
        "schema": SCHEMA,
        "status": "verified",
        "readiness_state": (
            "not_required" if already_active else "ready_for_authorization"
        ),
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": (
            "await a separate exact-scope timer installation and activation "
            "authorization after the current runtime recovery decision"
            if ready
            else "continue monitoring the active host controller timer"
        ),
        "plan_id": "pqhar_" + plan_digest[:24],
        "semantic_plan_sha256": plan_digest,
        "source_units": {
            "service": {
                "path": str(Path(service_source).absolute()),
                "sha256": service_sha256,
                "bytes": len(service_raw),
                "mode": service_mode,
                "syntax_verified": True,
            },
            "timer": {
                "path": str(Path(timer_source).absolute()),
                "sha256": timer_sha256,
                "bytes": len(timer_raw),
                "mode": timer_mode,
                "syntax_verified": True,
            },
        },
        "observed_state": {
            "service_target": service_installed,
            "timer_target": timer_installed,
            "enabled_state": enabled_state,
            "active_state": active_state,
        },
        "activation_plan": {
            "install_commands": [
                [
                    "/usr/bin/install",
                    "-D",
                    "-m",
                    "0644",
                    str(Path(service_source).absolute()),
                    service_target,
                ],
                [
                    "/usr/bin/install",
                    "-D",
                    "-m",
                    "0644",
                    str(Path(timer_source).absolute()),
                    timer_target,
                ],
            ],
            "reload_command": [
                "/usr/bin/systemctl",
                "--user",
                "daemon-reload",
            ],
            "enable_start_command": [
                "/usr/bin/systemctl",
                "--user",
                "enable",
                "--now",
                TIMER_UNIT,
            ],
            "rollback_commands": [
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "disable",
                    "--now",
                    TIMER_UNIT,
                ],
                ["/usr/bin/unlink", timer_target],
                ["/usr/bin/unlink", service_target],
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "daemon-reload",
                ],
            ],
            "manual_invocation_required": True,
        },
        "action_required": ready,
        "interrupt_operator": False,
        "authorization_required": ready,
        "authorization_recorded": False,
        "installation_authorized": False,
        "activation_authorized": False,
        "installation_performed": False,
        "daemon_reload_performed": False,
        "timer_enablement_performed": False,
        "timer_activation_performed": False,
        "current_evidence_verified": True,
        "receipt_persisted": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    return _persist(result, path=receipt_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage host-controller user-systemd activation readiness. This "
            "never installs, reloads, enables, starts, presents, or sends."
        )
    )
    parser.add_argument("--service-source", type=Path, default=DEFAULT_SERVICE_SOURCE)
    parser.add_argument("--timer-source", type=Path, default=DEFAULT_TIMER_SOURCE)
    parser.add_argument("--user-unit-dir", type=Path, default=DEFAULT_USER_UNIT_DIR)
    parser.add_argument("--write", type=Path, default=DEFAULT_RECEIPT_PATH)
    args = parser.parse_args(argv)
    result = stage_activation_readiness(
        service_source=args.service_source,
        timer_source=args.timer_source,
        user_unit_dir=args.user_unit_dir,
        receipt_path=args.write,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
