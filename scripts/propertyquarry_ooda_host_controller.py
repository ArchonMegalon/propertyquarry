#!/usr/bin/env python3
"""Run one host-only PropertyQuarry OODA tick without operator side effects.

This controller is suitable for a host timer.  It consumes the scheduler's
advisory host handoff when present, but the handoff never confers authority.
The controller runs the existing serialized evaluate-only safe tick and writes
one private receipt.  It never records a presentation, sends, edits runtime
configuration, deploys, restarts, or calls a quota-consuming provider.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_runtime_control as runtime_control
from scripts import propertyquarry_ooda_safe_tick as safe_tick
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_host_controller.v1"
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "host-controller-latest.json"
)
DEFAULT_HANDOFF_PATH = runtime_control.DEFAULT_HOST_HANDOFF_PATH
DEFAULT_HANDOFF_MAX_AGE_SECONDS = 1800.0
MAX_HANDOFF_BYTES = 512 * 1024


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _false_authority(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and value.get("delivery_attempted") is False
        and value.get("sent") is False
    )


def _handoff_projection(
    path: Path,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    target = runtime_control.runtime_review._rooted(path)
    try:
        target.lstat()
    except FileNotFoundError:
        return {
            "status": "absent",
            "path": str(path),
            "sha256": "",
            "host_review_required": False,
            "handoff_confers_authority": False,
        }
    except OSError:
        return {
            "status": "not_admissible",
            "path": str(path),
            "sha256": "",
            "host_review_required": False,
            "handoff_confers_authority": False,
        }
    try:
        handoff, _raw, digest = load_strict_json_object_snapshot(
            target,
            field="runtime control host handoff",
            maximum_bytes=MAX_HANDOFF_BYTES,
        )
    except Exception:
        return {
            "status": "not_admissible",
            "path": str(path),
            "sha256": "",
            "host_review_required": False,
            "handoff_confers_authority": False,
        }

    updated_at = _timestamp(handoff.get("updated_at"))
    age_seconds = (
        (now - updated_at).total_seconds()
        if updated_at is not None
        else float("nan")
    )
    status = str(handoff.get("status") or "")
    expected_required = status == "host_review_required"
    if not (
        handoff.get("schema") == runtime_control.HOST_HANDOFF_SCHEMA
        and status in {"host_review_required", "not_required"}
        and handoff.get("receipt_persisted") is True
        and handoff.get("host_review_required") is expected_required
        and handoff.get("host_runtime_observation_verified") is False
        and handoff.get("interrupt_operator") is False
        and _false_authority(handoff)
        and updated_at is not None
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= max_age_seconds
    ):
        return {
            "status": "not_admissible",
            "path": str(path),
            "sha256": digest,
            "host_review_required": False,
            "handoff_confers_authority": False,
        }
    return {
        "status": status,
        "path": str(path),
        "sha256": digest,
        "updated_at": updated_at.isoformat(),
        "handoff_id": str(handoff.get("handoff_id") or ""),
        "host_review_required": expected_required,
        "handoff_confers_authority": False,
    }


def _blocked(
    reason: str,
    *,
    now: datetime,
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair and rerun the host evaluate-only controller; no operator "
            "presentation or protected operation was attempted"
        ),
        "scheduler_handoff": dict(handoff),
        "host_safe_tick_completed": False,
        "operator_presentation_required": False,
        "action_required": False,
        "interrupt_operator": False,
        "presentation_recorded": False,
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


def _persist(result: dict[str, Any], *, path: Path) -> dict[str, Any]:
    persisted = {**result, "receipt_persisted": True}
    try:
        target = runtime_control.runtime_review._rooted(path)
        atomic_write_bytes(
            target,
            runtime_control.runtime_review._canonical(persisted),
            overwrite=True,
        )
        observed, _raw, _digest = load_strict_json_object_snapshot(
            target,
            field="host controller receipt",
            maximum_bytes=512 * 1024,
        )
        if observed != persisted:
            raise ValueError("host_controller_receipt_write_not_verified")
    except Exception:
        return {**result, "receipt_persisted": False}
    return persisted


def run_host_controller_once(
    *,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    handoff_max_age_seconds: float = DEFAULT_HANDOFF_MAX_AGE_SECONDS,
    now: datetime | None = None,
    safe_tick_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one serialized host safe tick and publish non-authorizing posture."""

    observed_now = _now(now)
    if (
        isinstance(handoff_max_age_seconds, bool)
        or not isinstance(handoff_max_age_seconds, (int, float))
        or not math.isfinite(float(handoff_max_age_seconds))
        or not 60.0 <= float(handoff_max_age_seconds) <= 24.0 * 3600.0
    ):
        handoff = {
            "status": "not_evaluated",
            "path": str(handoff_path),
            "host_review_required": False,
            "handoff_confers_authority": False,
        }
        return _persist(
            _blocked(
                "host_controller_handoff_freshness_policy_invalid",
                now=observed_now,
                handoff=handoff,
            ),
            path=receipt_path,
        )

    handoff = _handoff_projection(
        handoff_path,
        now=observed_now,
        max_age_seconds=float(handoff_max_age_seconds),
    )
    try:
        tick_kwargs = dict(safe_tick_kwargs or {})
        tick_kwargs["now"] = observed_now
        tick = safe_tick.run_safe_tick(**tick_kwargs)
    except Exception:
        return _persist(
            _blocked(
                "host_safe_tick_execution_failed",
                now=observed_now,
                handoff=handoff,
            ),
            path=receipt_path,
        )

    components = tick.get("components")
    runtime_component = (
        components.get("runtime_control")
        if isinstance(components, Mapping)
        else None
    )
    if not (
        tick.get("status") == "ready"
        and tick.get("action_required") in {True, False}
        and tick.get("interrupt_operator") in {True, False}
        and _false_authority(tick)
        and isinstance(runtime_component, Mapping)
        and runtime_component.get("status") in {"verified", "not_required"}
        and runtime_component.get("current_evidence_verified") is True
        and runtime_component.get("receipt_persisted") in {True, False}
        and _false_authority(runtime_component)
    ):
        failure = _blocked(
            "host_safe_tick_not_ready",
            now=observed_now,
            handoff=handoff,
        )
        failure["safe_tick"] = {
            "status": str(tick.get("status") or ""),
            "blocking_reason": str(tick.get("blocking_reason") or ""),
            "result_sha256": runtime_control.runtime_review._sha256(
                runtime_control.runtime_review._canonical(tick)
            ),
        }
        return _persist(failure, path=receipt_path)

    presentation_required = tick.get("interrupt_operator") is True
    result = {
        "schema": SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": (
            "surface the current action-only projection through an actual "
            "operator channel, then record that presentation"
            if presentation_required
            else str(tick.get("next_action") or "")
        ),
        "scheduler_handoff": handoff,
        "safe_tick": {
            "status": "ready",
            "updated_at": str(tick.get("updated_at") or ""),
            "result_sha256": runtime_control.runtime_review._sha256(
                runtime_control.runtime_review._canonical(tick)
            ),
            "action_required": tick.get("action_required") is True,
            "interrupt_operator": presentation_required,
            "runtime_control_status": str(
                runtime_component.get("status") or ""
            ),
        },
        "host_safe_tick_completed": True,
        "operator_presentation_required": presentation_required,
        "action_required": tick.get("action_required") is True,
        "interrupt_operator": False,
        "presentation_recorded": False,
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
            "Run one host-only PropertyQuarry evaluate/stage tick. This never "
            "presents, sends, applies, deploys, restarts, or calls providers."
        )
    )
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF_PATH)
    parser.add_argument("--write", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument(
        "--handoff-max-age-seconds",
        type=float,
        default=DEFAULT_HANDOFF_MAX_AGE_SECONDS,
    )
    args = parser.parse_args(argv)
    result = run_host_controller_once(
        handoff_path=args.handoff,
        receipt_path=args.write,
        handoff_max_age_seconds=args.handoff_max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
