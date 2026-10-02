from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app import scheduler_healthcheck as healthcheck


def _configure_process_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    role: str = "scheduler",
) -> Path:
    path = tmp_path / f"{role}-heartbeat.json"
    path.write_text(
        json.dumps(
            {
                "epoch": time.time(),
                "role": role,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_ROLE", role)
    prefix = "SCHEDULER" if role == "scheduler" else "WORKER"
    monkeypatch.setenv(f"EA_{prefix}_HEARTBEAT_PATH", str(path))
    monkeypatch.setenv(f"EA_{prefix}_HEARTBEAT_MAX_AGE_SECONDS", "60")
    return path


def _verified_witness() -> dict[str, object]:
    return {
        "status": "verified",
        "iteration_status": "completed",
        "blocking_reason": "",
        "persistent_reevaluation_verified": True,
        "progress": {
            "current_evidence_verified": True,
            "cycle_binding_verified": True,
        },
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
    }


def _verified_source_refresh() -> dict[str, object]:
    return {
        "status": "verified",
        "request_state": "producer_refresh_staged",
        "blocking_reason": "",
        "request_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "request_integrity_verified": True,
            "source_binding_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_source_handoff() -> dict[str, object]:
    return {
        "status": "verified",
        "handoff_state": "producer_pickup_available",
        "blocking_reason": "",
        "handoff_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "handoff_integrity_verified": True,
            "request_binding_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "handoff_confers_authority": False,
        "producer_claim_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_source_claims() -> dict[str, object]:
    return {
        "status": "verified",
        "claim_state": "unclaimed",
        "settlement_state": "awaiting_current_evidence",
        "blocking_reason": "",
        "producer_claim_recorded": False,
        "lifecycle_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "claim_directory": {
            "required": True,
            "present": True,
        },
        "progress": {
            "current_evidence_verified": True,
            "lifecycle_integrity_verified": True,
            "handoff_binding_verified": True,
            "claim_count": 0,
        },
        "action_required": False,
        "interrupt_operator": False,
        "claim_confers_authority": False,
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
    }


def _verified_source_settlement() -> dict[str, object]:
    return {
        "status": "verified",
        "settlement_state": "unclaimed",
        "blocking_reason": "",
        "producer_completion_recorded": False,
        "settlement_attributed": False,
        "settlement_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "completion_directory": {"required": True, "present": True},
        "progress": {
            "current_evidence_verified": True,
            "settlement_integrity_verified": True,
            "current_source_binding_verified": True,
            "completion_directory_binding_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "completion_confers_authority": False,
        "claim_confers_authority": False,
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
    }


def _verified_source_trust_intake() -> dict[str, object]:
    return {
        "status": "verified",
        "intake_state": "awaiting_producer_public_key_evidence",
        "blocking_reason": "",
        "request_staged": True,
        "candidates": [],
        "candidate_directory": {
            "present": True,
            "current_candidate_file_count": 0,
        },
        "intake_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "claim_binding_verified": True,
            "trust_registry_binding_verified": True,
            "intake_integrity_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _verified_source_trust_candidate_import(
    *,
    import_state: str = "not_required",
) -> dict[str, object]:
    presentable = import_state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
        "recovery_required",
    }
    recovery = import_state == "recovery_required"
    return {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1",
        "status": "verified",
        "import_state": import_state,
        "blocking_reason": "candidate_destination_exists" if recovery else "",
        "request_id": "pqtrustintake_" + "f" * 24 if presentable else "",
        "action_required": presentable,
        "interrupt_operator": presentable,
        "operator_review_required": False,
        "presentation": {
            "state": "novel" if presentable else "not_required",
            "presentation_digest": "f" * 64 if presentable else "",
            "already_presented": False,
            "reminder_due": False,
        },
        "candidate_import_authorized": False,
        "candidate_import_attempted": recovery,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
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
        "progress": {"current_evidence_verified": True},
    }


def _verified_source_trust_candidate_artifact_request(
    *,
    request_state: str = "not_required",
) -> dict[str, object]:
    staged = request_state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
    }
    return {
        "status": "verified",
        "request_state": request_state,
        "artifact_request_staged": staged,
        "artifact_request_receipt_sha256": "a" * 64,
        "action_required": staged,
        "interrupt_operator": False,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
            "path_free_projection_verified": True,
            "public_only_projection_verified": True,
        },
        "producer_contacted": False,
        "transport_delivery_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_source_trust_candidate_manual_action(
    *,
    action_state: str = "not_required",
    interrupt: bool = False,
) -> dict[str, object]:
    staged = action_state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
        "recovery_required",
    }
    return {
        "status": "verified",
        "action_state": action_state,
        "operator_action_receipt_staged": staged,
        "operator_action_receipt_sha256": "b" * 64,
        "action_required": staged,
        "interrupt_operator": staged and interrupt,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
            "artifact_request_binding_verified": True,
            "presentation_binding_verified": True,
            "path_free_projection_verified": True,
            "public_only_projection_verified": True,
        },
        "producer_contacted": False,
        "transport_delivery_authorized": False,
        "transport_delivery_attempted": False,
        "notification_sent": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_source_trust_candidate_artifact_notification(
    *,
    staged: bool = False,
    interrupt: bool = False,
    completed: bool = False,
) -> dict[str, object]:
    notification_status = (
        "completed"
        if completed
        else "action_required"
        if staged and interrupt
        else "deduplicated"
        if staged
        else "not_required"
    )
    return {
        "status": "verified",
        "notification_status": notification_status,
        "action_required": staged,
        "interrupt_operator": notification_status == "action_required",
        "delivery_authorized": completed,
        "delivery_attempted": completed,
        "sent": completed,
        "presentation_recorded": completed,
        "receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "notification_integrity_verified": True,
            "artifact_request_binding_verified": True,
            "operator_action_binding_verified": True,
            "presentation_binding_verified": True,
            "path_free_projection_verified": True,
        },
        "producer_contacted": False,
        "artifact_transport_authorized": False,
        "artifact_transport_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
    }
def _verified_source_trust_decision() -> dict[str, object]:
    return {
        "status": "not_required",
        "review_state": "not_required",
        "blocking_reason": "",
        "progress": {
            "current_evidence_verified": True,
            "candidate_review_verified": True,
            "candidate_decision_recorded": False,
        },
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _verified_source_trust_notification() -> dict[str, object]:
    return {
        "status": "verified",
        "notification_status": "not_required",
        "blocking_reason": "",
        "candidate_review_id": "",
        "action_required": False,
        "interrupt_operator": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "presentation_recorded": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "progress": {
            "current_evidence_verified": True,
            "notification_integrity_verified": True,
            "candidate_review_binding_verified": True,
        },
    }


def _verified_source_trust_enrollment_preview(
    *,
    preview_state: str = "not_required",
) -> dict[str, object]:
    staged = preview_state == "preview_staged"
    return {
        "status": "verified",
        "preview_state": preview_state,
        "blocking_reason": "",
        "preview_id": "pqtrustpreview_" + "b" * 24 if staged else "",
        "authorization_scope": (
            {
                "operation": "replace_trust_registry_with_exact_preview",
                "authorization_recorded": False,
            }
            if staged
            else {}
        ),
        "preview_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "action_required": staged,
        "interrupt_operator": staged,
        "operator_review_required": staged,
        "preview_staged": staged,
        "identity_verification_asserted": staged,
        "trust_enrollment_preview_authorized": staged,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            "current_evidence_verified": True,
            "intake_binding_verified": True,
            "decision_binding_verified": True,
            "trust_registry_binding_verified": True,
            "preview_integrity_verified": True,
        },
    }


def _verified_source_trust_enrollment_authorization(
    *,
    authorization_state: str = "not_required",
) -> dict[str, object]:
    pending = authorization_state == "exact_preview_authorization_pending"
    recorded = authorization_state in {
        "exact_preview_authorized",
        "exact_preview_rejected",
        "deferred",
    }
    exact = authorization_state == "exact_preview_authorized"
    decision = {
        "exact_preview_authorized": "authorize_exact_preview",
        "exact_preview_rejected": "reject",
        "deferred": "defer",
    }.get(authorization_state, "")
    return {
        "status": "pending" if pending else "verified" if recorded else "not_required",
        "authorization_state": authorization_state,
        "blocking_reason": "",
        "authorization_id": "pqtrustauth_" + "c" * 24 if recorded else "",
        "decision": decision,
        "action_required": pending,
        "interrupt_operator": False,
        "operator_review_required": pending,
        "explicit_authorization_recorded": recorded,
        "exact_preview_authorized": exact,
        "trust_enrollment_authorized": exact,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            "current_evidence_verified": True,
            "preview_binding_verified": True,
            "authorization_decision_recorded": recorded,
        },
    }


def _verified_source_trust_enrollment_execution_readiness(
    *,
    readiness_state: str = "not_required",
) -> dict[str, object]:
    ready = readiness_state == "ready_for_governed_execution"
    recorded = readiness_state in {
        "ready_for_governed_execution",
        "authorization_rejected",
        "deferred",
    }
    return {
        "status": "verified",
        "readiness_state": readiness_state,
        "blocking_reason": "",
        "readiness_id": "pqtrustready_" + "d" * 24,
        "action_required": ready,
        "interrupt_operator": False,
        "operator_review_required": ready,
        "explicit_authorization_recorded": recorded,
        "exact_preview_authorized": ready,
        "trust_enrollment_authorized": ready,
        "execution_request_staged": ready,
        "execution_readiness_verified": ready,
        "governed_execution_available": ready,
        "trust_registry_modified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "readiness_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "preview_binding_verified": True,
            "authorization_binding_verified": True,
            "registry_binding_verified": True,
            "readiness_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    }


@pytest.fixture(autouse=True)
def _current_source_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import propertyquarry_ooda_source_refresh_settlement as settlement
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as trust_enrollment_authorization
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution as trust_enrollment_execution
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as trust_enrollment_execution_readiness
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as trust_candidate_artifact_request
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as trust_candidate_artifact_notification
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as trust_candidate_import
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_manual_action as trust_candidate_manual_action
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    monkeypatch.setattr(
        settlement,
        "inspect_current_settlement_bundle",
        lambda **_kwargs: _verified_source_settlement(),
    )
    monkeypatch.setattr(
        trust_intake,
        "inspect_trust_intake_bundle",
        lambda **_kwargs: _verified_source_trust_intake(),
    )
    monkeypatch.setattr(
        trust_candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: _verified_source_trust_candidate_import(),
    )
    monkeypatch.setattr(
        trust_candidate_artifact_request,
        "inspect_candidate_artifact_request",
        lambda report, **_kwargs: _verified_source_trust_candidate_artifact_request(
            request_state=str(report.get("import_state") or "not_required")
        ),
    )
    monkeypatch.setattr(
        trust_candidate_manual_action,
        "inspect_candidate_manual_action",
        lambda report, _artifact, **_kwargs: (
            _verified_source_trust_candidate_manual_action(
                action_state=str(report.get("import_state") or "not_required"),
                interrupt=report.get("interrupt_operator") is True,
            )
        ),
    )
    monkeypatch.setattr(
        trust_candidate_artifact_notification,
        "inspect_candidate_artifact_notification",
        lambda _report, _artifact, action, **_kwargs: (
            _verified_source_trust_candidate_artifact_notification(
                staged=action.get("operator_action_receipt_staged") is True,
                interrupt=action.get("interrupt_operator") is True,
            )
        ),
    )
    monkeypatch.setattr(
        trust_decision,
        "verify_current_candidate_review_decision",
        lambda **_kwargs: _verified_source_trust_decision(),
    )
    monkeypatch.setattr(
        trust_notification,
        "inspect_current_candidate_notification",
        lambda **_kwargs: _verified_source_trust_notification(),
    )
    monkeypatch.setattr(
        trust_enrollment_preview,
        "inspect_current_enrollment_preview",
        lambda **_kwargs: _verified_source_trust_enrollment_preview(),
    )
    monkeypatch.setattr(
        trust_enrollment_authorization,
        "verify_current_enrollment_authorization",
        lambda **_kwargs: _verified_source_trust_enrollment_authorization(),
    )
    monkeypatch.setattr(
        trust_enrollment_execution_readiness,
        "inspect_current_execution_readiness",
        lambda **_kwargs: _verified_source_trust_enrollment_execution_readiness(),
    )
    monkeypatch.setattr(
        trust_enrollment_execution,
        "inspect_execution_for_readiness",
        lambda report, **_kwargs: {
            "status": "verified",
            "execution_state": {
                "not_required": "not_required",
                "awaiting_exact_preview_authorization": "awaiting_authorization",
                "authorization_rejected": "authorization_rejected",
                "deferred": "deferred",
                "ready_for_governed_execution": "ready_for_manual_execution",
            }[str(report.get("readiness_state") or "")],
            "action_required": report.get("readiness_state")
            == "ready_for_governed_execution",
            "interrupt_operator": False,
            "manual_execution_authorized": report.get("readiness_state")
            == "ready_for_governed_execution",
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "trust_registry_modified": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "progress": {"current_evidence_verified": True},
        },
    )


def test_source_trust_candidate_import_inspection_uses_configured_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import (
        propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import,
    )

    candidate_dir = tmp_path / "candidate-inbox"
    import_dir = tmp_path / "import-history"
    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR",
        str(candidate_dir),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_DIR",
        str(import_dir),
    )

    def inspect(report, **kwargs):
        observed["report"] = report
        observed.update(kwargs)
        return _verified_source_trust_candidate_import()

    monkeypatch.setattr(
        candidate_import,
        "inspect_candidate_import_readiness",
        inspect,
    )
    intake = _verified_source_trust_intake()

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_candidate_import(
        intake
    )

    assert result == {
        **_verified_source_trust_candidate_import(),
        "presentation": {
            "state": "not_required",
            "presentation_digest": "",
            "already_presented": False,
            "reminder_due": False,
        },
    }
    assert observed["report"] is intake
    assert observed["candidate_dir"] == candidate_dir
    assert observed["import_dir"] == import_dir


def test_source_trust_candidate_artifact_request_uses_configured_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import (
        propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as artifact_request,
    )

    receipt_path = tmp_path / "public-candidate-artifact-request.json"
    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_REQUEST_RECEIPT_PATH",
        str(receipt_path),
    )

    def inspect(report, **kwargs):
        observed["report"] = report
        observed.update(kwargs)
        return _verified_source_trust_candidate_artifact_request()

    monkeypatch.setattr(
        artifact_request,
        "inspect_candidate_artifact_request",
        inspect,
    )
    candidate_import = _verified_source_trust_candidate_import()

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_request(
        candidate_import
    )

    assert result == _verified_source_trust_candidate_artifact_request()
    assert observed["report"] is candidate_import
    assert observed["receipt_path"] == receipt_path


def test_source_trust_candidate_manual_action_uses_configured_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import (
        propertyquarry_ooda_source_refresh_trust_candidate_manual_action as manual_action,
    )

    receipt_path = tmp_path / "candidate-manual-action.json"
    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_MANUAL_ACTION_RECEIPT_PATH",
        str(receipt_path),
    )

    def inspect(report, artifact, **kwargs):
        observed["report"] = report
        observed["artifact"] = artifact
        observed.update(kwargs)
        return _verified_source_trust_candidate_manual_action()

    monkeypatch.setattr(
        manual_action,
        "inspect_candidate_manual_action",
        inspect,
    )
    candidate_import = _verified_source_trust_candidate_import()
    artifact = _verified_source_trust_candidate_artifact_request()

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_candidate_manual_action(
        candidate_import,
        artifact,
    )

    assert result == _verified_source_trust_candidate_manual_action()
    assert observed["report"] is candidate_import
    assert observed["artifact"] is artifact
    assert observed["receipt_path"] == receipt_path


def test_source_trust_candidate_artifact_notification_uses_configured_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import (
        propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as notification,
    )

    receipt_path = tmp_path / "candidate-artifact-notification.json"
    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_NOTIFICATION_RECEIPT_PATH",
        str(receipt_path),
    )

    def inspect(report, artifact, action, **kwargs):
        observed["report"] = report
        observed["artifact"] = artifact
        observed["action"] = action
        observed.update(kwargs)
        return _verified_source_trust_candidate_artifact_notification()

    monkeypatch.setattr(
        notification,
        "inspect_candidate_artifact_notification",
        inspect,
    )
    candidate_import = _verified_source_trust_candidate_import()
    artifact = _verified_source_trust_candidate_artifact_request()
    action = _verified_source_trust_candidate_manual_action()

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification(
        candidate_import,
        artifact,
        action,
    )

    assert result == _verified_source_trust_candidate_artifact_notification()
    assert observed["report"] is candidate_import
    assert observed["artifact"] is artifact
    assert observed["action"] is action
    assert observed["receipt_path"] == receipt_path


def test_property_only_scheduler_requires_completed_current_ooda_witness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )

    assert healthcheck.main() == 0


def test_property_only_scheduler_accepts_receipted_artifact_alert_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    candidate = {
        **_verified_source_trust_candidate_import(
            import_state="awaiting_external_artifact"
        ),
        "interrupt_operator": False,
        "presentation": {
            "state": "already_presented",
            "presentation_digest": "d" * 64,
            "already_presented": True,
            "reminder_due": False,
        },
    }
    artifact = _verified_source_trust_candidate_artifact_request(
        request_state="awaiting_external_artifact"
    )
    action = _verified_source_trust_candidate_manual_action(
        action_state="awaiting_external_artifact",
        interrupt=False,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_candidate_import",
        lambda _intake: candidate,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_request",
        lambda _candidate: artifact,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_candidate_manual_action",
        lambda _candidate, _artifact: action,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification",
        lambda _candidate, _artifact, _action: (
            _verified_source_trust_candidate_artifact_notification(
                staged=True,
                completed=True,
            )
        ),
    )

    assert healthcheck.main() == 0


def test_property_only_scheduler_accepts_novel_import_recovery_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_candidate_import",
        lambda _intake: _verified_source_trust_candidate_import(
            import_state="recovery_required"
        ),
    )

    assert healthcheck.main() == 0


@pytest.mark.parametrize(
    "witness",
    [
        {
            **_verified_witness(),
            "status": "blocked",
            "blocking_reason": "scheduler_iteration_witness_not_fresh",
            "persistent_reevaluation_verified": False,
        },
        {
            **_verified_witness(),
            "iteration_status": "timeout",
            "persistent_reevaluation_verified": False,
            "progress": {
                "current_evidence_verified": True,
                "cycle_binding_verified": False,
            },
        },
        {
            **_verified_witness(),
            "deployment_or_restart_authorized": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_ooda_iteration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    witness: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        lambda: dict(witness),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    ("role", "profile", "enabled"),
    [
        ("worker", "property_only", "1"),
        ("scheduler", "", "1"),
        ("scheduler", "property_only", "0"),
    ],
)
def test_non_propertyquarry_scheduler_posture_does_not_require_ooda_witness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    profile: str,
    enabled: str,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path, role=role)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", profile)
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", enabled)
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        lambda: (_ for _ in ()).throw(
            AssertionError("witness must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_intake",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh trust intake must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_decision",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh trust decision must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_notification",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "source refresh trust notification must not be consulted"
            )
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "source refresh trust enrollment preview must not be consulted"
            )
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_authorization",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "source refresh trust enrollment authorization must not be consulted"
            )
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "source refresh trust enrollment execution readiness must not be consulted"
            )
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_settlement",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh settlement must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh claims must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh handoff must not be consulted")
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        lambda: (_ for _ in ()).throw(
            AssertionError("source refresh request must not be consulted")
        ),
    )

    assert healthcheck.main() == 0


@pytest.mark.parametrize(
    "source_refresh",
    [
        {
            **_verified_source_refresh(),
            "status": "blocked",
            "blocking_reason": "source_refresh_request_bundle_not_current",
        },
        {
            **_verified_source_refresh(),
            "request_receipt_persisted": False,
        },
        {
            **_verified_source_refresh(),
            "producer_dispatch_authorized": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_source_refresh_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_refresh: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        lambda: dict(source_refresh),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    "source_handoff",
    [
        {
            **_verified_source_handoff(),
            "status": "blocked",
            "blocking_reason": "source_refresh_handoff_bundle_not_current",
        },
        {
            **_verified_source_handoff(),
            "handoff_receipt_persisted": False,
        },
        {
            **_verified_source_handoff(),
            "producer_dispatch_authorized": True,
        },
        {
            **_verified_source_handoff(),
            "request_binding_verified": False,
            "progress": {
                "current_evidence_verified": True,
                "handoff_integrity_verified": True,
                "request_binding_verified": False,
            },
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_source_refresh_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_handoff: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        lambda: dict(source_handoff),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    "source_claims",
    [
        {
            **_verified_source_claims(),
            "status": "blocked",
            "claim_state": "blocked",
            "blocking_reason": "source_refresh_claim_bundle_not_current",
        },
        {
            **_verified_source_claims(),
            "lifecycle_receipt_persisted": False,
        },
        {
            **_verified_source_claims(),
            "claim_directory": {"required": True, "present": False},
        },
        {
            **_verified_source_claims(),
            "claim_confers_authority": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_source_refresh_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_claims: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        lambda: dict(source_claims),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    "source_settlement",
    [
        {
            **_verified_source_settlement(),
            "status": "blocked",
            "settlement_state": "blocked",
            "blocking_reason": "source_refresh_settlement_bundle_not_current",
        },
        {
            **_verified_source_settlement(),
            "settlement_receipt_persisted": False,
        },
        {
            **_verified_source_settlement(),
            "completion_directory": {"required": True, "present": False},
        },
        {
            **_verified_source_settlement(),
            "completion_confers_authority": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_source_settlement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_settlement: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_settlement",
        lambda: dict(source_settlement),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    "source_trust_intake",
    [
        {
            **_verified_source_trust_intake(),
            "status": "blocked",
            "intake_state": "blocked",
        },
        {
            **_verified_source_trust_intake(),
            "candidate_directory": {
                "present": False,
                "current_candidate_file_count": 0,
            },
        },
        {
            **_verified_source_trust_intake(),
            "trust_enrollment_authorized": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_trust_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_trust_intake: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_intake",
        lambda: dict(source_trust_intake),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize(
    "source_trust_decision",
    [
        {
            **_verified_source_trust_decision(),
            "status": "blocked",
            "review_state": "blocked",
        },
        {
            **_verified_source_trust_decision(),
            "trust_enrollment_authorized": True,
        },
        {
            **_verified_source_trust_decision(),
            "status": "pending",
            "review_state": "candidate_review_pending",
            "action_required": False,
            "operator_review_required": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_trust_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_trust_decision: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_decision",
        lambda: dict(source_trust_decision),
    )

    assert healthcheck.main() == 1


def test_property_only_scheduler_accepts_unconfigured_trust_as_safe_current_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        lambda: {
            **_verified_source_claims(),
            "claim_state": "producer_trust_unconfigured",
            "settlement_state": "awaiting_producer_trust",
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_settlement",
        lambda: {
            **_verified_source_settlement(),
            "settlement_state": "producer_trust_unconfigured",
            },
        )
    assert healthcheck.main() == 0


@pytest.mark.parametrize(
    "source_trust_notification",
    [
        {
            **_verified_source_trust_notification(),
            "status": "blocked",
            "notification_status": "blocked",
        },
        {
            **_verified_source_trust_notification(),
            "notification_status": "completed",
            "action_required": True,
            "interrupt_operator": True,
            "delivery_authorized": True,
            "delivery_attempted": True,
            "sent": False,
            "presentation_recorded": True,
        },
        {
            **_verified_source_trust_notification(),
            "trust_registry_modified": True,
        },
    ],
)
def test_property_only_scheduler_fails_closed_on_unproven_trust_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_trust_notification: dict[str, object],
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_notification",
        lambda: dict(source_trust_notification),
    )

    assert healthcheck.main() == 1


@pytest.mark.parametrize("notification_status", ["action_required", "completed"])
def test_property_only_scheduler_accepts_current_genuine_candidate_alert_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    notification_status: str,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_intake",
        lambda: {
            **_verified_source_trust_intake(),
            "intake_state": "candidate_ready_for_operator_review",
            "candidates": [{"public_key_sha256": "a" * 64}],
            "candidate_directory": {
                "present": True,
                "current_candidate_file_count": 1,
            },
            "action_required": True,
            "interrupt_operator": True,
            "operator_review_required": True,
            "public_key_candidate_recorded": True,
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_decision",
        lambda: {
            **_verified_source_trust_decision(),
            "status": "pending",
            "review_state": "candidate_review_pending",
            "action_required": True,
            "operator_review_required": True,
        },
    )
    completed = notification_status == "completed"
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_notification",
        lambda: {
            **_verified_source_trust_notification(),
            "notification_status": notification_status,
            "candidate_review_id": "pqtrustreview_" + "a" * 24,
            "action_required": True,
            "interrupt_operator": True,
            "delivery_authorized": completed,
            "delivery_attempted": completed,
            "sent": completed,
            "presentation_recorded": completed,
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview",
        lambda: _verified_source_trust_enrollment_preview(
            preview_state="awaiting_decision"
        ),
    )

    assert healthcheck.main() == 0


@pytest.mark.parametrize(
    "authorization_state",
    [
        "exact_preview_authorization_pending",
        "exact_preview_authorized",
        "exact_preview_rejected",
        "deferred",
    ],
)
def test_property_only_scheduler_accepts_confirmed_exact_enrollment_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authorization_state: str,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_intake",
        lambda: {
            **_verified_source_trust_intake(),
            "intake_state": "candidate_ready_for_operator_review",
            "candidates": [{"public_key_sha256": "a" * 64}],
            "candidate_directory": {
                "present": True,
                "current_candidate_file_count": 1,
            },
            "action_required": True,
            "interrupt_operator": True,
            "operator_review_required": True,
            "public_key_candidate_recorded": True,
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_decision",
        lambda: {
            **_verified_source_trust_decision(),
            "status": "verified",
            "review_state": "identity_verified",
            "decision": "confirm_identity_verified",
            "identity_verification_asserted": True,
            "trust_enrollment_preview_authorized": True,
            "progress": {
                "current_evidence_verified": True,
                "candidate_review_verified": True,
                "candidate_decision_recorded": True,
            },
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_notification",
        lambda: {
            **_verified_source_trust_notification(),
            "notification_status": "resolved",
            "candidate_review_id": "pqtrustreview_" + "a" * 24,
            "trust_enrollment_preview_authorized": True,
        },
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview",
        lambda: _verified_source_trust_enrollment_preview(
            preview_state="preview_staged"
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_authorization",
        lambda: _verified_source_trust_enrollment_authorization(
            authorization_state=authorization_state
        ),
    )
    readiness_state = {
        "exact_preview_authorization_pending": (
            "awaiting_exact_preview_authorization"
        ),
        "exact_preview_authorized": "ready_for_governed_execution",
        "exact_preview_rejected": "authorization_rejected",
        "deferred": "deferred",
    }[authorization_state]
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness",
        lambda: _verified_source_trust_enrollment_execution_readiness(
            readiness_state=readiness_state
        ),
    )

    assert healthcheck.main() == 0


def test_property_only_scheduler_fails_closed_on_unsafe_enrollment_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview",
        lambda: {
            **_verified_source_trust_enrollment_preview(),
            "trust_registry_modified": True,
        },
    )

    assert healthcheck.main() == 1


def test_property_only_scheduler_fails_closed_on_unsafe_execution_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_process_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "1")
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_witness",
        _verified_witness,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_request",
        _verified_source_refresh,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_handoff",
        _verified_source_handoff,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_claims",
        _verified_source_claims,
    )
    monkeypatch.setattr(
        healthcheck,
        "_verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness",
        lambda: {
            **_verified_source_trust_enrollment_execution_readiness(),
            "trust_registry_modified": True,
        },
    )

    assert healthcheck.main() == 1


def test_source_refresh_request_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    observed: dict[str, object] = {}
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH", "/r/cycle")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_APPROVAL_MANIFEST_PATH", "/s/manifest.json")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_SOURCE_REFRESH_REQUEST_PATH", "/r/request")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_SOURCE_REFRESH_VERIFICATION_PATH", "/r/request-v")
    monkeypatch.setattr(
        source_refresh,
        "inspect_current_source_refresh_request_bundle",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_request()

    assert result == {"status": "verified"}
    assert observed["cycle_receipt_path"] == Path("/r/cycle")
    assert observed["signal_dir"] == Path("/s")
    assert observed["request_path"] == Path("/r/request")
    assert observed["verification_path"] == Path("/r/request-v")


def test_source_refresh_claim_probe_requires_the_read_only_runtime_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims

    observed: dict[str, object] = {}
    monkeypatch.setattr(
        source_claims,
        "inspect_current_claim_lifecycle_bundle",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_claims()

    assert result == {"status": "verified"}
    assert observed["require_claim_dir"] is True
    assert observed["claim_dir"] == Path(
        "/run/propertyquarry/ooda-producer-claims"
    )
    assert observed["trust_registry_path"] == Path(
        "/config/propertyquarry_ooda_source_refresh_producer_trust.v1.json"
    )


def test_source_refresh_settlement_probe_requires_read_only_completion_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_settlement as settlement

    observed: dict[str, object] = {}
    monkeypatch.setattr(
        settlement,
        "inspect_current_settlement_bundle",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_settlement()

    assert result == {"status": "verified"}
    assert observed["require_completion_dir"] is True
    assert observed["completion_dir"] == Path(
        "/run/propertyquarry/ooda-producer-completions"
    )
    assert observed["trust_registry_path"] == Path(
        "/config/propertyquarry_ooda_source_refresh_producer_trust.v1.json"
    )


def test_source_refresh_trust_intake_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH",
        "/r/claims-v",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH",
        "/r/trust",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR",
        "/r/candidates",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH",
        "/r/intake",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH",
        "/r/intake-v",
    )
    monkeypatch.setattr(
        trust_intake,
        "inspect_trust_intake_bundle",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_intake()

    assert result == {"status": "verified"}
    assert observed["claim_verification_path"] == Path("/r/claims-v")
    assert observed["trust_registry_path"] == Path("/r/trust")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["receipt_path"] == Path("/r/intake")
    assert observed["verification_path"] == Path("/r/intake-v")


def test_source_refresh_trust_decision_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision

    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR",
        "/r/decisions",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR",
        "/r/candidates",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH",
        "/r/intake",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH",
        "/r/intake-v",
    )
    monkeypatch.setattr(
        trust_decision,
        "verify_current_candidate_review_decision",
        lambda **kwargs: observed.update(kwargs) or {"status": "not_required"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_decision()

    assert result == {"status": "not_required"}
    assert observed["decision_dir"] == Path("/r/decisions")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["intake_receipt_path"] == Path("/r/intake")
    assert observed["intake_verification_path"] == Path("/r/intake-v")


def test_source_refresh_trust_notification_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR",
        "/r/decisions",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR",
        "/r/candidates",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH",
        "/r/intake",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH",
        "/r/intake-v",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_PRESENTATION_STATE_PATH",
        "/r/presentation",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_NOTIFICATION_RECEIPT_PATH",
        "/r/notification",
    )
    monkeypatch.setattr(
        trust_notification,
        "inspect_current_candidate_notification",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = (
        healthcheck._verify_propertyquarry_ooda_source_refresh_trust_notification()
    )

    assert result == {"status": "verified"}
    assert observed["decision_dir"] == Path("/r/decisions")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["intake_receipt_path"] == Path("/r/intake")
    assert observed["intake_verification_path"] == Path("/r/intake-v")
    assert observed["presentation_state_path"] == Path("/r/presentation")
    assert observed["receipt_path"] == Path("/r/notification")


def test_source_refresh_trust_enrollment_preview_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview

    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR",
        "/r/decisions",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH",
        "/r/claims-v",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH",
        "/r/trust",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR",
        "/r/candidates",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH",
        "/r/intake",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH",
        "/r/intake-v",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH",
        "/r/preview",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH",
        "/r/preview-v",
    )
    monkeypatch.setattr(
        trust_enrollment_preview,
        "inspect_current_enrollment_preview",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = (
        healthcheck._verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview()
    )

    assert result == {"status": "verified"}
    assert observed["decision_dir"] == Path("/r/decisions")
    assert observed["claim_verification_path"] == Path("/r/claims-v")
    assert observed["trust_registry_path"] == Path("/r/trust")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["intake_receipt_path"] == Path("/r/intake")
    assert observed["intake_verification_path"] == Path("/r/intake-v")
    assert observed["receipt_path"] == Path("/r/preview")
    assert observed["verification_path"] == Path("/r/preview-v")


def test_source_refresh_trust_enrollment_authorization_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as trust_enrollment_authorization

    observed: dict[str, object] = {}
    environment = {
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_DIR": "/r/authorizations",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR": "/r/decisions",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH": "/r/claims-v",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH": "/r/trust",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR": "/r/candidates",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH": "/r/intake",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH": "/r/intake-v",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH": "/r/preview",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH": "/r/preview-v",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        trust_enrollment_authorization,
        "verify_current_enrollment_authorization",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = (
        healthcheck._verify_propertyquarry_ooda_source_refresh_trust_enrollment_authorization()
    )

    assert result == {"status": "verified"}
    assert observed["decision_dir"] == Path("/r/authorizations")
    assert observed["preview_decision_dir"] == Path("/r/decisions")
    assert observed["claim_verification_path"] == Path("/r/claims-v")
    assert observed["trust_registry_path"] == Path("/r/trust")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["intake_receipt_path"] == Path("/r/intake")
    assert observed["intake_verification_path"] == Path("/r/intake-v")
    assert observed["preview_receipt_path"] == Path("/r/preview")
    assert observed["preview_verification_path"] == Path("/r/preview-v")


def test_source_refresh_trust_enrollment_execution_readiness_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as execution_readiness

    observed: dict[str, object] = {}
    environment = {
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_DIR": "/r/authorizations",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR": "/r/decisions",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH": "/r/claims-v",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH": "/r/trust",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR": "/r/candidates",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH": "/r/intake",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH": "/r/intake-v",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH": "/r/preview",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH": "/r/preview-v",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS_RECEIPT_PATH": "/r/readiness",
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS_VERIFICATION_PATH": "/r/readiness-v",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        execution_readiness,
        "inspect_current_execution_readiness",
        lambda **kwargs: observed.update(kwargs) or {"status": "verified"},
    )

    result = healthcheck._verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness()

    assert result == {"status": "verified"}
    assert observed["authorization_dir"] == Path("/r/authorizations")
    assert observed["preview_decision_dir"] == Path("/r/decisions")
    assert observed["claim_verification_path"] == Path("/r/claims-v")
    assert observed["trust_registry_path"] == Path("/r/trust")
    assert observed["candidate_dir"] == Path("/r/candidates")
    assert observed["intake_receipt_path"] == Path("/r/intake")
    assert observed["intake_verification_path"] == Path("/r/intake-v")
    assert observed["preview_receipt_path"] == Path("/r/preview")
    assert observed["preview_verification_path"] == Path("/r/preview-v")
    assert observed["receipt_path"] == Path("/r/readiness")
    assert observed["verification_path"] == Path("/r/readiness-v")


def test_source_refresh_trust_enrollment_execution_probe_routes_exact_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution as execution

    observed: dict[str, object] = {}
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_DIR",
        "/r/executions",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_BACKUP_DIR",
        "/r/backups",
    )
    monkeypatch.setattr(
        execution,
        "inspect_execution_for_readiness",
        lambda report, **kwargs: observed.update(
            {"report": report, **kwargs}
        )
        or {"status": "verified"},
    )
    readiness = {"status": "verified", "readiness_state": "not_required"}

    result = (
        healthcheck._verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution(
            readiness
        )
    )

    assert result == {"status": "verified"}
    assert observed["report"] == readiness
    assert observed["execution_dir"] == Path("/r/executions")
    assert observed["backup_dir"] == Path("/r/backups")
