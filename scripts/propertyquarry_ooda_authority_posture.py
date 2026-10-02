#!/usr/bin/env python3
"""Refresh the complete non-executing PropertyQuarry authority receipt chain."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_authorization_decision as decision
from scripts import propertyquarry_ooda_authorization_request as request
from scripts import propertyquarry_ooda_configuration_plan as configuration
from scripts import propertyquarry_ooda_runtime_review as review
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_authority_posture.v1"
DEFAULT_POSTURE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/current-authority-posture.json"
)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _blocked_component(
    *,
    schema: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "progress": {"current_evidence_verified": False},
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _safe_verify(
    verifier: Callable[[], dict[str, Any]],
    *,
    schema: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        payload = verifier()
    except Exception:
        return _blocked_component(schema=schema, reason=reason, now=now)
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        return _blocked_component(schema=schema, reason=reason, now=now)
    return dict(payload)


def _persist_verification(
    path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    persisted = dict(payload)
    persisted["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(path).absolute(),
            approved.canonical_json_bytes(persisted),
            overwrite=True,
        )
    except Exception:
        persisted["verification_receipt_persisted"] = False
    return persisted


def refresh_current_authority_posture(
    *,
    posture_path: Path = DEFAULT_POSTURE_PATH,
    review_verification_path: Path = review.DEFAULT_VERIFICATION_PATH,
    request_verification_path: Path = request.DEFAULT_VERIFICATION_PATH,
    decision_verification_path: Path = decision.DEFAULT_VERIFICATION_PATH,
    plan_verification_path: Path = configuration.DEFAULT_VERIFICATION_PATH,
    packet_path: Path = review.DEFAULT_PACKET_PATH,
    request_path: Path = request.DEFAULT_REQUEST_PATH,
    decision_dir: Path = decision.DEFAULT_DECISION_DIR,
    plan_path: Path = configuration.DEFAULT_PLAN_PATH,
    cycle_receipt_path: Path = review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = review.DEFAULT_RELEASE_MANIFEST,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reverify and persist every authority layer without granting or executing it."""

    observed_now = _now(now)
    shared = {
        "packet_path": packet_path,
        "cycle_receipt_path": cycle_receipt_path,
        "signal_dir": signal_dir,
        "live_mobile_receipt_path": live_mobile_receipt_path,
        "release_manifest_path": release_manifest_path,
        "project": project,
        "now": observed_now,
    }
    review_result = _safe_verify(
        lambda: review.verify_current_review_packet(**shared),
        schema=review.VERIFY_SCHEMA,
        reason="runtime_review_verification_failed",
        now=observed_now,
    )
    request_result = _safe_verify(
        lambda: request.verify_current_authorization_request(
            request_path=request_path,
            **shared,
        ),
        schema=request.VERIFY_SCHEMA,
        reason="authorization_request_verification_failed",
        now=observed_now,
    )
    decision_result = _safe_verify(
        lambda: decision.verify_current_authorization_decision(
            decision_dir=decision_dir,
            request_path=request_path,
            **shared,
        ),
        schema=decision.VERIFY_SCHEMA,
        reason="authorization_decision_verification_failed",
        now=observed_now,
    )
    plan_result = _safe_verify(
        lambda: configuration.verify_current_configuration_plan(
            plan_path=plan_path,
            request_path=request_path,
            decision_dir=decision_dir,
            root=root,
            **shared,
        ),
        schema=configuration.VERIFY_SCHEMA,
        reason="configuration_plan_verification_failed",
        now=observed_now,
    )

    persisted = {
        "runtime_review": _persist_verification(
            review_verification_path,
            review_result,
        ),
        "authorization_request": _persist_verification(
            request_verification_path,
            request_result,
        ),
        "authorization_decision": _persist_verification(
            decision_verification_path,
            decision_result,
        ),
        "configuration_plan": _persist_verification(
            plan_verification_path,
            plan_result,
        ),
    }
    all_verifications_persisted = all(
        payload.get("verification_receipt_persisted") is True
        for payload in persisted.values()
    )
    request_current = bool(
        request_result.get("status") == "verified"
        and dict(request_result.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
    )
    decision_current = bool(
        decision_result.get("status") in {"pending", "verified"}
        and dict(decision_result.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
    )
    exact_scope_authorized = bool(
        request_current
        and decision_current
        and decision_result.get("status") == "verified"
        and decision_result.get("exact_scope_authorized") is True
    )
    recorded_authorization_is_current = bool(
        exact_scope_authorized and all_verifications_persisted
    )
    plan_current = bool(
        plan_result.get("status") == "verified"
        and dict(plan_result.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
    )
    authorized_plan_current = (
        exact_scope_authorized and plan_current and all_verifications_persisted
    )
    operator_decision_required = bool(
        request_current
        and decision_current
        and decision_result.get("status") == "pending"
        and all_verifications_persisted
    )
    state = (
        "blocked"
        if not all_verifications_persisted
        else "authorized_exact_scope"
        if authorized_plan_current
        else "pending_operator_decision"
        if operator_decision_required
        else "inactive"
    )
    blocking_reason = (
        "authority_verification_receipt_persistence_incomplete"
        if not all_verifications_persisted
        else ""
        if authorized_plan_current
        else "explicit_operator_decision_required"
        if operator_decision_required
        else "current_authority_chain_unavailable"
    )
    next_action = (
        "repair private verification receipt persistence before presenting or using any authority"
        if not all_verifications_persisted
        else "invoke only the exact consent-bound manual configuration action; deployment and restart remain excluded"
        if authorized_plan_current
        else "record approve_exact_scope, reject, or defer for the exact current request"
        if operator_decision_required
        else "await a fresh approved operator action before staging any new authority"
    )
    component_status = {
        name: {
            "status": str(payload.get("status") or "blocked"),
            "updated_at": str(payload.get("updated_at") or ""),
            "blocking_reason": str(payload.get("blocking_reason") or ""),
            "current_evidence_verified": dict(payload.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True,
            "verification_receipt_persisted": payload.get(
                "verification_receipt_persisted"
            )
            is True,
        }
        for name, payload in persisted.items()
    }
    posture: dict[str, Any] = {
        "schema": SCHEMA,
        "status": state,
        "updated_at": observed_now.isoformat(),
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "progress": {
            "verification_receipt_count": len(persisted),
            "verification_receipt_persisted_count": sum(
                1
                for payload in persisted.values()
                if payload.get("verification_receipt_persisted") is True
            ),
            "request_current": request_current,
            "decision_current": decision_current,
            "plan_current": plan_current,
            "current_evidence_verified": bool(
                authorized_plan_current or operator_decision_required
            ),
        },
        "components": component_status,
        "authorization_required": True,
        "operator_decision_required": operator_decision_required,
        "authorization_recorded": recorded_authorization_is_current,
        "exact_scope_authorized": authorized_plan_current,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    posture["posture_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(posture_path).absolute(),
            approved.canonical_json_bytes(posture),
            overwrite=True,
        )
    except Exception:
        posture["posture_receipt_persisted"] = False
        posture.update(
            {
                "status": "blocked",
                "blocking_reason": "authority_posture_receipt_persistence_failed",
                "next_action": "repair private authority-posture receipt persistence before presenting or using any authority",
                "operator_decision_required": False,
                "authorization_recorded": False,
                "exact_scope_authorized": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
            }
        )
    return posture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the current non-executing PropertyQuarry authority posture."
    )
    parser.add_argument("--write", type=Path, default=DEFAULT_POSTURE_PATH)
    parser.add_argument("--project", default="property")
    args = parser.parse_args(argv)
    posture = refresh_current_authority_posture(
        posture_path=args.write,
        project=args.project,
    )
    print(json.dumps(posture, sort_keys=True))
    return 0 if posture.get("posture_receipt_persisted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
