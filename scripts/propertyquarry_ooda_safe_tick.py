#!/usr/bin/env python3
"""Run one serialized, manifest-bound PropertyQuarry OODA evaluation tick."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_notify_gold_status as gold_notify
from scripts import propertyquarry_notify_scene_video_provider_refresh as scene_notify
from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_public_origin_observation as public_origin
from scripts import propertyquarry_ooda_runtime_control as runtime_control
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts import propertyquarry_ooda_source_refresh_settlement as source_settlement
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification
from scripts import propertyquarry_stage_ooda_signals as stage
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_safe_tick.v1"
DEFAULT_SIGNAL_DIR = Path(stage.DEFAULT_TARGET_DIR)
DEFAULT_STAGE_RECEIPT = Path(stage.DEFAULT_RECEIPT_PATH)
DEFAULT_CYCLE_RECEIPT = Path(cycle.DEFAULT_RECEIPT_PATH)
DEFAULT_TICK_RECEIPT = Path(
    "_completion/propertyquarry_ooda_notification_cycle/safe-tick-latest.json"
)
DEFAULT_TICK_LOCK = Path(
    "_completion/propertyquarry_ooda_notification_cycle/safe-tick.lock"
)
DEFAULT_OPERATOR_PRESENTATION_STATE = Path(
    operator_status.DEFAULT_PRESENTATION_STATE
)
DEFAULT_PUBLIC_ORIGIN_OBSERVATION = Path(public_origin.DEFAULT_RECEIPT_PATH)
DEFAULT_RUNTIME_REVIEW_PACKET = Path(runtime_review.DEFAULT_PACKET_PATH)
DEFAULT_RUNTIME_REVIEW_VERIFICATION = Path(
    runtime_review.DEFAULT_VERIFICATION_PATH
)
DEFAULT_RUNTIME_REVIEW_RELEASE_MANIFEST = Path(
    runtime_review.DEFAULT_RELEASE_MANIFEST
)
DEFAULT_RUNTIME_REVIEW_DEPLOYMENT_ENV = Path(
    runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH
)
DEFAULT_SOURCE_REFRESH_REQUEST = Path(source_refresh.DEFAULT_REQUEST_PATH)
DEFAULT_SOURCE_REFRESH_VERIFICATION = Path(
    source_refresh.DEFAULT_VERIFICATION_PATH
)
DEFAULT_SOURCE_REFRESH_HANDOFF = Path(source_handoff.DEFAULT_HANDOFF_PATH)
DEFAULT_SOURCE_REFRESH_HANDOFF_VERIFICATION = Path(
    source_handoff.DEFAULT_VERIFICATION_PATH
)
DEFAULT_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY = Path(
    source_claims.DEFAULT_TRUST_REGISTRY_PATH
)
DEFAULT_SOURCE_REFRESH_CLAIM_DIR = Path(source_claims.DEFAULT_CLAIM_DIR)
DEFAULT_SOURCE_REFRESH_CLAIMS = Path(source_claims.DEFAULT_RECEIPT_PATH)
DEFAULT_SOURCE_REFRESH_CLAIMS_VERIFICATION = Path(
    source_claims.DEFAULT_VERIFICATION_PATH
)
DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR = Path(
    trust_intake.DEFAULT_CANDIDATE_DIR
)
DEFAULT_SOURCE_REFRESH_TRUST_INTAKE = Path(trust_intake.DEFAULT_RECEIPT_PATH)
DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION = Path(
    trust_intake.DEFAULT_VERIFICATION_PATH
)
DEFAULT_SOURCE_REFRESH_TRUST_DECISION_DIR = Path(
    trust_decision.DEFAULT_DECISION_DIR
)
DEFAULT_SOURCE_REFRESH_TRUST_PRESENTATION_STATE = Path(
    trust_notification.DEFAULT_PRESENTATION_STATE_PATH
)
DEFAULT_SOURCE_REFRESH_TRUST_NOTIFICATION = Path(
    trust_notification.DEFAULT_RECEIPT_PATH
)
DEFAULT_SOURCE_REFRESH_COMPLETION_DIR = Path(
    source_settlement.DEFAULT_COMPLETION_DIR
)
DEFAULT_SOURCE_REFRESH_SETTLEMENT = Path(source_settlement.DEFAULT_RECEIPT_PATH)
DEFAULT_SOURCE_REFRESH_SETTLEMENT_VERIFICATION = Path(
    source_settlement.DEFAULT_VERIFICATION_PATH
)
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
RUNTIME_REVIEW_COMPONENT_SCHEMA = (
    "propertyquarry.ooda_safe_tick_runtime_review_component.v1"
)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _absolute_path(value: Path | str) -> Path:
    candidate = Path(
        os.path.abspath(os.fspath(Path(value).expanduser()))
    )
    if candidate == Path("/") or candidate.name in {"", ".", ".."}:
        raise ValueError("safe_tick_path_not_admissible")
    return candidate


def _acquire_tick_lock(path: Path) -> int | None:
    target = _absolute_path(path)
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = target.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError("safe_tick_lock_directory_not_admissible")
    descriptor = os.open(
        target,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("safe_tick_lock_not_admissible")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _release_tick_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _blocked(
    reason: str,
    *,
    now: datetime,
    stage_completed: bool = False,
    cycle_completed: bool = False,
    runtime_review_verified: bool = False,
    error_type: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the local evaluate-only tick inputs or receipt lane, then retry; "
            "deployment and restart remain separately consent-gated"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "progress": {
            "signal_stage_completed": stage_completed,
            "evaluate_only_cycle_completed": cycle_completed,
            "runtime_review_verified": runtime_review_verified,
            "runtime_control_verified": False,
            "source_refresh_request_verified": False,
            "source_refresh_handoff_verified": False,
            "source_refresh_claims_verified": False,
            "source_refresh_trust_intake_verified": False,
            "source_refresh_trust_decision_verified": False,
            "source_refresh_trust_notification_verified": False,
            "source_refresh_settlement_verified": False,
            "current_evidence_verified": False,
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
    if error_type:
        result["error_type"] = error_type
    return result


def _persisted_component(
    path: Path,
    *,
    expected: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    target = _absolute_path(path)
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{label}_receipt_not_admissible")
    payload, raw, digest = load_strict_json_object_snapshot(
        target,
        field=label,
        maximum_bytes=MAX_COMPONENT_BYTES,
    )
    if payload != dict(expected):
        raise ValueError(f"{label}_receipt_not_current")
    return {
        "path": str(target),
        "sha256": digest,
        "bytes": len(raw),
        "mode": stat.S_IMODE(metadata.st_mode),
        "current_evidence_verified": True,
    }


def _stage_is_admissible(report: Mapping[str, Any]) -> bool:
    publication_status = str(report.get("publication_status") or "")
    source_posture = report.get("source_evidence_posture")
    progress = (
        dict(source_posture.get("progress") or {})
        if isinstance(source_posture, Mapping)
        else {}
    )
    source_status = str(report.get("source_status") or "")
    posture_status = (
        str(source_posture.get("status") or "")
        if isinstance(source_posture, Mapping)
        else ""
    )
    publication_is_current = bool(
        (
            publication_status == "approved"
            and posture_status
            in {"verified_current", "waiting_for_fresh_sources"}
            and int(progress.get("unavailable_lane_count") or 0) == 0
        )
        or (
            publication_status == "revoked"
            and posture_status == "waiting_for_fresh_sources"
            and int(progress.get("unavailable_lane_count") or 0) > 0
        )
    )
    return bool(
        report.get("schema") == stage.SCHEMA
        and report.get("status") == "ready"
        and report.get("blocking_reason") == ""
        and publication_is_current
        and source_status == posture_status
        and stage._SHA256.fullmatch(str(report.get("manifest_sha256") or ""))
        and report.get("receipt_persisted") is True
        and report.get("automatic_execution_allowed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("protected_operation_executed") is False
    )


def _cycle_is_admissible(report: Mapping[str, Any]) -> bool:
    approval = report.get("signal_approval")
    approved_snapshot = bool(
        isinstance(approval, Mapping)
        and approval.get("approved") is True
        and approval.get("reason") == "approved_projection_manifest_verified"
        and approval.get("policy") == approved.POLICY
    )
    verified_revocation = bool(
        isinstance(approval, Mapping)
        and approval.get("approved") is False
        and approval.get("revocation_verified") is True
        and approval.get("reason") == "approved_projection_manifest_revoked"
        and approval.get("policy") == approved.POLICY
        and report.get("status") == "silent"
        and report.get("publication_status") == "revoked"
        and report.get("operator_action_required") is False
        and report.get("interrupt_operator") is False
        and int(report.get("action_required_count") or 0) == 0
        and int(report.get("novel_action_count") or 0) == 0
        and not list(report.get("actions") or [])
    )
    return bool(
        report.get("schema") == cycle.SCHEMA
        and report.get("status") in {"silent", "action_required", "deduplicated"}
        and report.get("execution_mode") == "evaluate_only"
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("protected_operation_executed") is False
        and (approved_snapshot or verified_revocation)
    )


def _operator_projection_is_admissible(
    report: Mapping[str, Any],
    *,
    cycle_evidence: Mapping[str, Any],
    presentation_state_path: Path,
) -> bool:
    """Require one current, presentation-aware, non-authorizing projection."""

    status = str(report.get("status") or "")
    action_required = report.get("action_required")
    interrupt_operator = report.get("interrupt_operator")
    actions = report.get("actions")
    pending_actions = report.get("pending_actions", [])
    progress = report.get("progress")
    presentation = report.get("presentation")
    source_digest = str(report.get("source_cycle_receipt_sha256") or "")
    state_status = (
        str(presentation.get("state_status") or "")
        if isinstance(presentation, Mapping)
        else ""
    )
    state_digest = (
        str(presentation.get("state_sha256") or "")
        if isinstance(presentation, Mapping)
        else ""
    )
    if not (
        report.get("schema") == operator_status.SCHEMA
        and status
        in {"ready", "action_required", "pending_action", "waiting_for_evidence"}
        and isinstance(action_required, bool)
        and isinstance(interrupt_operator, bool)
        and isinstance(actions, list)
        and all(isinstance(action, Mapping) for action in actions)
        and isinstance(pending_actions, list)
        and all(isinstance(action, Mapping) for action in pending_actions)
        and isinstance(progress, Mapping)
        and progress.get("current_snapshot_hashes_verified") is True
        and operator_status._SHA256.fullmatch(source_digest)
        and source_digest == str(cycle_evidence.get("sha256") or "")
        and isinstance(presentation, Mapping)
        and presentation.get("state_path")
        == str(_absolute_path(presentation_state_path))
        and state_status in {"missing", "ready"}
        and (
            not state_digest
            if state_status == "missing"
            else operator_status._SHA256.fullmatch(state_digest) is not None
        )
        and presentation.get("pending_action_count") == len(pending_actions)
        and presentation.get("novel_action_count") == len(actions)
        and presentation.get("delivery_state_independent") is True
        and presentation.get("delivery_state_updated") is False
        and action_required is bool(pending_actions)
        and interrupt_operator is bool(actions)
        and (not interrupt_operator or action_required)
        and report.get("automatic_execution_allowed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("protected_operation_executed") is False
        and isinstance(report.get("blocking_reason"), str)
        and isinstance(report.get("next_action"), str)
        and str(report.get("next_action") or "").strip()
    ):
        return False
    return bool(
        (status == "action_required" and action_required and interrupt_operator)
        or (status == "pending_action" and action_required and not interrupt_operator)
        or (
            status in {"ready", "waiting_for_evidence"}
            and not action_required
            and not interrupt_operator
        )
    )


def _load_operator_projection(
    *,
    cycle_receipt_path: Path,
    signal_dir: Path,
    presentation_state_path: Path,
    cycle_evidence: Mapping[str, Any],
    runtime_review_report: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    report = operator_status.load_operator_status(
        cycle_receipt_path=_absolute_path(cycle_receipt_path),
        signal_dir=_absolute_path(signal_dir),
        max_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
        now=now,
    )
    contexts: dict[str, Mapping[str, Any]] = {}
    if runtime_review_report.get("status") == "verified":
        context = runtime_review_report.get("presentation_context")
        if not (
            isinstance(context, Mapping)
            and context.get("lane") == "gold_live_runtime"
            and context.get("schema")
            == "propertyquarry.ooda_operator_presentation_context.v1"
        ):
            raise ValueError("runtime_review_presentation_context_not_admissible")
        contexts["gold_live_runtime"] = context
    report = operator_status.project_operator_presentation_with_context_hydration(
        report,
        state_path=_absolute_path(presentation_state_path),
        presentation_context_by_lane=contexts,
        now=now,
    )
    if not _operator_projection_is_admissible(
        report,
        cycle_evidence=cycle_evidence,
        presentation_state_path=presentation_state_path,
    ):
        raise ValueError("operator_projection_not_admissible")
    return report


def _notification_state_incident_is_admissible(
    report: Mapping[str, Any],
) -> bool:
    """Accept only the producer's exact quiet, fail-closed ledger incident."""

    return bool(
        report.get("schema") == cycle.SCHEMA
        and report.get("status") == "notification_state_invalid"
        and report.get("execution_mode") == "evaluate_only"
        and report.get("notification_state_status") == "invalid"
        and report.get("notification_state_admissible") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
        and report.get("would_send") is False
        and report.get("notification_count") == 0
        and report.get("message_ids") == []
        and report.get("delivery_mode") == ""
        and report.get("state_updated") is False
        and report.get("operator_action_required") is False
        and report.get("interrupt_operator") is False
        and report.get("action_required_count") == 0
        and report.get("novel_action_count") == 0
        and report.get("active_action_count_before") == 0
        and report.get("active_action_count_projected") == 0
        and report.get("active_action_count_after") == 0
        and report.get("actions") == []
        and report.get("automatic_execution_allowed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("protected_operation_executed") is False
        and report.get("next_action")
        == (
            "repair or explicitly replace the private notification incident ledger before "
            "another send-enabled cycle"
        )
    )


def _source_refresh_is_admissible(report: Mapping[str, Any]) -> bool:
    return bool(
        report.get("schema") == source_refresh.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("request_state")
        in {"producer_refresh_staged", "current_sources_verified"}
        and report.get("request_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and dict(report.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
        and report.get("action_required") is False
        and report.get("interrupt_operator") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
    )


def _source_handoff_is_admissible(report: Mapping[str, Any]) -> bool:
    return bool(
        report.get("schema") == source_handoff.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("handoff_state")
        in {"producer_pickup_available", "current_sources_verified"}
        and report.get("handoff_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and dict(report.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
        and dict(report.get("progress") or {}).get(
            "request_binding_verified"
        )
        is True
        and report.get("action_required") is False
        and report.get("interrupt_operator") is False
        and report.get("handoff_confers_authority") is False
        and report.get("producer_claim_recorded") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
    )


def _source_claims_is_admissible(report: Mapping[str, Any]) -> bool:
    claim_directory = report.get("claim_directory")
    directory_admissible = bool(
        isinstance(claim_directory, Mapping)
        and (
            claim_directory.get("required") is False
            or claim_directory.get("present") is True
        )
    )
    return bool(
        report.get("schema") == source_claims.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("claim_state")
        in {
            "unclaimed",
            "partially_claimed",
            "claimed",
            "not_required",
            "producer_trust_unconfigured",
            "producer_trust_incomplete",
        }
        and report.get("settlement_state")
        in {
            "awaiting_current_evidence",
            "awaiting_producer_trust",
            "current_evidence_verified",
        }
        and report.get("lifecycle_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and dict(report.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
        and dict(report.get("progress") or {}).get(
            "handoff_binding_verified"
        )
        is True
        and dict(report.get("progress") or {}).get(
            "lifecycle_integrity_verified"
        )
        is True
        and directory_admissible
        and report.get("action_required") is False
        and report.get("interrupt_operator") is False
        and report.get("claim_confers_authority") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
    )


def _source_settlement_is_admissible(report: Mapping[str, Any]) -> bool:
    completion_directory = report.get("completion_directory")
    directory_admissible = bool(
        isinstance(completion_directory, Mapping)
        and (
            completion_directory.get("required") is False
            or completion_directory.get("present") is True
        )
    )
    progress = dict(report.get("progress") or {})
    return bool(
        report.get("schema") == source_settlement.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("settlement_state")
        in {
            "no_prior_work",
            "settled_attributed",
            "completion_verified_awaiting_current_evidence",
            "claimed_awaiting_completion",
            "current_evidence_verified_unattributed",
            "unclaimed",
            "producer_trust_unconfigured",
            "producer_trust_incomplete",
        }
        and report.get("settlement_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and progress.get("current_evidence_verified") is True
        and progress.get("settlement_integrity_verified") is True
        and progress.get("current_source_binding_verified") is True
        and progress.get("completion_directory_binding_verified") is True
        and directory_admissible
        and report.get("action_required") is False
        and report.get("interrupt_operator") is False
        and report.get("completion_confers_authority") is False
        and report.get("claim_confers_authority") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
    )


def _source_trust_intake_is_admissible(report: Mapping[str, Any]) -> bool:
    progress = dict(report.get("progress") or {})
    requirements = dict(report.get("candidate_requirements") or {})
    staged = report.get("request_staged") is True
    candidates = list(report.get("candidates") or [])
    review_required = report.get("intake_state") == (
        "candidate_ready_for_operator_review"
    )
    return bool(
        report.get("schema") == trust_intake.VERIFY_SCHEMA
        and report.get("status") == "verified"
        and report.get("request_status")
        == ("staged" if staged else "not_required")
        and report.get("intake_state")
        == (
            "candidate_ready_for_operator_review"
            if review_required
            else "awaiting_producer_public_key_evidence"
            if staged
            else "not_required"
        )
        and bool(list(report.get("requested_lanes") or [])) is staged
        and bool(candidates) is review_required
        and report.get("intake_receipt_persisted") is True
        and report.get("verification_receipt_persisted") is True
        and progress.get("current_evidence_verified") is True
        and progress.get("claim_binding_verified") is True
        and progress.get("trust_registry_binding_verified") is True
        and progress.get("intake_integrity_verified") is True
        and requirements.get("algorithm") == "Ed25519"
        and requirements.get("private_key_material_allowed") is False
        and requirements.get("out_of_band_identity_verification_required")
        is True
        and progress.get("candidate_evidence_count") == len(candidates)
        and (report.get("action_required") is True) is review_required
        and (report.get("interrupt_operator") is True) is review_required
        and (report.get("operator_review_required") is True)
        is review_required
        and (report.get("public_key_candidate_recorded") is True)
        is review_required
        and report.get("private_key_material_requested") is False
        and report.get("private_key_material_recorded") is False
        and report.get("trust_enrollment_authorized") is False
        and report.get("trust_registry_modified") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
        and all(
            isinstance(candidate, Mapping)
            and candidate.get("proof_of_possession_verified") is True
            and candidate.get("out_of_band_identity_verification_required")
            is True
            and candidate.get("candidate_confers_authority") is False
            and candidate.get("trust_enrollment_authorized") is False
            for candidate in candidates
        )
    )


def _source_trust_decision_is_admissible(
    report: Mapping[str, Any],
    *,
    intake: Mapping[str, Any],
) -> bool:
    status = str(report.get("status") or "")
    progress = dict(report.get("progress") or {})
    candidates_present = bool(list(intake.get("candidates") or []))
    pending = status == "pending"
    verified = status == "verified"
    not_required = status == "not_required"
    confirm = verified and report.get("decision") == (
        "confirm_identity_verified"
    )
    current_binding = bool(
        not candidates_present
        and not_required
        or candidates_present
        and status in {"pending", "verified"}
        and report.get("candidate_review_id")
        == intake.get("candidate_review_id")
        and report.get("trust_intake_verification_sha256")
        == intake.get("verification_receipt_sha256")
    )
    return bool(
        report.get("schema") == trust_decision.VERIFY_SCHEMA
        and current_binding
        and progress.get("current_evidence_verified") is True
        and progress.get("candidate_review_verified") is True
        and (progress.get("candidate_decision_recorded") is True)
        is verified
        and (report.get("action_required") is True) is pending
        and report.get("interrupt_operator") is False
        and (report.get("operator_review_required") is True) is pending
        and (report.get("identity_verification_asserted") is True)
        is confirm
        and (report.get("trust_enrollment_preview_authorized") is True)
        is confirm
        and report.get("trust_enrollment_authorized") is False
        and report.get("trust_registry_modified") is False
        and report.get("decision_confers_trust") is False
        and report.get("private_key_material_requested") is False
        and report.get("private_key_material_recorded") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
    )


def _materialize_source_trust_notification_projection(
    *,
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    presentation_state_path: Path,
    receipt_path: Path,
    now: datetime,
) -> dict[str, Any]:
    """Persist and verify one evaluate-only trust-review novelty decision."""

    target = _absolute_path(receipt_path)
    presentation_target = _absolute_path(presentation_state_path)
    report = trust_notification.run_candidate_notification(
        intake,
        decision,
        presentation_state_path=presentation_target,
        receipt_path=target,
        send=False,
        now=now,
    )
    if not (
        report.get("schema") == trust_notification.SCHEMA
        and report.get("status")
        in {"not_required", "resolved", "action_required", "deduplicated"}
        and report.get("execution_mode") == "evaluate_only"
        and report.get("receipt_persisted") is True
        and report.get("delivery_authorized") is False
        and report.get("delivery_attempted") is False
        and report.get("sent") is False
        and report.get("presentation_recorded") is False
        and report.get("trust_enrollment_authorized") is False
        and report.get("trust_registry_modified") is False
        and report.get("decision_confers_trust") is False
        and report.get("private_key_material_requested") is False
        and report.get("private_key_material_recorded") is False
        and report.get("producer_dispatch_authorized") is False
        and report.get("producer_refresh_authorized") is False
        and report.get("automatic_source_refresh_allowed") is False
        and report.get("automatic_execution_allowed") is False
        and report.get("execution_authorized") is False
        and report.get("deployment_or_restart_authorized") is False
        and report.get("protected_operation_executed") is False
        and report.get("provider_quota_consumption_allowed") is False
        and report.get("secret_values_recorded") is False
        and report.get("would_send")
        is (report.get("interrupt_operator") is True)
        and isinstance(report.get("next_action"), str)
        and str(report.get("next_action") or "").strip()
        and trust_notification._SHA256.fullmatch(
            str(report.get("notification_receipt_sha256") or "")
        )
    ):
        raise ValueError("source_trust_notification_not_admissible")
    persisted, raw, digest = trust_intake._private_snapshot(
        target,
        field="safe_tick_source_refresh_trust_candidate_notification",
    )
    verification = trust_notification.verify_notification_receipt(
        persisted,
        intake=intake,
        decision=decision,
        presentation_state_path=presentation_target,
        now=now,
        max_age_seconds=trust_notification.DEFAULT_MAX_AGE_SECONDS,
    )
    if not (
        verification.get("schema") == trust_notification.VERIFY_SCHEMA
        and verification.get("status") == "verified"
        and verification.get("notification_status") == report.get("status")
        and verification.get("action_required")
        is (report.get("action_required") is True)
        and verification.get("interrupt_operator")
        is (report.get("interrupt_operator") is True)
        and verification.get("delivery_authorized") is False
        and verification.get("delivery_attempted") is False
        and verification.get("sent") is False
        and verification.get("trust_enrollment_authorized") is False
        and verification.get("trust_registry_modified") is False
        and verification.get("decision_confers_trust") is False
        and verification.get("private_key_material_requested") is False
        and verification.get("private_key_material_recorded") is False
        and verification.get("producer_dispatch_authorized") is False
        and verification.get("producer_refresh_authorized") is False
        and verification.get("automatic_source_refresh_allowed") is False
        and verification.get("automatic_execution_allowed") is False
        and verification.get("execution_authorized") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("secret_values_recorded") is False
        and dict(verification.get("progress") or {}).get(
            "current_evidence_verified"
        )
        is True
        and digest == report.get("notification_receipt_sha256")
        and len(raw) == report.get("notification_receipt_bytes")
    ):
        raise ValueError("source_trust_notification_not_verified")
    return {
        "schema": trust_notification.VERIFY_SCHEMA,
        "status": "verified",
        "notification_status": str(report.get("status") or ""),
        "updated_at": str(verification.get("updated_at") or ""),
        "blocking_reason": str(report.get("blocking_reason") or ""),
        "next_action": str(report.get("next_action") or ""),
        "candidate_review_id": str(
            report.get("candidate_review_id") or ""
        ),
        "presentation_status": str(
            report.get("presentation_status") or ""
        ),
        "action_required": verification.get("action_required") is True,
        "interrupt_operator": (
            verification.get("interrupt_operator") is True
        ),
        "operator_review_required": (
            report.get("operator_review_required") is True
        ),
        "receipt_path": str(target),
        "notification_receipt_sha256": digest,
        "notification_receipt_bytes": len(raw),
        "current_evidence_verified": True,
        "presentation_recorded": False,
        "trust_enrollment_preview_authorized": (
            verification.get("trust_enrollment_preview_authorized") is True
        ),
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }


def _runtime_review_action(
    action_source: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve one current runtime-review action without creating a new alert."""

    action_key = "pending_actions" if "pending_actions" in action_source else "actions"
    pending_actions = list(action_source.get(action_key) or [])
    runtime_actions = [
        dict(action)
        for action in pending_actions
        if isinstance(action, Mapping)
        and action.get("lane") == "gold_live_runtime"
        and action.get("reason") in runtime_review._REVIEW_ACTION_REASONS
    ]
    if not runtime_actions:
        return None
    if len(pending_actions) != 1 or len(runtime_actions) != 1:
        raise ValueError("runtime_review_action_cardinality_not_admissible")
    return runtime_actions[0]


def _runtime_review_not_required(*, now: datetime) -> dict[str, Any]:
    return {
        "schema": RUNTIME_REVIEW_COMPONENT_SCHEMA,
        "status": "not_required",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": "await a current verified runtime action",
        "current_evidence_verified": True,
        "review_packet_staged": False,
        "action_required": False,
        "interrupt_operator": False,
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _materialize_runtime_review_projection(
    *,
    action_source: Mapping[str, Any],
    packet_path: Path,
    verification_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    public_origin_observation_path: Path,
    release_manifest_path: Path,
    deployment_env_path: Path,
    project: str,
    now: datetime,
) -> dict[str, Any]:
    """Refresh one exact review packet while retaining every authority gate."""

    action = _runtime_review_action(action_source)
    if action is None:
        return _runtime_review_not_required(now=now)
    runtime_review.materialize_local_runtime_binding_candidate()
    packet_target = _absolute_path(packet_path)
    verification_target = _absolute_path(verification_path)
    packet = runtime_review.materialize_current_review_packet(
        cycle_receipt_path=_absolute_path(cycle_receipt_path),
        signal_dir=_absolute_path(signal_dir),
        live_mobile_receipt_path=_absolute_path(
            public_origin_observation_path
        ),
        release_manifest_path=_absolute_path(release_manifest_path),
        deployment_env_path=_absolute_path(deployment_env_path),
        write_path=packet_target,
        project=project,
        now=now,
    )
    packet_evidence = _persisted_component(
        packet_target,
        expected=packet,
        label="safe_tick_runtime_review_packet",
    )
    verification = runtime_review.verify_current_review_packet(
        packet_path=packet_target,
        cycle_receipt_path=_absolute_path(cycle_receipt_path),
        signal_dir=_absolute_path(signal_dir),
        live_mobile_receipt_path=_absolute_path(
            public_origin_observation_path
        ),
        release_manifest_path=_absolute_path(release_manifest_path),
        deployment_env_path=_absolute_path(deployment_env_path),
        project=project,
        now=now,
    )
    proposal = verification.get("configuration_proposal")
    progress = verification.get("progress")
    if not (
        verification.get("schema") == runtime_review.VERIFY_SCHEMA
        and verification.get("status") == "verified"
        and verification.get("blocking_reason") == action.get("reason")
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and isinstance(proposal, Mapping)
        and proposal.get("authorization_required") is True
        and proposal.get("execution_authorized") is False
        and verification.get("packet_sha256") == packet_evidence.get("sha256")
        and verification.get("execution_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        raise ValueError("runtime_review_projection_not_admissible")
    presentation_context = runtime_review.operator_presentation_context(
        verification
    )
    persisted_verification = {
        **dict(verification),
        "verification_receipt_persisted": True,
    }
    atomic_write_bytes(
        verification_target,
        approved.canonical_json_bytes(persisted_verification),
        overwrite=True,
    )
    verification_evidence = _persisted_component(
        verification_target,
        expected=persisted_verification,
        label="safe_tick_runtime_review_verification",
    )
    return {
        "schema": RUNTIME_REVIEW_COMPONENT_SCHEMA,
        "status": "verified",
        "updated_at": str(verification.get("updated_at") or ""),
        "blocking_reason": str(verification.get("blocking_reason") or ""),
        "next_action": str(verification.get("next_action") or ""),
        "current_evidence_verified": True,
        "review_packet_staged": True,
        "packet_generated_at": str(packet.get("generated_at") or ""),
        "packet_path": str(packet_target),
        "packet_sha256": str(packet_evidence.get("sha256") or ""),
        "packet_bytes": int(packet_evidence.get("bytes") or 0),
        "verification_path": str(verification_target),
        "verification_sha256": str(
            verification_evidence.get("sha256") or ""
        ),
        "configuration_proposal": dict(proposal),
        "presentation_context": presentation_context,
        "action_required": False,
        "interrupt_operator": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _selected_next_action(
    *,
    cycle_report: Mapping[str, Any],
    operator_projection: Mapping[str, Any],
    runtime_review_report: Mapping[str, Any],
    runtime_control_report: Mapping[str, Any],
    source_posture: Mapping[str, Any],
    source_trust_intake_report: Mapping[str, Any],
    source_trust_notification_report: Mapping[str, Any],
    source_claims_report: Mapping[str, Any],
) -> str:
    """Keep a current operator action ahead of background source maintenance."""

    cycle_next_action = str(cycle_report.get("next_action") or "").strip()
    if runtime_control_report.get("interrupt_operator") is True:
        return str(runtime_control_report.get("next_action") or "").strip()
    if operator_projection.get("interrupt_operator") is True:
        delegation = dict(
            operator_projection.get("runtime_control_delegation") or {}
        )
        if delegation.get("active") is True:
            return str(operator_projection.get("next_action") or "").strip()
        if runtime_review_report.get("status") == "verified":
            return str(runtime_review_report.get("next_action") or "").strip()
        return str(operator_projection.get("next_action") or "").strip()
    if source_trust_notification_report.get("interrupt_operator") is True:
        return str(
            source_trust_notification_report.get("next_action") or ""
        ).strip()
    if runtime_control_report.get("action_required") is True:
        return str(runtime_control_report.get("next_action") or "").strip()
    if operator_projection.get("action_required") is True:
        return str(operator_projection.get("next_action") or "").strip()
    if source_trust_notification_report.get("action_required") is True:
        return str(
            source_trust_notification_report.get("next_action") or ""
        ).strip()
    if source_posture.get("status") == "waiting_for_fresh_sources":
        return str(
            (
                source_trust_intake_report.get("next_action")
                if source_trust_intake_report.get("request_staged") is True
                else ""
            )
            or source_claims_report.get("next_action")
            or ""
        ).strip()
    return cycle_next_action


def _delegate_runtime_operator_novelty(
    operator_projection: Mapping[str, Any],
    *,
    runtime_review_report: Mapping[str, Any],
    runtime_control_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Let the exact runtime-control lane own its presentation novelty.

    The generic operator projection is still retained as source evidence. A
    verified runtime-control projection has richer, authorization-bound
    context for the same lane, so the generic layer must not independently
    re-interrupt when that context is refreshed. Novel actions from every
    other lane remain untouched.
    """

    projected = dict(operator_projection)
    context = runtime_review_report.get("presentation_context")
    if not (
        runtime_review_report.get("status") == "verified"
        and isinstance(context, Mapping)
        and context.get("schema")
        == "propertyquarry.ooda_operator_presentation_context.v1"
        and str(context.get("lane") or "").strip()
        and runtime_control_report.get("status") == "verified"
        and runtime_control_report.get("current_evidence_verified") is True
        and runtime_control_report.get("action_required") is True
    ):
        return projected

    raw_pending = list(operator_projection.get("pending_actions") or [])
    raw_novel = list(operator_projection.get("actions") or [])
    presentation = dict(operator_projection.get("presentation") or {})
    if not (
        all(isinstance(action, Mapping) for action in raw_pending)
        and all(isinstance(action, Mapping) for action in raw_novel)
        and presentation.get("pending_action_count") == len(raw_pending)
        and presentation.get("novel_action_count") == len(raw_novel)
    ):
        return projected

    governed_lane = str(context.get("lane") or "")
    governed_pending = [
        dict(action)
        for action in raw_pending
        if str(action.get("lane") or "") == governed_lane
    ]
    if not governed_pending:
        return projected
    governed_novel = [
        dict(action)
        for action in raw_novel
        if str(action.get("lane") or "") == governed_lane
    ]
    effective_novel = [
        dict(action)
        for action in raw_novel
        if str(action.get("lane") or "") != governed_lane
    ]

    projected["actions"] = effective_novel
    projected["interrupt_operator"] = bool(effective_novel)
    if projected.get("action_required") is True:
        projected["status"] = (
            "action_required" if effective_novel else "pending_action"
        )
    if effective_novel:
        projected["blocking_reason"] = ",".join(
            str(action.get("reason") or "") for action in effective_novel
        )
        projected["next_action"] = (
            str(effective_novel[0].get("safe_next_action") or "")
            if len(effective_novel) == 1
            else (
                "review each listed action; keep delivery and protected "
                "operations separately authorized"
            )
        )
    else:
        projected["blocking_reason"] = str(
            runtime_control_report.get("blocking_reason") or ""
        )
        projected["next_action"] = str(
            runtime_control_report.get("next_action") or ""
        )

    presentation["novel_action_count"] = len(effective_novel)
    projected["presentation"] = presentation
    projected["runtime_control_delegation"] = {
        "active": True,
        "lane": governed_lane,
        "pending_action_count": len(governed_pending),
        "source_novel_action_count": len(governed_novel),
        "effective_novel_action_count": 0,
        "runtime_control_interrupt_operator": (
            runtime_control_report.get("interrupt_operator") is True
        ),
        "current_evidence_verified": True,
    }
    return projected


def _persist_tick_receipt(
    result: dict[str, Any],
    *,
    receipt_path: Path,
) -> dict[str, Any]:
    persisted = dict(result)
    persisted["receipt_persisted"] = True
    try:
        atomic_write_bytes(
            _absolute_path(receipt_path),
            approved.canonical_json_bytes(persisted),
            overwrite=True,
        )
    except Exception:
        persisted.update(
            {
                "status": "blocked",
                "blocking_reason": "safe_tick_receipt_persistence_failed",
                "next_action": (
                    "repair the private safe-tick receipt lane before trusting or "
                    "presenting this evaluation"
                ),
                "action_required": False,
                "interrupt_operator": False,
                "receipt_persisted": False,
            }
        )
        progress = dict(persisted.get("progress") or {})
        progress["current_evidence_verified"] = False
        persisted["progress"] = progress
    return persisted


def run_safe_tick(
    *,
    gold_receipt_path: Path = Path(gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0]),
    public_origin_observation_path: Path = DEFAULT_PUBLIC_ORIGIN_OBSERVATION,
    refresh_public_origin_observation: bool = True,
    public_origin_url: str = public_origin.DEFAULT_ORIGIN,
    scene_packet_path: Path = Path(scene_notify._CANONICAL_PACKET_PATHS[0]),
    scene_verifier_path: Path = Path(scene_notify._CANONICAL_VERIFIER_PATHS[0]),
    scene_runtime_status_path: Path = Path(
        scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0]
    ),
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    stage_receipt_path: Path = DEFAULT_STAGE_RECEIPT,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT,
    tick_receipt_path: Path = DEFAULT_TICK_RECEIPT,
    tick_lock_path: Path = DEFAULT_TICK_LOCK,
    operator_presentation_state_path: Path = (
        DEFAULT_OPERATOR_PRESENTATION_STATE
    ),
    runtime_review_packet_path: Path = DEFAULT_RUNTIME_REVIEW_PACKET,
    runtime_review_verification_path: Path = (
        DEFAULT_RUNTIME_REVIEW_VERIFICATION
    ),
    runtime_review_release_manifest_path: Path = (
        DEFAULT_RUNTIME_REVIEW_RELEASE_MANIFEST
    ),
    runtime_review_deployment_env_path: Path = (
        DEFAULT_RUNTIME_REVIEW_DEPLOYMENT_ENV
    ),
    runtime_review_project: str = (
        runtime_review.local_deployment.DEFAULT_COMPOSE_PROJECT
    ),
    runtime_control_artifact_dir: Path | None = None,
    source_refresh_request_path: Path = DEFAULT_SOURCE_REFRESH_REQUEST,
    source_refresh_verification_path: Path = DEFAULT_SOURCE_REFRESH_VERIFICATION,
    source_refresh_handoff_path: Path = DEFAULT_SOURCE_REFRESH_HANDOFF,
    source_refresh_handoff_verification_path: Path = (
        DEFAULT_SOURCE_REFRESH_HANDOFF_VERIFICATION
    ),
    source_refresh_claim_trust_registry_path: Path = (
        DEFAULT_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY
    ),
    source_refresh_claim_dir: Path = DEFAULT_SOURCE_REFRESH_CLAIM_DIR,
    source_refresh_claim_receipt_path: Path = DEFAULT_SOURCE_REFRESH_CLAIMS,
    source_refresh_claim_verification_path: Path = (
        DEFAULT_SOURCE_REFRESH_CLAIMS_VERIFICATION
    ),
    source_refresh_trust_intake_candidate_dir: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR
    ),
    source_refresh_trust_intake_receipt_path: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_INTAKE
    ),
    source_refresh_trust_intake_verification_path: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION
    ),
    source_refresh_trust_decision_dir: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_DECISION_DIR
    ),
    source_refresh_trust_presentation_state_path: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_PRESENTATION_STATE
    ),
    source_refresh_trust_notification_path: Path = (
        DEFAULT_SOURCE_REFRESH_TRUST_NOTIFICATION
    ),
    source_refresh_completion_dir: Path = DEFAULT_SOURCE_REFRESH_COMPLETION_DIR,
    source_refresh_settlement_receipt_path: Path = (
        DEFAULT_SOURCE_REFRESH_SETTLEMENT
    ),
    source_refresh_settlement_verification_path: Path = (
        DEFAULT_SOURCE_REFRESH_SETTLEMENT_VERIFICATION
    ),
    require_source_refresh_claim_dir: bool = False,
    require_source_refresh_completion_dir: bool = False,
    state_path: Path = Path(cycle.DEFAULT_STATE_PATH),
    send_lock_path: Path = Path(cycle.DEFAULT_LOCK_PATH),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restage and evaluate once, with no delivery or protected-operation authority."""

    observed_now = _now(now)
    try:
        lock_descriptor = _acquire_tick_lock(tick_lock_path)
    except Exception as exc:
        return _persist_tick_receipt(
            _blocked(
                "safe_tick_lock_unavailable",
                now=observed_now,
                error_type=type(exc).__name__,
            ),
            receipt_path=tick_receipt_path,
        )
    if lock_descriptor is None:
        return _blocked("safe_tick_already_running", now=observed_now)

    stage_completed = False
    try:
        if refresh_public_origin_observation:
            try:
                public_origin.write_public_origin_observation(
                    receipt_path=_absolute_path(
                        public_origin_observation_path
                    ),
                    origin=public_origin_url,
                    now=observed_now,
                )
            except Exception as exc:
                result = _blocked(
                    "safe_tick_public_origin_observation_failed",
                    now=observed_now,
                    error_type=type(exc).__name__,
                )
                return _persist_tick_receipt(
                    result,
                    receipt_path=tick_receipt_path,
                )
        try:
            stage_report = stage.run_stage_iteration(
                gold_receipt_path=_absolute_path(gold_receipt_path),
                public_origin_observation_path=_absolute_path(
                    public_origin_observation_path
                ),
                scene_packet_path=_absolute_path(scene_packet_path),
                scene_verifier_path=_absolute_path(scene_verifier_path),
                scene_runtime_status_path=_absolute_path(
                    scene_runtime_status_path
                ),
                target_dir=_absolute_path(signal_dir),
                receipt_path=_absolute_path(stage_receipt_path),
                max_age_seconds=approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
                now=observed_now,
            )
            if not _stage_is_admissible(stage_report):
                raise ValueError("signal_stage_result_not_admissible")
            stage_evidence = _persisted_component(
                stage_receipt_path,
                expected=stage_report,
                label="safe_tick_stage",
            )
            stage_completed = True
        except Exception as exc:
            result = _blocked(
                "safe_tick_signal_stage_failed",
                now=observed_now,
                error_type=type(exc).__name__,
            )
        else:
            signal_root = _absolute_path(signal_dir)
            try:
                cycle_report = cycle.run_cycle_once(
                    gold_receipt=str(
                        signal_root / approved.SIGNAL_FILENAMES["gold_receipt"]
                    ),
                    public_origin_observation=str(
                        signal_root
                        / approved.SIGNAL_FILENAMES[
                            "public_origin_observation"
                        ]
                    ),
                    scene_packet=str(
                        signal_root / approved.SIGNAL_FILENAMES["scene_packet"]
                    ),
                    scene_verifier=str(
                        signal_root / approved.SIGNAL_FILENAMES["scene_verifier"]
                    ),
                    scene_runtime_status=str(
                        signal_root
                        / approved.SIGNAL_FILENAMES["scene_runtime_status"]
                    ),
                    approval_manifest=str(signal_root / "manifest.json"),
                    require_approval_manifest=True,
                    approval_manifest_max_age_seconds=(
                        approved.DEFAULT_MAX_MANIFEST_AGE_SECONDS
                    ),
                    state_file=str(_absolute_path(state_path)),
                    lock_file=str(_absolute_path(send_lock_path)),
                    write=str(_absolute_path(cycle_receipt_path)),
                    send=False,
                    now=observed_now,
                )
                if cycle_report.get("status") == "notification_state_invalid":
                    if not _notification_state_incident_is_admissible(
                        cycle_report
                    ):
                        raise ValueError(
                            "notification_state_incident_not_admissible"
                        )
                    cycle_evidence = _persisted_component(
                        cycle_receipt_path,
                        expected=cycle_report,
                        label="safe_tick_cycle",
                    )
                    result = _blocked(
                        "safe_tick_notification_state_not_admissible",
                        now=observed_now,
                        stage_completed=stage_completed,
                        cycle_completed=True,
                    )
                    result["next_action"] = str(
                        cycle_report.get("next_action") or ""
                    )
                    result["notification_state"] = {
                        "status": "invalid",
                        "sha256": str(
                            cycle_report.get("notification_state_sha256") or ""
                        ),
                        "admissible": False,
                        "state_updated": False,
                    }
                    result["components"] = {
                        "signal_stage": stage_evidence,
                        "evaluate_only_cycle": cycle_evidence,
                    }
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                if not _cycle_is_admissible(cycle_report):
                    raise ValueError("evaluate_only_cycle_result_not_admissible")
                cycle_evidence = _persisted_component(
                    cycle_receipt_path,
                    expected=cycle_report,
                    label="safe_tick_cycle",
                )
            except Exception as exc:
                result = _blocked(
                    "safe_tick_evaluate_only_cycle_failed",
                    now=observed_now,
                    stage_completed=stage_completed,
                    error_type=type(exc).__name__,
                )
            else:
                try:
                    runtime_review_report = (
                        _materialize_runtime_review_projection(
                            action_source=cycle_report,
                            packet_path=runtime_review_packet_path,
                            verification_path=(
                                runtime_review_verification_path
                            ),
                            cycle_receipt_path=cycle_receipt_path,
                            signal_dir=signal_root,
                            public_origin_observation_path=(
                                public_origin_observation_path
                            ),
                            release_manifest_path=(
                                runtime_review_release_manifest_path
                            ),
                            deployment_env_path=(
                                runtime_review_deployment_env_path
                            ),
                            project=runtime_review_project,
                            now=observed_now,
                        )
                    )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_runtime_review_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        error_type=type(exc).__name__,
                    )
                    result["next_action"] = (
                        "repair the current evaluate-only runtime review "
                        "receipt binding before requesting deployment or "
                        "restart authority"
                    )
                    result["components"] = {
                        "signal_stage": stage_evidence,
                        "evaluate_only_cycle": cycle_evidence,
                    }
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    cycle_operator_projection = _load_operator_projection(
                        cycle_receipt_path=cycle_receipt_path,
                        signal_dir=signal_root,
                        presentation_state_path=(
                            operator_presentation_state_path
                        ),
                        cycle_evidence=cycle_evidence,
                        runtime_review_report=runtime_review_report,
                        now=observed_now,
                    )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_operator_projection_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    result["next_action"] = (
                        "repair or explicitly replace the private "
                        "operator-presentation ledger, then retry the "
                        "evaluate-only tick"
                    )
                    result["components"] = {
                        "signal_stage": stage_evidence,
                        "evaluate_only_cycle": cycle_evidence,
                    }
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                if runtime_review_report.get("status") == "verified":
                    try:
                        runtime_control_report = (
                            runtime_control.stage_current_runtime_control(
                                artifact_dir=(
                                    runtime_control_artifact_dir
                                    or _absolute_path(tick_receipt_path).parent
                                ),
                                packet_path=_absolute_path(
                                    runtime_review_packet_path
                                ),
                                review_verification_path=_absolute_path(
                                    runtime_review_verification_path
                                ),
                                cycle_receipt_path=_absolute_path(
                                    cycle_receipt_path
                                ),
                                signal_dir=signal_root,
                                live_mobile_receipt_path=_absolute_path(
                                    public_origin_observation_path
                                ),
                                release_manifest_path=_absolute_path(
                                    runtime_review_release_manifest_path
                                ),
                                deployment_env_path=_absolute_path(
                                    runtime_review_deployment_env_path
                                ),
                                presentation_state_path=_absolute_path(
                                    operator_presentation_state_path
                                ),
                                project=runtime_review_project,
                                root=ROOT,
                                now=observed_now,
                            )
                        )
                        if not (
                            runtime_control_report.get("status") == "verified"
                            and runtime_control_report.get(
                                "current_evidence_verified"
                            )
                            is True
                            and runtime_control_report.get("receipt_persisted")
                            is True
                            and runtime_control_report.get(
                                "automatic_execution_allowed"
                            )
                            is False
                            and runtime_control_report.get(
                                "execution_authorized"
                            )
                            is False
                            and runtime_control_report.get(
                                "deployment_or_restart_authorized"
                            )
                            is False
                            and runtime_control_report.get(
                                "protected_operation_executed"
                            )
                            is False
                            and runtime_control_report.get(
                                "provider_quota_consumption_allowed"
                            )
                            is False
                            and runtime_control_report.get(
                                "delivery_authorized"
                            )
                            is False
                            and runtime_control_report.get(
                                "delivery_attempted"
                            )
                            is False
                            and runtime_control_report.get("sent") is False
                            and runtime_control_report.get(
                                "presentation_recorded"
                            )
                            is False
                        ):
                            raise ValueError(
                                "runtime_control_result_not_admissible"
                            )
                    except Exception as exc:
                        result = _blocked(
                            "safe_tick_runtime_control_failed",
                            now=observed_now,
                            stage_completed=True,
                            cycle_completed=True,
                            runtime_review_verified=True,
                            error_type=type(exc).__name__,
                        )
                        result["next_action"] = (
                            "repair the current host-side authorization and "
                            "configuration-plan receipt chain; no authority "
                            "was granted or exercised"
                        )
                        result["components"] = {
                            "signal_stage": stage_evidence,
                            "evaluate_only_cycle": cycle_evidence,
                            "runtime_recovery_review": dict(
                                runtime_review_report
                            ),
                        }
                        return _persist_tick_receipt(
                            result,
                            receipt_path=tick_receipt_path,
                        )
                else:
                    runtime_control_report = {
                        "schema": runtime_control.SCHEMA,
                        "status": "not_required",
                        "updated_at": observed_now.isoformat(),
                        "blocking_reason": "",
                        "next_action": "await a current verified runtime action",
                        "action_required": False,
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
                try:
                    source_settlement_report = (
                        source_settlement.materialize_current_settlement_bundle(
                            cycle_receipt_path=_absolute_path(
                                cycle_receipt_path
                            ),
                            signal_dir=signal_root,
                            handoff_path=_absolute_path(
                                source_refresh_handoff_path
                            ),
                            handoff_verification_path=_absolute_path(
                                source_refresh_handoff_verification_path
                            ),
                            claim_lifecycle_path=_absolute_path(
                                source_refresh_claim_receipt_path
                            ),
                            claim_verification_path=_absolute_path(
                                source_refresh_claim_verification_path
                            ),
                            trust_registry_path=_absolute_path(
                                source_refresh_claim_trust_registry_path
                            ),
                            claim_dir=_absolute_path(
                                source_refresh_claim_dir
                            ),
                            completion_dir=_absolute_path(
                                source_refresh_completion_dir
                            ),
                            receipt_path=_absolute_path(
                                source_refresh_settlement_receipt_path
                            ),
                            verification_path=_absolute_path(
                                source_refresh_settlement_verification_path
                            ),
                            require_claim_dir=(
                                require_source_refresh_claim_dir
                            ),
                            require_completion_dir=(
                                require_source_refresh_completion_dir
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_settlement_is_admissible(
                        source_settlement_report
                    ):
                        raise ValueError(
                            "source_refresh_settlement_result_not_admissible"
                        )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_settlement_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    source_refresh_report = (
                        source_refresh.materialize_current_source_refresh_request_bundle(
                            cycle_receipt_path=_absolute_path(
                                cycle_receipt_path
                            ),
                            signal_dir=signal_root,
                            request_path=_absolute_path(
                                source_refresh_request_path
                            ),
                            verification_path=_absolute_path(
                                source_refresh_verification_path
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_refresh_is_admissible(
                        source_refresh_report
                    ):
                        raise ValueError(
                            "source_refresh_request_result_not_admissible"
                        )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_request_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    source_handoff_report = (
                        source_handoff.materialize_current_source_refresh_handoff_bundle(
                            cycle_receipt_path=_absolute_path(
                                cycle_receipt_path
                            ),
                            signal_dir=signal_root,
                            request_path=_absolute_path(
                                source_refresh_request_path
                            ),
                            request_verification_path=_absolute_path(
                                source_refresh_verification_path
                            ),
                            handoff_path=_absolute_path(
                                source_refresh_handoff_path
                            ),
                            verification_path=_absolute_path(
                                source_refresh_handoff_verification_path
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_handoff_is_admissible(
                        source_handoff_report
                    ):
                        raise ValueError(
                            "source_refresh_handoff_result_not_admissible"
                        )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_handoff_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    source_claims_report = (
                        source_claims.materialize_current_claim_lifecycle_bundle(
                            cycle_receipt_path=_absolute_path(
                                cycle_receipt_path
                            ),
                            signal_dir=signal_root,
                            request_path=_absolute_path(
                                source_refresh_request_path
                            ),
                            request_verification_path=_absolute_path(
                                source_refresh_verification_path
                            ),
                            handoff_path=_absolute_path(
                                source_refresh_handoff_path
                            ),
                            handoff_verification_path=_absolute_path(
                                source_refresh_handoff_verification_path
                            ),
                            trust_registry_path=_absolute_path(
                                source_refresh_claim_trust_registry_path
                            ),
                            claim_dir=_absolute_path(
                                source_refresh_claim_dir
                            ),
                            receipt_path=_absolute_path(
                                source_refresh_claim_receipt_path
                            ),
                            verification_path=_absolute_path(
                                source_refresh_claim_verification_path
                            ),
                            require_claim_dir=(
                                require_source_refresh_claim_dir
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_claims_is_admissible(
                        source_claims_report
                    ):
                        raise ValueError(
                            "source_refresh_claims_result_not_admissible"
                        )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_claims_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    source_trust_intake_report = (
                        trust_intake.materialize_trust_intake_bundle(
                            claim_verification_path=_absolute_path(
                                source_refresh_claim_verification_path
                            ),
                            trust_registry_path=_absolute_path(
                                source_refresh_claim_trust_registry_path
                            ),
                            candidate_dir=_absolute_path(
                                source_refresh_trust_intake_candidate_dir
                            ),
                            receipt_path=_absolute_path(
                                source_refresh_trust_intake_receipt_path
                            ),
                            verification_path=_absolute_path(
                                source_refresh_trust_intake_verification_path
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_trust_intake_is_admissible(
                        source_trust_intake_report
                    ):
                        raise ValueError(
                            "source_refresh_trust_intake_result_not_admissible"
                        )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_trust_intake_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    notification_intake_report = source_trust_intake_report
                    source_trust_decision_report = (
                        trust_decision.verify_candidate_review_decision_for_report(
                            source_trust_intake_report,
                            decision_dir=_absolute_path(
                                source_refresh_trust_decision_dir
                            ),
                            now=observed_now,
                        )
                    )
                    if not _source_trust_decision_is_admissible(
                        source_trust_decision_report,
                        intake=source_trust_intake_report,
                    ):
                        raise ValueError(
                            "source_refresh_trust_decision_result_not_admissible"
                        )
                    source_trust_intake_report = (
                        trust_decision.project_candidate_review_decision(
                            source_trust_intake_report,
                            source_trust_decision_report,
                        )
                    )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_trust_decision_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                try:
                    source_trust_notification_report = (
                        _materialize_source_trust_notification_projection(
                            intake=notification_intake_report,
                            decision=source_trust_decision_report,
                            presentation_state_path=_absolute_path(
                                source_refresh_trust_presentation_state_path
                            ),
                            receipt_path=_absolute_path(
                                source_refresh_trust_notification_path
                            ),
                            now=observed_now,
                        )
                    )
                except Exception as exc:
                    result = _blocked(
                        "safe_tick_source_refresh_trust_notification_failed",
                        now=observed_now,
                        stage_completed=True,
                        cycle_completed=True,
                        runtime_review_verified=True,
                        error_type=type(exc).__name__,
                    )
                    return _persist_tick_receipt(
                        result,
                        receipt_path=tick_receipt_path,
                    )
                source_posture = dict(
                    cycle_report.get("source_evidence_posture") or {}
                )
                effective_operator_projection = (
                    _delegate_runtime_operator_novelty(
                        cycle_operator_projection,
                        runtime_review_report=runtime_review_report,
                        runtime_control_report=runtime_control_report,
                    )
                )
                next_action = _selected_next_action(
                    cycle_report=cycle_report,
                    operator_projection=effective_operator_projection,
                    runtime_review_report=runtime_review_report,
                    runtime_control_report=runtime_control_report,
                    source_posture=source_posture,
                    source_trust_intake_report=(
                        source_trust_intake_report
                    ),
                    source_trust_notification_report=(
                        source_trust_notification_report
                    ),
                    source_claims_report=source_claims_report,
                )
                result = {
                    "schema": SCHEMA,
                    "status": "ready",
                    "updated_at": observed_now.isoformat(),
                    "blocking_reason": "",
                    "next_action": next_action,
                    "action_required": (
                        effective_operator_projection.get("action_required") is True
                        or runtime_control_report.get("action_required") is True
                        or source_trust_notification_report.get("action_required")
                        is True
                    ),
                    "interrupt_operator": (
                        effective_operator_projection.get("interrupt_operator") is True
                        or runtime_control_report.get("interrupt_operator") is True
                        or source_trust_notification_report.get(
                            "interrupt_operator"
                        )
                        is True
                    ),
                    "progress": {
                        "signal_stage_completed": True,
                        "evaluate_only_cycle_completed": True,
                        "operator_presentation_verified": True,
                        "runtime_review_verified": True,
                        "runtime_control_verified": (
                            runtime_control_report.get("status")
                            in {"verified", "not_required"}
                            and runtime_control_report.get(
                                "current_evidence_verified"
                            )
                            is True
                        ),
                        "source_refresh_request_verified": True,
                        "source_refresh_handoff_verified": True,
                        "source_refresh_claims_verified": True,
                        "source_refresh_trust_intake_verified": True,
                        "source_refresh_trust_decision_verified": True,
                        "source_refresh_trust_notification_verified": True,
                        "source_refresh_settlement_verified": True,
                        "source_refresh_request_staged": (
                            source_refresh_report.get("request_staged") is True
                        ),
                        "current_evidence_verified": True,
                    },
                    "source_evidence_posture": source_posture,
                    "components": {
                        "signal_stage": stage_evidence,
                        "evaluate_only_cycle": cycle_evidence,
                        "operator_presentation": {
                            "status": str(
                                effective_operator_projection.get("status") or ""
                            ),
                            "action_required": (
                                effective_operator_projection.get("action_required")
                                is True
                            ),
                            "interrupt_operator": (
                                effective_operator_projection.get("interrupt_operator")
                                is True
                            ),
                            "source_interrupt_operator": (
                                cycle_operator_projection.get("interrupt_operator")
                                is True
                            ),
                            "blocking_reason": str(
                                effective_operator_projection.get("blocking_reason")
                                or ""
                            ),
                            "next_action": str(
                                effective_operator_projection.get("next_action")
                                or ""
                            ),
                            "source_cycle_receipt_sha256": str(
                                cycle_operator_projection.get(
                                    "source_cycle_receipt_sha256"
                                )
                                or ""
                            ),
                            "state_path": str(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("state_path")
                                or ""
                            ),
                            "state_status": str(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("state_status")
                                or ""
                            ),
                            "state_sha256": str(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("state_sha256")
                                or ""
                            ),
                            "pending_action_count": int(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("pending_action_count")
                                or 0
                            ),
                            "novel_action_count": int(
                                dict(
                                    effective_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("novel_action_count")
                                or 0
                            ),
                            "source_novel_action_count": int(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("novel_action_count")
                                or 0
                            ),
                            "runtime_control_delegation": dict(
                                effective_operator_projection.get(
                                    "runtime_control_delegation"
                                )
                                or {}
                            ),
                            "context_hydration_state_updated": (
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("context_hydration_state_updated")
                                is True
                            ),
                            "context_hydrated_lanes": list(
                                dict(
                                    cycle_operator_projection.get(
                                        "presentation"
                                    )
                                    or {}
                                ).get("context_hydrated_lanes")
                                or []
                            ),
                            "current_evidence_verified": True,
                            "delivery_state_updated": False,
                            "provider_quota_consumed": False,
                            "protected_operation_executed": False,
                        },
                        "runtime_recovery_review": dict(
                            runtime_review_report
                        ),
                        "runtime_control": dict(runtime_control_report),
                        "source_refresh_settlement": {
                            "status": str(
                                source_settlement_report.get(
                                    "settlement_state"
                                )
                                or ""
                            ),
                            "producer_completion_recorded": (
                                source_settlement_report.get(
                                    "producer_completion_recorded"
                                )
                                is True
                            ),
                            "settlement_attributed": (
                                source_settlement_report.get(
                                    "settlement_attributed"
                                )
                                is True
                            ),
                            "settled_count": int(
                                dict(
                                    source_settlement_report.get("progress")
                                    or {}
                                ).get("settled_count")
                                or 0
                            ),
                            "expected_work_item_count": int(
                                dict(
                                    source_settlement_report.get("progress")
                                    or {}
                                ).get("expected_work_item_count")
                                or 0
                            ),
                            "settlements": list(
                                source_settlement_report.get("settlements")
                                or []
                            ),
                            "receipt_path": str(
                                source_settlement_report.get("receipt_path")
                                or ""
                            ),
                            "settlement_receipt_sha256": str(
                                source_settlement_report.get(
                                    "settlement_receipt_sha256"
                                )
                                or ""
                            ),
                            "verification_path": str(
                                source_settlement_report.get(
                                    "verification_path"
                                )
                                or ""
                            ),
                            "verification_receipt_sha256": str(
                                source_settlement_report.get(
                                    "verification_receipt_sha256"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "completion_confers_authority": False,
                            "producer_dispatch_authorized": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_request": {
                            "status": str(
                                source_refresh_report.get("request_state")
                                or ""
                            ),
                            "request_id": str(
                                source_refresh_report.get("request_id") or ""
                            ),
                            "requested_lane_count": len(
                                list(
                                    source_refresh_report.get(
                                        "requested_lanes"
                                    )
                                    or []
                                )
                            ),
                            "request_path": str(
                                source_refresh_report.get("request_path") or ""
                            ),
                            "request_receipt_sha256": str(
                                source_refresh_report.get(
                                    "request_receipt_sha256"
                                )
                                or ""
                            ),
                            "verification_path": str(
                                source_refresh_report.get("verification_path")
                                or ""
                            ),
                            "verification_receipt_sha256": str(
                                source_refresh_report.get(
                                    "verification_receipt_sha256"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "producer_dispatch_authorized": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_handoff": {
                            "status": str(
                                source_handoff_report.get("handoff_state")
                                or ""
                            ),
                            "handoff_id": str(
                                source_handoff_report.get("handoff_id") or ""
                            ),
                            "work_item_count": len(
                                list(
                                    source_handoff_report.get("work_items")
                                    or []
                                )
                            ),
                            "handoff_path": str(
                                source_handoff_report.get("handoff_path") or ""
                            ),
                            "handoff_receipt_sha256": str(
                                source_handoff_report.get(
                                    "handoff_receipt_sha256"
                                )
                                or ""
                            ),
                            "verification_path": str(
                                source_handoff_report.get("verification_path")
                                or ""
                            ),
                            "verification_receipt_sha256": str(
                                source_handoff_report.get(
                                    "verification_receipt_sha256"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "handoff_confers_authority": False,
                            "producer_claim_recorded": False,
                            "producer_dispatch_authorized": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_claims": {
                            "status": str(
                                source_claims_report.get("claim_state") or ""
                            ),
                            "settlement_state": str(
                                source_claims_report.get("settlement_state")
                                or ""
                            ),
                            "handoff_id": str(
                                source_claims_report.get("handoff_id") or ""
                            ),
                            "claim_count": int(
                                dict(
                                    source_claims_report.get("progress") or {}
                                ).get("claim_count")
                                or 0
                            ),
                            "expected_claim_count": int(
                                dict(
                                    source_claims_report.get("progress") or {}
                                ).get("expected_claim_count")
                                or 0
                            ),
                            "claims": list(
                                source_claims_report.get("claims") or []
                            ),
                            "receipt_path": str(
                                source_claims_report.get("receipt_path") or ""
                            ),
                            "lifecycle_receipt_sha256": str(
                                source_claims_report.get(
                                    "lifecycle_receipt_sha256"
                                )
                                or ""
                            ),
                            "verification_path": str(
                                source_claims_report.get("verification_path")
                                or ""
                            ),
                            "verification_receipt_sha256": str(
                                source_claims_report.get(
                                    "verification_receipt_sha256"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "claim_confers_authority": False,
                            "producer_dispatch_authorized": False,
                            "producer_refresh_authorized": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_trust_intake": {
                            "status": str(
                                source_trust_intake_report.get(
                                    "intake_state"
                                )
                                or ""
                            ),
                            "request_id": str(
                                source_trust_intake_report.get("request_id")
                                or ""
                            ),
                            "request_staged": (
                                source_trust_intake_report.get(
                                    "request_staged"
                                )
                                is True
                            ),
                            "requested_lanes": list(
                                source_trust_intake_report.get(
                                    "requested_lanes"
                                )
                                or []
                            ),
                            "candidate_evidence_count": int(
                                dict(
                                    source_trust_intake_report.get("progress")
                                    or {}
                                ).get("candidate_evidence_count")
                                or 0
                            ),
                            "candidate_review_id": str(
                                source_trust_intake_report.get(
                                    "candidate_review_id"
                                )
                                or ""
                            ),
                            "review_decision_options": list(
                                source_trust_intake_report.get(
                                    "review_decision_options"
                                )
                                or []
                            ),
                            "candidates": list(
                                source_trust_intake_report.get("candidates")
                                or []
                            ),
                            "candidate_coverage": dict(
                                source_trust_intake_report.get(
                                    "candidate_coverage"
                                )
                                or {}
                            ),
                            "receipt_path": str(
                                source_trust_intake_report.get("receipt_path")
                                or ""
                            ),
                            "intake_receipt_sha256": str(
                                source_trust_intake_report.get(
                                    "intake_receipt_sha256"
                                )
                                or ""
                            ),
                            "verification_path": str(
                                source_trust_intake_report.get(
                                    "verification_path"
                                )
                                or ""
                            ),
                            "verification_receipt_sha256": str(
                                source_trust_intake_report.get(
                                    "verification_receipt_sha256"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "action_required": (
                                source_trust_notification_report.get(
                                    "action_required"
                                )
                                is True
                            ),
                            "interrupt_operator": (
                                source_trust_notification_report.get(
                                    "interrupt_operator"
                                )
                                is True
                            ),
                            "operator_review_required": (
                                source_trust_notification_report.get(
                                    "operator_review_required"
                                )
                                is True
                            ),
                            "review_decision": dict(
                                source_trust_intake_report.get(
                                    "review_decision"
                                )
                                or {}
                            ),
                            "trust_enrollment_preview_authorized": (
                                source_trust_intake_report.get(
                                    "trust_enrollment_preview_authorized"
                                )
                                is True
                            ),
                            "public_key_candidate_recorded": (
                                source_trust_intake_report.get(
                                    "public_key_candidate_recorded"
                                )
                                is True
                            ),
                            "private_key_material_requested": False,
                            "trust_enrollment_authorized": False,
                            "trust_registry_modified": False,
                            "producer_dispatch_authorized": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_trust_decision": {
                            "status": str(
                                source_trust_decision_report.get("status")
                                or ""
                            ),
                            "review_state": str(
                                source_trust_decision_report.get(
                                    "review_state"
                                )
                                or ""
                            ),
                            "candidate_review_id": str(
                                source_trust_decision_report.get(
                                    "candidate_review_id"
                                )
                                or ""
                            ),
                            "decision_id": str(
                                source_trust_decision_report.get(
                                    "decision_id"
                                )
                                or ""
                            ),
                            "decision": str(
                                source_trust_decision_report.get("decision")
                                or ""
                            ),
                            "decision_path": str(
                                source_trust_decision_report.get(
                                    "decision_path"
                                )
                                or ""
                            ),
                            "current_evidence_verified": True,
                            "action_required": (
                                source_trust_decision_report.get(
                                    "action_required"
                                )
                                is True
                            ),
                            "interrupt_operator": False,
                            "identity_verification_asserted": (
                                source_trust_decision_report.get(
                                    "identity_verification_asserted"
                                )
                                is True
                            ),
                            "trust_enrollment_preview_authorized": (
                                source_trust_decision_report.get(
                                    "trust_enrollment_preview_authorized"
                                )
                                is True
                            ),
                            "trust_enrollment_authorized": False,
                            "trust_registry_modified": False,
                            "decision_confers_trust": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                            "protected_operation_executed": False,
                        },
                        "source_refresh_trust_notification": dict(
                            source_trust_notification_report
                        ),
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
    finally:
        _release_tick_lock(lock_descriptor)

    return _persist_tick_receipt(result, receipt_path=tick_receipt_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one serialized, manifest-bound PropertyQuarry OODA evaluation "
            "without delivery, provider, deployment, or restart authority."
        )
    )
    parser.add_argument("--gold-receipt", type=Path, default=Path(gold_notify._CANONICAL_GOLD_RECEIPT_PATHS[0]))
    parser.add_argument(
        "--public-origin-observation",
        type=Path,
        default=DEFAULT_PUBLIC_ORIGIN_OBSERVATION,
    )
    public_origin_refresh = parser.add_mutually_exclusive_group()
    public_origin_refresh.add_argument(
        "--refresh-public-origin-observation",
        dest="refresh_public_origin_observation",
        action="store_true",
        default=True,
        help=(
            "Perform one credential-free GET and refresh only the private "
            "public-origin observation receipt before staging (default)."
        ),
    )
    public_origin_refresh.add_argument(
        "--no-refresh-public-origin-observation",
        dest="refresh_public_origin_observation",
        action="store_false",
        help=(
            "Use an already-current private public-origin observation "
            "receipt without making a GET."
        ),
    )
    parser.add_argument(
        "--public-origin-url",
        default=public_origin.DEFAULT_ORIGIN,
    )
    parser.add_argument("--scene-packet", type=Path, default=Path(scene_notify._CANONICAL_PACKET_PATHS[0]))
    parser.add_argument("--scene-verifier", type=Path, default=Path(scene_notify._CANONICAL_VERIFIER_PATHS[0]))
    parser.add_argument("--scene-runtime-status", type=Path, default=Path(scene_notify._CANONICAL_RUNTIME_STATUS_PATHS[0]))
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL_DIR)
    parser.add_argument("--stage-receipt", type=Path, default=DEFAULT_STAGE_RECEIPT)
    parser.add_argument("--cycle-receipt", type=Path, default=DEFAULT_CYCLE_RECEIPT)
    parser.add_argument("--tick-receipt", type=Path, default=DEFAULT_TICK_RECEIPT)
    parser.add_argument("--tick-lock", type=Path, default=DEFAULT_TICK_LOCK)
    parser.add_argument(
        "--operator-presentation-state",
        type=Path,
        default=DEFAULT_OPERATOR_PRESENTATION_STATE,
    )
    parser.add_argument(
        "--runtime-review-packet",
        type=Path,
        default=DEFAULT_RUNTIME_REVIEW_PACKET,
    )
    parser.add_argument(
        "--runtime-review-verification",
        type=Path,
        default=DEFAULT_RUNTIME_REVIEW_VERIFICATION,
    )
    parser.add_argument(
        "--runtime-review-release-manifest",
        type=Path,
        default=DEFAULT_RUNTIME_REVIEW_RELEASE_MANIFEST,
    )
    parser.add_argument(
        "--runtime-review-deployment-env",
        type=Path,
        default=DEFAULT_RUNTIME_REVIEW_DEPLOYMENT_ENV,
    )
    parser.add_argument(
        "--runtime-review-project",
        default=runtime_review.local_deployment.DEFAULT_COMPOSE_PROJECT,
    )
    parser.add_argument(
        "--source-refresh-request",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_REQUEST,
    )
    parser.add_argument(
        "--source-refresh-verification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_VERIFICATION,
    )
    parser.add_argument(
        "--source-refresh-handoff",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_HANDOFF,
    )
    parser.add_argument(
        "--source-refresh-handoff-verification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_HANDOFF_VERIFICATION,
    )
    parser.add_argument(
        "--source-refresh-claim-trust-registry",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY,
    )
    parser.add_argument(
        "--source-refresh-claim-dir",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_CLAIM_DIR,
    )
    parser.add_argument(
        "--source-refresh-claims",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_CLAIMS,
    )
    parser.add_argument(
        "--source-refresh-claims-verification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_CLAIMS_VERIFICATION,
    )
    parser.add_argument(
        "--source-refresh-trust-intake-candidate-dir",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR,
    )
    parser.add_argument(
        "--source-refresh-trust-intake",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_INTAKE,
    )
    parser.add_argument(
        "--source-refresh-trust-intake-verification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION,
    )
    parser.add_argument(
        "--source-refresh-trust-decision-dir",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_DECISION_DIR,
    )
    parser.add_argument(
        "--source-refresh-trust-presentation-state",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_PRESENTATION_STATE,
    )
    parser.add_argument(
        "--source-refresh-trust-notification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_TRUST_NOTIFICATION,
    )
    parser.add_argument(
        "--source-refresh-completion-dir",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_COMPLETION_DIR,
    )
    parser.add_argument(
        "--source-refresh-settlement",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_SETTLEMENT,
    )
    parser.add_argument(
        "--source-refresh-settlement-verification",
        type=Path,
        default=DEFAULT_SOURCE_REFRESH_SETTLEMENT_VERIFICATION,
    )
    parser.add_argument(
        "--require-source-refresh-claim-dir",
        action="store_true",
    )
    parser.add_argument(
        "--require-source-refresh-completion-dir",
        action="store_true",
    )
    parser.add_argument("--state", type=Path, default=Path(cycle.DEFAULT_STATE_PATH))
    parser.add_argument("--send-lock", type=Path, default=Path(cycle.DEFAULT_LOCK_PATH))
    args = parser.parse_args(argv)
    result = run_safe_tick(
        gold_receipt_path=args.gold_receipt,
        public_origin_observation_path=args.public_origin_observation,
        refresh_public_origin_observation=bool(
            args.refresh_public_origin_observation
        ),
        public_origin_url=str(args.public_origin_url or ""),
        scene_packet_path=args.scene_packet,
        scene_verifier_path=args.scene_verifier,
        scene_runtime_status_path=args.scene_runtime_status,
        signal_dir=args.signal_dir,
        stage_receipt_path=args.stage_receipt,
        cycle_receipt_path=args.cycle_receipt,
        tick_receipt_path=args.tick_receipt,
        tick_lock_path=args.tick_lock,
        operator_presentation_state_path=(
            args.operator_presentation_state
        ),
        runtime_review_packet_path=args.runtime_review_packet,
        runtime_review_verification_path=(
            args.runtime_review_verification
        ),
        runtime_review_release_manifest_path=(
            args.runtime_review_release_manifest
        ),
        runtime_review_deployment_env_path=(
            args.runtime_review_deployment_env
        ),
        runtime_review_project=str(args.runtime_review_project or ""),
        source_refresh_request_path=args.source_refresh_request,
        source_refresh_verification_path=args.source_refresh_verification,
        source_refresh_handoff_path=args.source_refresh_handoff,
        source_refresh_handoff_verification_path=(
            args.source_refresh_handoff_verification
        ),
        source_refresh_claim_trust_registry_path=(
            args.source_refresh_claim_trust_registry
        ),
        source_refresh_claim_dir=args.source_refresh_claim_dir,
        source_refresh_claim_receipt_path=args.source_refresh_claims,
        source_refresh_claim_verification_path=(
            args.source_refresh_claims_verification
        ),
        source_refresh_trust_intake_candidate_dir=(
            args.source_refresh_trust_intake_candidate_dir
        ),
        source_refresh_trust_intake_receipt_path=(
            args.source_refresh_trust_intake
        ),
        source_refresh_trust_intake_verification_path=(
            args.source_refresh_trust_intake_verification
        ),
        source_refresh_trust_decision_dir=(
            args.source_refresh_trust_decision_dir
        ),
        source_refresh_trust_presentation_state_path=(
            args.source_refresh_trust_presentation_state
        ),
        source_refresh_trust_notification_path=(
            args.source_refresh_trust_notification
        ),
        source_refresh_completion_dir=args.source_refresh_completion_dir,
        source_refresh_settlement_receipt_path=(
            args.source_refresh_settlement
        ),
        source_refresh_settlement_verification_path=(
            args.source_refresh_settlement_verification
        ),
        require_source_refresh_claim_dir=(
            args.require_source_refresh_claim_dir
        ),
        require_source_refresh_completion_dir=(
            args.require_source_refresh_completion_dir
        ),
        state_path=args.state,
        send_lock_path=args.send_lock,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
