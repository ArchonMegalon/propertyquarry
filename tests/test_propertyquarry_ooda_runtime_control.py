from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_runtime_control as runtime_control


NOW = datetime(2026, 8, 28, 0, 5, tzinfo=timezone.utc)


def _false_authority() -> dict[str, object]:
    return {
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _review() -> dict[str, object]:
    return {
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "live_runtime_tunnel_unavailable",
        "progress": {"current_evidence_verified": True},
        **_false_authority(),
    }


def _authorization_handoff() -> dict[str, object]:
    return {
        "status": "ready",
        "request_state": "refreshed",
        "request_id": "pqar_" + "a" * 24,
        "request_sha256": "1" * 64,
        "current_evidence_verified": True,
        **_false_authority(),
    }


def _configuration_handoff(
    *,
    decision: str = "pending",
) -> dict[str, object]:
    return {
        "status": "ready",
        "plan_state": "refreshed",
        "plan_id": "pqcp_" + "b" * 24,
        "authorization_decision": decision,
        "current_evidence_verified": True,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        **_false_authority(),
    }


def _manual_action(
    *,
    ready: bool = False,
) -> dict[str, object]:
    common = {
        "status": "ready" if ready else "not_authorized",
        "current_evidence_verified": True,
        "automatic_execution_allowed": False,
        "source_edit_performed": False,
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    if ready:
        common.update(
            {
                "next_action": "invoke the exact staged add-only configuration action",
                "handoff_id": "pqmh_" + "c" * 24,
                "handoff_sha256": "2" * 64,
                "handoff_path": "/private/manual.json",
                "request_id": "pqar_" + "a" * 24,
                "plan_id": "pqcp_" + "b" * 24,
                "preview_id": "pqcv_" + "d" * 24,
                "target": ".env",
                "apply_command": "python3 scripts/apply.py --exact",
                "rollback_command_source": "verified apply receipt",
                "manual_invocation_required": True,
                "manual_apply_authorized": True,
            }
        )
    return common


def _posture(*, authorized: bool = False) -> dict[str, object]:
    return {
        "status": (
            "authorized_exact_scope" if authorized else "pending_operator_decision"
        ),
        "posture_receipt_persisted": True,
        **_false_authority(),
    }


def _projected(*, interrupt: bool = True) -> dict[str, object]:
    return {
        "status": "action_required" if interrupt else "pending_action",
        "action_required": True,
        "interrupt_operator": interrupt,
        "next_action": "await an explicit operator decision",
        "actions": [
            {
                "lane": "gold_live_runtime",
                "reason": "live_runtime_tunnel_unavailable",
                "consent_required": True,
                "automatic_execution_allowed": False,
                "provider_quota_consumption_allowed": False,
                "protected_operations": ["runtime_configuration_change"],
            }
        ]
        if interrupt
        else [],
        **_false_authority(),
    }


def _install_common_stubs(monkeypatch, *, authorized: bool = False) -> None:
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "verify_current_review_packet",
        lambda **_kwargs: _review(),
    )
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "operator_presentation_context",
        lambda _verification: {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {"compose_project": "property"},
        },
    )
    monkeypatch.setattr(
        runtime_control.authorization_request,
        "stage_current_authorization_handoff",
        lambda **_kwargs: _authorization_handoff(),
    )
    monkeypatch.setattr(
        runtime_control.configuration_plan,
        "stage_current_configuration_handoff",
        lambda **_kwargs: _configuration_handoff(
            decision="approve_exact_scope" if authorized else "pending"
        ),
    )
    monkeypatch.setattr(
        runtime_control.manual_action,
        "stage_current_manual_action_handoff",
        lambda **_kwargs: _manual_action(ready=authorized),
    )
    monkeypatch.setattr(
        runtime_control.authority_posture,
        "refresh_current_authority_posture",
        lambda **_kwargs: _posture(authorized=authorized),
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "load_operator_status",
        lambda **_kwargs: {"status": "action_required"},
    )


def test_pending_runtime_control_stages_one_private_novel_authorization_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_common_stubs(monkeypatch)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        runtime_control.action_only,
        "_runtime_authorization_presentation",
        lambda handoff, _context: {
            "request_id": handoff["request_id"],
            "authorization_instruction": "Authorize exact request",
        },
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda _status, **kwargs: (
            observed.update(kwargs) or _projected(interrupt=True)
        ),
    )
    monkeypatch.setattr(
        runtime_control.action_only,
        "build_action_only_projection",
        lambda _status, **kwargs: (
            observed.update({"action_contexts": kwargs["presentation_context_by_lane"]})
            or {
                "status": "action_required",
                "next_action": "Authorize exact request",
                "actions": [{"lane": "gold_live_runtime"}],
            }
        ),
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        packet_path=tmp_path / "packet.json",
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        live_mobile_receipt_path=tmp_path / "origin.json",
        release_manifest_path=tmp_path / "release.md",
        deployment_env_path=tmp_path / ".env",
        presentation_state_path=tmp_path / "presentation.json",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["projection_kind"] == "runtime_configuration_authorization"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["presentation_recorded"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    assert result["protected_operation_executed"] is False
    context = observed["action_contexts"]["gold_live_runtime"]
    assert context["scope"]["authorization_request"]["request_id"] == (
        "pqar_" + "a" * 24
    )
    receipt = tmp_path / "private" / "runtime-control-current.json"
    assert json.loads(receipt.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_authorized_runtime_control_stages_manual_action_without_applying(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_common_stubs(monkeypatch, authorized=True)
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        runtime_control.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda _status, **kwargs: (
            observed.update(kwargs) or _projected(interrupt=True)
        ),
    )
    monkeypatch.setattr(
        runtime_control.action_only,
        "build_action_only_projection",
        lambda _status, **_kwargs: {
            "status": "action_required",
            "blocking_reason": "old",
            "next_action": "old",
            "actions": [{"lane": "gold_live_runtime", "safe_next_action": "old"}],
        },
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        presentation_state_path=tmp_path / "presentation.json",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["projection_kind"] == "manual_configuration_apply"
    assert result["interrupt_operator"] is True
    action = result["action_projection"]
    assert action["next_action"] == (
        "invoke the exact staged add-only configuration action"
    )
    assert action["configuration_manual_action"]["manual_apply_authorized"] is True
    assert action["configuration_manual_action"][
        "deployment_or_restart_authorized"
    ] is False
    assert result["protected_operation_executed"] is False
    context = observed["presentation_context_by_lane"]["gold_live_runtime"]
    assert context["scope"]["configuration_manual_action"]["handoff_id"] == (
        "pqmh_" + "c" * 24
    )


def test_retained_runtime_control_action_stays_pending_and_does_not_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_common_stubs(monkeypatch)
    monkeypatch.setattr(
        runtime_control.action_only,
        "_runtime_authorization_presentation",
        lambda handoff, _context: {"request_id": handoff["request_id"]},
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda *_args, **_kwargs: _projected(interrupt=False),
    )
    monkeypatch.setattr(
        runtime_control.action_only,
        "build_action_only_projection",
        lambda *_args, **_kwargs: None,
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["novel_action_count"] == 0
    assert result["action_projection"] == {}
    assert result["presentation_recorded"] is False
    assert result["delivery_attempted"] is False


def test_runtime_control_fails_closed_when_any_chain_component_is_not_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_common_stubs(monkeypatch)
    monkeypatch.setattr(
        runtime_control.configuration_plan,
        "stage_current_configuration_handoff",
        lambda **_kwargs: {
            "status": "blocked",
            "current_evidence_verified": False,
        },
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert (
        result["blocking_reason"]
        == "runtime_control_configuration_plan_not_current"
    )
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["current_evidence_verified"] is False
    assert result["receipt_persisted"] is True
    assert result["protected_operation_executed"] is False
    assert result["delivery_authorized"] is False


def test_scheduler_host_handoff_is_current_but_never_claims_host_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_control.operator_status,
        "load_operator_status",
        lambda **_kwargs: {
            "status": "action_required",
            "source_cycle_receipt_sha256": "3" * 64,
            "actions": [
                {
                    "lane": "gold_live_runtime",
                    "reason": "live_runtime_tunnel_unavailable",
                    "source_generated_at": NOW.isoformat(),
                    "safe_next_action": "inspect the connector",
                }
            ],
            "automatic_execution_allowed": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
        },
    )
    receipt = tmp_path / "host-handoff.json"

    result = runtime_control.stage_host_runtime_control_handoff(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        receipt_path=receipt,
        now=NOW,
    )

    assert result["schema"] == runtime_control.HOST_HANDOFF_SCHEMA
    assert result["status"] == "host_review_required"
    assert result["host_review_required"] is True
    assert result["host_runtime_observation_verified"] is False
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["receipt_persisted"] is True
    assert json.loads(receipt.read_text(encoding="utf-8")) == result


def test_scheduler_host_handoff_is_silent_without_a_runtime_incident(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_control.operator_status,
        "load_operator_status",
        lambda **_kwargs: {
            "status": "ready",
            "source_cycle_receipt_sha256": "4" * 64,
            "actions": [],
            "automatic_execution_allowed": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
        },
    )

    result = runtime_control.stage_host_runtime_control_handoff(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        receipt_path=tmp_path / "host-handoff.json",
        now=NOW,
    )

    assert result["status"] == "not_required"
    assert result["host_review_required"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["receipt_persisted"] is True


def _post_configuration_review() -> dict[str, object]:
    return {
        **_review(),
        "configuration_proposal": {
            "recovery_preview_sha256": "5" * 64,
            "recovery_preview": {
                "operation": "deployment_or_restart",
                "compose_project": "property",
                "application_service": "propertyquarry-api",
                "connector_service": "propertyquarry-cloudflared",
                "eligible_for_authorization": False,
                "blocking_reasons": ["release_worktree_not_clean"],
                "execution_authorized": False,
                "deployment_or_restart_performed": False,
                "deployment_environment": {
                    "configuration_complete": True,
                    "missing_required_key_count": 0,
                    "unresolved_required_key_count": 0,
                    "environment_values_recorded": False,
                    "environment_values_hashed": False,
                    "secret_values_recorded": False,
                    "intake_plan": {
                        "local_merge_review_required": False,
                        "operator_merge_review_required": False,
                    },
                },
            },
        },
    }


def _post_configuration_action(*, refreshed: bool) -> dict[str, object]:
    return {
        "status": "ready" if refreshed else "action_required",
        "state": "applied_evidence_refreshed" if refreshed else "applied",
        "next_action": (
            "review the fresh OODA posture"
            if refreshed
            else "run the exact evaluate-only evidence refresh"
        ),
        "action_command": "python3 scripts/refresh.py --exact",
        "action_required": not refreshed,
        "current_evidence_verified": True,
        "receipt_verified": True,
        "source_state_verified": True,
        "receipt_id": "pqme_" + "6" * 24,
        "receipt_sha256": "7" * 64,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def test_post_configuration_runtime_control_requires_evidence_refresh_without_reopening_merge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "verify_current_review_packet",
        lambda **_kwargs: _post_configuration_review(),
    )
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "operator_presentation_context",
        lambda _verification: {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {"compose_project": "property"},
        },
    )
    monkeypatch.setattr(
        runtime_control.configuration_action_status,
        "inspect_manual_action_status",
        lambda **_kwargs: _post_configuration_action(refreshed=False),
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "load_operator_status",
        lambda **_kwargs: {"status": "action_required", **_false_authority()},
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda *_args, **_kwargs: _projected(interrupt=True),
    )
    monkeypatch.setattr(
        runtime_control.action_only,
        "build_action_only_projection",
        lambda *_args, **_kwargs: {
            "status": "action_required",
            "actions": [{"lane": "gold_live_runtime"}],
        },
    )
    monkeypatch.setattr(
        runtime_control.authorization_request,
        "stage_current_authorization_handoff",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("merge request reopened")),
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        presentation_state_path=tmp_path / "presentation.json",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["projection_kind"] == "configuration_evidence_refresh"
    assert result["next_action"] == "run the exact evaluate-only evidence refresh"
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["components"]["authorization_request"]["status"] == "not_required"


def test_refreshed_post_configuration_runtime_control_surfaces_blockers_without_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "verify_current_review_packet",
        lambda **_kwargs: _post_configuration_review(),
    )
    monkeypatch.setattr(
        runtime_control.runtime_review,
        "operator_presentation_context",
        lambda _verification: {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {"compose_project": "property"},
        },
    )
    monkeypatch.setattr(
        runtime_control.configuration_action_status,
        "inspect_manual_action_status",
        lambda **_kwargs: _post_configuration_action(refreshed=True),
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "load_operator_status",
        lambda **_kwargs: {"status": "action_required", **_false_authority()},
    )
    monkeypatch.setattr(
        runtime_control.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda *_args, **_kwargs: _projected(interrupt=True),
    )
    monkeypatch.setattr(
        runtime_control.action_only,
        "build_action_only_projection",
        lambda *_args, **_kwargs: {
            "status": "action_required",
            "actions": [{"lane": "gold_live_runtime"}],
        },
    )

    result = runtime_control.stage_current_runtime_control(
        artifact_dir=tmp_path / "private",
        presentation_state_path=tmp_path / "presentation.json",
        root=tmp_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["projection_kind"] == "deployment_restart_review"
    assert "release_worktree_not_clean" in result["next_action"]
    assert result["deployment_or_restart_authorized"] is False
    assert result["execution_authorized"] is False
