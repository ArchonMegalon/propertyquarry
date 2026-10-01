#!/usr/bin/env python3
"""Stage the complete non-executing PropertyQuarry runtime-control chain.

This coordinator is intentionally host-side.  It consumes a current runtime
review packet, keeps the expiring authorization request and configuration plan
current, stages (but never executes) the exact manual action when authorized,
and projects only a novel operator interrupt.  It never edits configuration,
deploys, restarts, calls providers, records a presentation, or sends.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_action_only as action_only
from scripts import propertyquarry_ooda_authority_posture as authority_posture
from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_configuration_action_status as configuration_action_status
from scripts import propertyquarry_ooda_configuration_manual_action as manual_action
from scripts import propertyquarry_ooda_configuration_plan as configuration_plan
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_runtime_control.v1"
HOST_HANDOFF_SCHEMA = "propertyquarry.ooda_runtime_control_host_handoff.v1"
DEFAULT_ARTIFACT_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle"
)
DEFAULT_RECEIPT_PATH = DEFAULT_ARTIFACT_DIR / "runtime-control-current.json"
DEFAULT_HOST_HANDOFF_PATH = (
    DEFAULT_ARTIFACT_DIR / "runtime-control-host-handoff.json"
)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _paths(artifact_dir: Path) -> dict[str, Path]:
    root = runtime_review._rooted(artifact_dir)
    return {
        "receipt": root / "runtime-control-current.json",
        "request": root / "runtime-authorization-request.json",
        "request_verification": (
            root / "runtime-authorization-request-verification.json"
        ),
        "decision_dir": root / "runtime-authorization-decisions",
        "decision_verification": (
            root / "runtime-authorization-decision-verification.json"
        ),
        "plan": root / "runtime-configuration-plan.json",
        "plan_verification": (
            root / "runtime-configuration-plan-verification.json"
        ),
        "manual_action_dir": root / "runtime-configuration-manual-actions",
        "manual_action_receipt_dir": (
            root / "runtime-configuration-manual-action-receipts"
        ),
        "manual_action_verification": (
            root / "runtime-configuration-manual-action-verification.json"
        ),
        "preview_dir": root / "runtime-configuration-change-previews",
        "posture": root / "current-authority-posture.json",
    }


def _false_authority(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("automatic_execution_allowed") is not True
        and value.get("execution_authorized") is not True
        and value.get("deployment_or_restart_authorized") is not True
        and value.get("protected_operation_executed") is not True
        and value.get("provider_quota_consumption_allowed") is not True
        and value.get("delivery_authorized") is not True
    )


def _blocked(reason: str, *, now: datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair and reverify the host-side runtime-control evidence; "
            "configuration, deployment, restart, provider, and delivery "
            "authority remain absent"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "presentation_recorded": False,
        "current_evidence_verified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _persist(
    result: dict[str, Any],
    *,
    receipt_path: Path,
) -> dict[str, Any]:
    persisted = {**result, "receipt_persisted": True}
    try:
        atomic_write_bytes(
            runtime_review._rooted(receipt_path),
            runtime_review._canonical(persisted),
            overwrite=True,
        )
    except Exception:
        return {
            **_blocked(
                "runtime_control_receipt_persistence_failed",
                now=_now(),
            ),
            "schema": str(result.get("schema") or SCHEMA),
            "receipt_persisted": False,
        }
    return persisted


def stage_host_runtime_control_handoff(
    *,
    cycle_receipt_path: Path,
    signal_dir: Path,
    receipt_path: Path = DEFAULT_HOST_HANDOFF_PATH,
    now: datetime | None = None,
    max_age_seconds: float = operator_status.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Stage a scheduler-to-host review handoff without claiming host evidence."""

    observed_now = _now(now)
    target = runtime_review._rooted(receipt_path)
    try:
        status = operator_status.load_operator_status(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        source_digest = str(
            status.get("source_cycle_receipt_sha256") or ""
        )
        runtime_actions = [
            dict(action)
            for action in list(status.get("actions") or [])
            if isinstance(action, Mapping)
            and action.get("lane") == "gold_live_runtime"
            and action.get("reason") in runtime_review._REVIEW_ACTION_REASONS
        ]
        if not (
            status.get("status") != "blocked"
            and runtime_review._SHA256.fullmatch(source_digest)
            and status.get("automatic_execution_allowed") is False
            and status.get("protected_operation_executed") is False
            and status.get("provider_quota_consumption_allowed") is False
        ):
            raise ValueError("host_handoff_source_not_admissible")
        if len(runtime_actions) > 1:
            raise ValueError("host_handoff_runtime_action_cardinality_invalid")
    except Exception:
        blocked = {
            **_blocked("host_runtime_control_handoff_not_current", now=observed_now),
            "schema": HOST_HANDOFF_SCHEMA,
            "host_review_required": False,
            "host_runtime_observation_verified": False,
        }
        return _persist(blocked, receipt_path=target)

    if not runtime_actions:
        result = {
            "schema": HOST_HANDOFF_SCHEMA,
            "status": "not_required",
            "updated_at": observed_now.isoformat(),
            "blocking_reason": "",
            "next_action": "await a current verified host-runtime incident",
            "handoff_id": "",
            "source_cycle_receipt_sha256": source_digest,
            "host_review_required": False,
            "host_runtime_observation_verified": False,
            "action_required": False,
            "interrupt_operator": False,
            "current_evidence_verified": True,
            "presentation_recorded": False,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }
        return _persist(result, receipt_path=target)

    action = runtime_actions[0]
    action_sha256 = runtime_review._sha256(runtime_review._canonical(action))
    handoff_id = "pqrh_" + runtime_review._sha256(
        runtime_review._canonical(
            {
                "source_cycle_receipt_sha256": source_digest,
                "action_sha256": action_sha256,
            }
        )
    )[:24]
    result = {
        "schema": HOST_HANDOFF_SCHEMA,
        "status": "host_review_required",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "host_runtime_review_required",
        "next_action": (
            "run one serialized PropertyQuarry safe tick on the deployment host; "
            "the scheduler has no Docker socket, source-write path, deployment "
            "environment authority, or protected-operation authority"
        ),
        "handoff_id": handoff_id,
        "source_cycle_receipt_sha256": source_digest,
        "runtime_action": {
            "lane": "gold_live_runtime",
            "reason": str(action.get("reason") or ""),
            "source_generated_at": str(action.get("source_generated_at") or ""),
            "action_sha256": action_sha256,
        },
        "host_review_required": True,
        "host_runtime_observation_verified": False,
        "action_required": True,
        "interrupt_operator": False,
        "current_evidence_verified": True,
        "presentation_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    return _persist(result, receipt_path=target)


def _manual_action_context(
    context: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "change_id": "local_environment_candidate_manual_apply",
            "compose_project": str(
                dict(context.get("scope") or {}).get("compose_project") or ""
            ),
            "configuration_manual_action": {
                "handoff_id": str(handoff.get("handoff_id") or ""),
                "handoff_sha256": str(handoff.get("handoff_sha256") or ""),
                "request_id": str(handoff.get("request_id") or ""),
                "plan_id": str(handoff.get("plan_id") or ""),
                "preview_id": str(handoff.get("preview_id") or ""),
                "target": str(handoff.get("target") or ""),
                "manual_invocation_required": True,
                "deployment_or_restart_authorized": False,
            },
        },
    }


def _project_operator_action(
    *,
    base_status: Mapping[str, Any],
    presentation_state_path: Path,
    runtime_context: Mapping[str, Any],
    authorization_handoff: Mapping[str, Any],
    configuration_handoff: Mapping[str, Any],
    manual_action_handoff: Mapping[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    decision = str(
        configuration_handoff.get("authorization_decision") or "pending"
    )
    context: dict[str, Any]
    projection_kind: str
    if manual_action_handoff.get("status") == "ready":
        context = _manual_action_context(runtime_context, manual_action_handoff)
        projection_kind = "manual_configuration_apply"
    elif decision == "pending":
        authorization_presentation = (
            action_only._runtime_authorization_presentation(
                authorization_handoff,
                runtime_context,
            )
        )
        context = dict(runtime_context)
        context["scope"] = dict(runtime_context.get("scope") or {})
        context["scope"]["authorization_request"] = authorization_presentation
        projection_kind = "runtime_configuration_authorization"
    else:
        context = {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {
                "change_id": "local_environment_candidate_decision_recorded",
                "decision": decision,
                "request_id": str(
                    authorization_handoff.get("request_id") or ""
                ),
            },
        }
        projection_kind = "decision_recorded"

    projected = operator_status.project_operator_presentation_with_context_hydration(
        dict(base_status),
        state_path=runtime_review._rooted(presentation_state_path),
        presentation_context_by_lane={"gold_live_runtime": context},
        now=now,
    )
    if projected.get("status") == "blocked" or not _false_authority(projected):
        raise ValueError("runtime_control_operator_projection_not_admissible")
    if projection_kind == "decision_recorded":
        projected["interrupt_operator"] = False
        projected["actions"] = []
        projected["status"] = (
            "pending_action"
            if projected.get("action_required") is True
            else str(projected.get("status") or "ready")
        )
        projected["next_action"] = (
            "await a fresh verified runtime change after the recorded operator decision"
        )
        return projected, None, projection_kind

    action_projection = action_only.build_action_only_projection(
        projected,
        presentation_context_by_lane={"gold_live_runtime": context},
    )
    if action_projection is not None and projection_kind == "manual_configuration_apply":
        next_action = str(manual_action_handoff.get("next_action") or "").strip()
        action_projection["blocking_reason"] = ""
        action_projection["next_action"] = next_action
        action_projection["configuration_manual_action"] = {
            key: manual_action_handoff.get(key)
            for key in (
                "handoff_id",
                "handoff_sha256",
                "handoff_path",
                "request_id",
                "plan_id",
                "preview_id",
                "target",
                "apply_command",
                "rollback_command_source",
                "manual_invocation_required",
                "manual_apply_authorized",
                "deployment_or_restart_authorized",
            )
        }
        for action in list(action_projection.get("actions") or []):
            if isinstance(action, dict) and action.get("lane") == "gold_live_runtime":
                action["safe_next_action"] = next_action
    return projected, action_projection, projection_kind


def _post_configuration_posture(
    review_verification: Mapping[str, Any],
) -> dict[str, Any] | None:
    proposal = review_verification.get("configuration_proposal")
    recovery = proposal.get("recovery_preview") if isinstance(proposal, Mapping) else None
    environment = (
        recovery.get("deployment_environment")
        if isinstance(recovery, Mapping)
        else None
    )
    intake = environment.get("intake_plan") if isinstance(environment, Mapping) else None
    if not (
        isinstance(proposal, Mapping)
        and isinstance(recovery, Mapping)
        and isinstance(environment, Mapping)
        and isinstance(intake, Mapping)
        and environment.get("configuration_complete") is True
        and environment.get("missing_required_key_count") == 0
        and environment.get("unresolved_required_key_count") == 0
        and intake.get("local_merge_review_required") is False
        and intake.get("operator_merge_review_required") is False
        and environment.get("environment_values_recorded") is False
        and environment.get("environment_values_hashed") is False
        and environment.get("secret_values_recorded") is False
        and recovery.get("deployment_or_restart_performed") is False
        and recovery.get("execution_authorized") is False
    ):
        return None
    blockers = [
        str(value)
        for value in list(recovery.get("blocking_reasons") or [])
        if str(value).strip()
    ]
    return {
        "operation": "deployment_or_restart",
        "compose_project": str(recovery.get("compose_project") or ""),
        "application_service": str(recovery.get("application_service") or ""),
        "connector_service": str(recovery.get("connector_service") or ""),
        "recovery_preview_sha256": str(
            proposal.get("recovery_preview_sha256") or ""
        ),
        "eligible_for_authorization": recovery.get("eligible_for_authorization")
        is True,
        "blocking_reasons": blockers,
        "configuration_complete": True,
        "missing_required_key_count": 0,
        "unresolved_required_key_count": 0,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
    }


def _stage_post_configuration_runtime_control(
    *,
    review_verification: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    post_configuration: Mapping[str, Any],
    paths: Mapping[str, Path],
    target: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    presentation_state_path: Path,
    root: Path,
    now: datetime,
) -> dict[str, Any]:
    try:
        current_action = configuration_action_status.inspect_manual_action_status(
            receipt_dir=paths["manual_action_receipt_dir"],
            handoff_dir=paths["manual_action_dir"],
            refresh_receipt_dir=(
                runtime_review._rooted(DEFAULT_ARTIFACT_DIR)
                / "runtime-configuration-evidence-refresh-receipts"
            ),
            root=root,
            now=now,
        )
        if not (
            current_action.get("status") in {"ready", "action_required", "pending_action"}
            and current_action.get("current_evidence_verified") is True
            and current_action.get("receipt_verified") is True
            and current_action.get("source_state_verified") is True
            and current_action.get("automatic_execution_allowed") is False
            and current_action.get("deployment_or_restart_authorized") is False
            and current_action.get("deployment_or_restart_performed") is False
            and current_action.get("provider_quota_consumption_allowed") is False
            and current_action.get("delivery_authorized") is False
        ):
            raise ValueError("post_configuration_action_not_current")
        base_status = operator_status.load_operator_status(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            now=now,
        )
        if base_status.get("status") == "blocked" or not _false_authority(base_status):
            raise ValueError("post_configuration_operator_status_not_current")

        action_pending = current_action.get("action_required") is True
        if action_pending:
            projection_kind = "configuration_evidence_refresh"
            next_action = str(current_action.get("next_action") or "")
            context_scope = {
                "change_id": "local_environment_candidate_evidence_refresh",
                "compose_project": str(post_configuration.get("compose_project") or ""),
                "configuration_action": {
                    "state": str(current_action.get("state") or ""),
                    "receipt_id": str(current_action.get("receipt_id") or ""),
                    "receipt_sha256": str(current_action.get("receipt_sha256") or ""),
                    "action_command": str(current_action.get("action_command") or ""),
                    "environment_values_recorded": False,
                    "environment_values_hashed": False,
                    "deployment_or_restart_authorized": False,
                },
            }
        else:
            projection_kind = "deployment_restart_review"
            blockers = list(post_configuration.get("blocking_reasons") or [])
            if post_configuration.get("eligible_for_authorization") is True:
                next_action = (
                    "stage a fresh exact deployment/restart authorization request; "
                    "do not deploy or restart before that separate decision is recorded"
                )
            elif blockers:
                next_action = (
                    "resolve the current deployment preview blockers before staging "
                    "deployment/restart authorization: " + ", ".join(blockers)
                )
            else:
                next_action = (
                    "reverify the exact deployment preview before staging any "
                    "deployment/restart authorization"
                )
            context_scope = {
                "change_id": "propertyquarry_edge_connector_recovery_deployment_review",
                "deployment_review": dict(post_configuration),
            }
        context = {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": context_scope,
        }
        projected = operator_status.project_operator_presentation_with_context_hydration(
            dict(base_status),
            state_path=runtime_review._rooted(presentation_state_path),
            presentation_context_by_lane={"gold_live_runtime": context},
            now=now,
        )
        if projected.get("status") == "blocked" or not _false_authority(projected):
            raise ValueError("post_configuration_projection_not_admissible")
        action_projection = action_only.build_action_only_projection(
            projected,
            presentation_context_by_lane={"gold_live_runtime": context},
        )
        if action_projection is not None:
            action_projection["blocking_reason"] = ""
            action_projection["next_action"] = next_action
            action_projection["post_configuration"] = {
                "projection_kind": projection_kind,
                "deployment_review": dict(post_configuration),
                "configuration_action_state": str(current_action.get("state") or ""),
            }
            for action in list(action_projection.get("actions") or []):
                if isinstance(action, dict) and action.get("lane") == "gold_live_runtime":
                    action["safe_next_action"] = next_action
    except Exception:
        return _persist(
            _blocked("runtime_control_post_configuration_not_current", now=now),
            receipt_path=target,
        )

    result = {
        "schema": SCHEMA,
        "status": "verified",
        "updated_at": now.isoformat(),
        "blocking_reason": str(projected.get("blocking_reason") or ""),
        "next_action": next_action,
        "action_required": projected.get("action_required") is True,
        "interrupt_operator": bool(
            projected.get("interrupt_operator") is True
            and action_projection is not None
        ),
        "presentation_recorded": False,
        "projection_kind": projection_kind,
        "novel_action_count": 1 if action_projection is not None else 0,
        "action_projection": dict(action_projection or {}),
        "current_evidence_verified": True,
        "components": {
            "runtime_review": {
                "status": "verified",
                "updated_at": str(review_verification.get("updated_at") or ""),
                "blocking_reason": str(review_verification.get("blocking_reason") or ""),
                "current_evidence_verified": True,
            },
            "authorization_request": {
                "status": "not_required",
                "current_evidence_verified": True,
                "deployment_or_restart_authorized": False,
            },
            "configuration_plan": {
                "status": "completed",
                "current_evidence_verified": True,
                "apply_performed": True,
                "deployment_or_restart_authorized": False,
            },
            "manual_configuration_action": dict(current_action),
            "deployment_review": dict(post_configuration),
            "authority_posture": {
                "status": "inactive",
                "current_evidence_verified": True,
                "automatic_execution_allowed": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "protected_operation_executed": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            },
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    return _persist(result, receipt_path=target)


def stage_current_runtime_control(
    *,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    receipt_path: Path | None = None,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    review_verification_path: Path = runtime_review.DEFAULT_VERIFICATION_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    presentation_state_path: Path = operator_status.DEFAULT_PRESENTATION_STATE,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stage and verify every host-side consent artifact without executing it."""

    observed_now = _now(now)
    paths = _paths(artifact_dir)
    target = runtime_review._rooted(receipt_path or paths["receipt"])
    shared = {
        "packet_path": packet_path,
        "cycle_receipt_path": cycle_receipt_path,
        "signal_dir": signal_dir,
        "live_mobile_receipt_path": live_mobile_receipt_path,
        "release_manifest_path": release_manifest_path,
        "project": project,
        "now": observed_now,
    }
    stage_name = "runtime_review"
    try:
        review_verification = runtime_review.verify_current_review_packet(
            deployment_env_path=deployment_env_path,
            **shared,
        )
        if not (
            review_verification.get("status") == "verified"
            and dict(review_verification.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and _false_authority(review_verification)
        ):
            raise ValueError("runtime_review_not_current")
        runtime_context = runtime_review.operator_presentation_context(
            review_verification
        )
        if not (
            runtime_context.get("schema")
            == "propertyquarry.ooda_operator_presentation_context.v1"
            and runtime_context.get("lane") == "gold_live_runtime"
        ):
            raise ValueError("runtime_context_not_admissible")

        post_configuration = _post_configuration_posture(review_verification)
        if post_configuration is not None:
            return _stage_post_configuration_runtime_control(
                review_verification=review_verification,
                runtime_context=runtime_context,
                post_configuration=post_configuration,
                paths=paths,
                target=target,
                cycle_receipt_path=cycle_receipt_path,
                signal_dir=signal_dir,
                presentation_state_path=presentation_state_path,
                root=root,
                now=observed_now,
            )

        stage_name = "authorization_request"
        authorization_handoff = (
            authorization_request.stage_current_authorization_handoff(
                request_path=paths["request"],
                deployment_env_path=deployment_env_path,
                **shared,
            )
        )
        if not (
            authorization_handoff.get("status") == "ready"
            and authorization_handoff.get("current_evidence_verified") is True
            and _false_authority(authorization_handoff)
        ):
            raise ValueError("runtime_authorization_handoff_not_current")

        stage_name = "configuration_plan"
        configuration_handoff = (
            configuration_plan.stage_current_configuration_handoff(
                plan_path=paths["plan"],
                request_path=paths["request"],
                decision_dir=paths["decision_dir"],
                root=root,
                **shared,
            )
        )
        if not (
            configuration_handoff.get("status") == "ready"
            and configuration_handoff.get("current_evidence_verified") is True
            and _false_authority(configuration_handoff)
            and configuration_handoff.get("automatic_apply_allowed") is False
            and configuration_handoff.get("apply_performed") is False
        ):
            raise ValueError("runtime_configuration_handoff_not_current")

        stage_name = "manual_configuration_action"
        manual_action_handoff = manual_action.stage_current_manual_action_handoff(
            handoff_dir=paths["manual_action_dir"],
            preview_dir=paths["preview_dir"],
            plan_path=paths["plan"],
            request_path=paths["request"],
            decision_dir=paths["decision_dir"],
            root=root,
            **shared,
        )
        if not (
            manual_action_handoff.get("status") in {"not_authorized", "ready"}
            and manual_action_handoff.get("current_evidence_verified") is True
            and manual_action_handoff.get("automatic_execution_allowed") is False
            and manual_action_handoff.get("source_edit_performed") is False
            and manual_action_handoff.get("deployment_or_restart_authorized") is False
            and manual_action_handoff.get("provider_quota_consumption_allowed") is False
            and manual_action_handoff.get("delivery_authorized") is False
        ):
            raise ValueError("runtime_manual_action_handoff_not_current")

        stage_name = "authority_posture"
        posture = authority_posture.refresh_current_authority_posture(
            posture_path=paths["posture"],
            review_verification_path=review_verification_path,
            request_verification_path=paths["request_verification"],
            decision_verification_path=paths["decision_verification"],
            plan_verification_path=paths["plan_verification"],
            request_path=paths["request"],
            decision_dir=paths["decision_dir"],
            plan_path=paths["plan"],
            root=root,
            **shared,
        )
        if not (
            posture.get("status")
            in {"pending_operator_decision", "authorized_exact_scope", "inactive"}
            and posture.get("posture_receipt_persisted") is True
            and posture.get("automatic_execution_allowed") is False
            and posture.get("execution_authorized") is False
            and posture.get("deployment_or_restart_authorized") is False
            and posture.get("protected_operation_executed") is False
            and posture.get("provider_quota_consumption_allowed") is False
            and posture.get("delivery_authorized") is False
        ):
            raise ValueError("runtime_authority_posture_not_current")

        stage_name = "operator_projection"
        base_status = operator_status.load_operator_status(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            now=observed_now,
        )
        projected, action_projection, projection_kind = _project_operator_action(
            base_status=base_status,
            presentation_state_path=presentation_state_path,
            runtime_context=runtime_context,
            authorization_handoff=authorization_handoff,
            configuration_handoff=configuration_handoff,
            manual_action_handoff=manual_action_handoff,
            now=observed_now,
        )
    except Exception:
        return _persist(
            _blocked(
                f"runtime_control_{stage_name}_not_current",
                now=observed_now,
            ),
            receipt_path=target,
        )

    action_required = projected.get("action_required") is True
    interrupt_operator = bool(
        projected.get("interrupt_operator") is True
        and action_projection is not None
    )
    result = {
        "schema": SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": str(projected.get("blocking_reason") or ""),
        "next_action": str(
            (action_projection or {}).get("next_action")
            or projected.get("next_action")
            or ""
        ),
        "action_required": action_required,
        "interrupt_operator": interrupt_operator,
        "presentation_recorded": False,
        "projection_kind": projection_kind,
        "novel_action_count": 1 if action_projection is not None else 0,
        "action_projection": dict(action_projection or {}),
        "current_evidence_verified": True,
        "components": {
            "runtime_review": {
                "status": "verified",
                "updated_at": str(review_verification.get("updated_at") or ""),
                "blocking_reason": str(
                    review_verification.get("blocking_reason") or ""
                ),
                "current_evidence_verified": True,
            },
            "authorization_request": dict(authorization_handoff),
            "configuration_plan": dict(configuration_handoff),
            "manual_configuration_action": dict(manual_action_handoff),
            "authority_posture": dict(posture),
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
    return _persist(result, receipt_path=target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage the complete host-side PropertyQuarry runtime-control chain. "
            "This command never applies, deploys, restarts, calls providers, "
            "records a presentation, or sends."
        )
    )
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--write", type=Path, default=None)
    parser.add_argument("--project", default="property")
    args = parser.parse_args(argv)
    result = stage_current_runtime_control(
        artifact_dir=args.artifact_dir,
        receipt_path=args.write,
        project=args.project,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
