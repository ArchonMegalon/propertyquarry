from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import propertyquarry_ooda_safe_tick as safe_tick


NOW = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)
REAL_TRUST_NOTIFICATION_MATERIALIZER = (
    safe_tick._materialize_source_trust_notification_projection
)
REAL_LOAD_OPERATOR_PROJECTION = safe_tick._load_operator_projection


def _paths(tmp_path: Path) -> dict[str, Any]:
    sources = tmp_path / "sources"
    sources.mkdir(exist_ok=True)
    paths = {
        "gold_receipt_path": sources / "gold.json",
        "public_origin_observation_path": sources / "public-origin.json",
        "scene_packet_path": sources / "packet.json",
        "scene_verifier_path": sources / "verifier.json",
        "scene_runtime_status_path": sources / "runtime.json",
        "signal_dir": tmp_path / "signals",
        "stage_receipt_path": tmp_path / "receipts" / "stage.json",
        "cycle_receipt_path": tmp_path / "receipts" / "cycle.json",
        "tick_receipt_path": tmp_path / "receipts" / "tick.json",
        "tick_lock_path": tmp_path / "receipts" / "tick.lock",
        "operator_presentation_state_path": (
            tmp_path / "receipts" / "operator-presentation.json"
        ),
        "runtime_review_packet_path": (
            tmp_path / "receipts" / "runtime-review-packet.json"
        ),
        "runtime_review_verification_path": (
            tmp_path / "receipts" / "runtime-review-verification.json"
        ),
        "runtime_review_release_manifest_path": (
            tmp_path / "release-manifest.md"
        ),
        "runtime_review_deployment_env_path": tmp_path / ".env",
        "source_refresh_request_path": tmp_path / "receipts" / "refresh.json",
        "source_refresh_verification_path": (
            tmp_path / "receipts" / "refresh-verification.json"
        ),
        "source_refresh_handoff_path": (
            tmp_path / "receipts" / "refresh-handoff.json"
        ),
        "source_refresh_handoff_verification_path": (
            tmp_path / "receipts" / "refresh-handoff-verification.json"
        ),
        "source_refresh_claim_trust_registry_path": (
            safe_tick.DEFAULT_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY
        ),
        "source_refresh_claim_dir": tmp_path / "producer-claims",
        "source_refresh_claim_receipt_path": (
            tmp_path / "receipts" / "refresh-claims.json"
        ),
        "source_refresh_claim_verification_path": (
            tmp_path / "receipts" / "refresh-claims-verification.json"
        ),
        "source_refresh_trust_intake_candidate_dir": (
            tmp_path / "producer-trust-candidates"
        ),
        "source_refresh_trust_intake_receipt_path": (
            tmp_path / "receipts" / "refresh-trust-intake.json"
        ),
        "source_refresh_trust_intake_verification_path": (
            tmp_path / "receipts" / "refresh-trust-intake-verification.json"
        ),
        "source_refresh_trust_decision_dir": (
            tmp_path / "receipts" / "refresh-trust-decisions"
        ),
        "source_refresh_trust_presentation_state_path": (
            tmp_path / "receipts" / "refresh-trust-presentation.json"
        ),
        "source_refresh_trust_notification_path": (
            tmp_path / "receipts" / "refresh-trust-notification.json"
        ),
        "source_refresh_completion_dir": tmp_path / "producer-completions",
        "source_refresh_settlement_receipt_path": (
            tmp_path / "receipts" / "refresh-settlement.json"
        ),
        "source_refresh_settlement_verification_path": (
            tmp_path / "receipts" / "refresh-settlement-verification.json"
        ),
        "state_path": tmp_path / "receipts" / "state.json",
        "send_lock_path": tmp_path / "receipts" / "send.lock",
        # Unit fixtures use private, already-current observations unless a
        # test intentionally exercises the credential-free live refresh.
        "refresh_public_origin_observation": False,
    }
    for name in (
        "gold_receipt_path",
        "public_origin_observation_path",
        "scene_packet_path",
        "scene_verifier_path",
        "scene_runtime_status_path",
    ):
        paths[name].write_text("{}", encoding="utf-8")
    paths["runtime_review_release_manifest_path"].write_text(
        "test release manifest\n",
        encoding="utf-8",
    )
    _write_private(paths["runtime_review_deployment_env_path"], {})
    return paths


def _write_private(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _operator_projection_for_test(
    *,
    cycle_receipt_path: Path,
    presentation_state_path: Path,
    cycle_evidence: dict[str, object],
    **_kwargs,
) -> dict[str, object]:
    receipt = json.loads(cycle_receipt_path.read_text(encoding="utf-8"))
    action_required = receipt.get("operator_action_required") is True
    interrupt_operator = receipt.get("interrupt_operator") is True
    pending_actions = list(receipt.get("actions") or []) if action_required else []
    actions = pending_actions if interrupt_operator else []
    return {
        "schema": safe_tick.operator_status.SCHEMA,
        "status": "action_required" if interrupt_operator else "ready",
        "action_required": action_required,
        "interrupt_operator": interrupt_operator,
        "updated_at": NOW.isoformat(),
        "blocking_reason": ",".join(
            str(action.get("reason") or "") for action in pending_actions
        ),
        "next_action": (
            str(pending_actions[0].get("safe_next_action") or "")
            if pending_actions
            else "await fresh approved signals"
        ),
        "actions": actions,
        "pending_actions": pending_actions,
        "progress": {"current_snapshot_hashes_verified": True},
        "source_cycle_receipt_sha256": str(cycle_evidence["sha256"]),
        "presentation": {
            "state_path": str(presentation_state_path.resolve()),
            "state_status": "missing",
            "state_sha256": "",
            "pending_action_count": len(pending_actions),
            "novel_action_count": len(actions),
            "delivery_state_independent": True,
            "delivery_state_updated": False,
        },
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
    }


def _trust_notification_projection_for_test(
    *,
    intake: dict[str, object],
    decision: dict[str, object],
    receipt_path: Path,
    **_kwargs,
) -> dict[str, object]:
    resolved = decision.get("status") == "verified"
    action_required = intake.get("action_required") is True and not resolved
    notification_status = (
        "resolved"
        if resolved
        else "action_required"
        if action_required
        else "not_required"
    )
    return {
        "schema": safe_tick.trust_notification.VERIFY_SCHEMA,
        "status": "verified",
        "notification_status": notification_status,
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "next_action": str(
            (
                decision.get("next_action")
                if resolved
                else intake.get("next_action")
            )
            or "await a current verified producer public-key candidate"
        ),
        "candidate_review_id": str(
            intake.get("candidate_review_id") or ""
        ),
        "presentation_status": "novel" if action_required else "not_required",
        "action_required": action_required,
        "interrupt_operator": action_required,
        "operator_review_required": action_required,
        "receipt_path": str(receipt_path.resolve()),
        "notification_receipt_sha256": "8" * 64,
        "notification_receipt_bytes": 123,
        "current_evidence_verified": True,
        "presentation_recorded": False,
        "trust_enrollment_preview_authorized": resolved,
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


def _runtime_review_projection_for_test(
    paths: dict[str, Path],
) -> dict[str, object]:
    return {
        "schema": safe_tick.RUNTIME_REVIEW_COMPONENT_SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "live_runtime_tunnel_unavailable",
        "next_action": (
            "review the exact reversible runtime recovery packet and record "
            "deployment or restart authority separately"
        ),
        "current_evidence_verified": True,
        "review_packet_staged": True,
        "packet_generated_at": NOW.isoformat(),
        "packet_path": str(paths["runtime_review_packet_path"].resolve()),
        "packet_sha256": "6" * 64,
        "packet_bytes": 4096,
        "verification_path": str(
            paths["runtime_review_verification_path"].resolve()
        ),
        "verification_sha256": "7" * 64,
        "configuration_proposal": {
            "authorization_required": True,
            "execution_authorized": False,
            "recovery_preview": {
                "preview_only": True,
                "command_recorded": True,
                "authorization_recorded": False,
                "execution_authorized": False,
            },
        },
        "presentation_context": {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {"change_id": "test-runtime-recovery"},
        },
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


def _runtime_control_report_for_test(
    *,
    action_required: bool = True,
    interrupt_operator: bool = True,
    next_action: str = (
        "reply exactly 'Authorize pqar_" + "a" * 24 + "' to authorize only "
        "this current add-only configuration merge"
    ),
) -> dict[str, object]:
    return {
        "schema": safe_tick.runtime_control.SCHEMA,
        "status": "verified",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "explicit_operator_decision_required",
        "next_action": next_action,
        "action_required": action_required,
        "interrupt_operator": interrupt_operator,
        "presentation_recorded": False,
        "current_evidence_verified": True,
        "receipt_persisted": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _stage_report() -> dict[str, object]:
    return {
        "schema": safe_tick.stage.SCHEMA,
        "status": "ready",
        "blocking_reason": "",
        "publication_status": "approved",
        "manifest_sha256": "a" * 64,
        "source_status": "waiting_for_fresh_sources",
        "source_evidence_posture": {
            "status": "waiting_for_fresh_sources",
            "progress": {"unavailable_lane_count": 0},
        },
        "receipt_persisted": True,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
    }


def _cycle_report() -> dict[str, object]:
    return {
        "schema": safe_tick.cycle.SCHEMA,
        "status": "silent",
        "execution_mode": "evaluate_only",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "operator_action_required": False,
        "interrupt_operator": False,
        "next_action": "the scheduler will reevaluate automatically",
        "source_evidence_posture": {"status": "waiting_for_fresh_sources"},
        "signal_approval": {
            "approved": True,
            "reason": "approved_projection_manifest_verified",
            "policy": safe_tick.approved.POLICY,
        },
    }


def _runtime_outage_cycle_report() -> dict[str, object]:
    report = _cycle_report()
    report.update(
        {
            "status": "action_required",
            "operator_action_required": True,
            "interrupt_operator": True,
            "next_action": (
                "rerun with --send only when factual operator delivery is "
                "authorized"
            ),
            "actions": [
                {
                    "lane": "gold_live_runtime",
                    "reason": "live_runtime_tunnel_unavailable",
                    "safe_next_action": (
                        "inspect and stage the PropertyQuarry Cloudflare tunnel "
                        "connector recovery for review; start or restart the "
                        "connector only after explicit deployment/restart "
                        "authorization"
                    ),
                }
            ],
        }
    )
    return report


def _source_refresh_report(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": safe_tick.source_refresh.VERIFY_SCHEMA,
        "status": "verified",
        "request_state": "producer_refresh_staged",
        "request_id": "pq-source-refresh-test",
        "request_staged": True,
        "next_action": (
            "route the staged work item to each named receipt producer under that "
            "producer's independently governed authority; this request does not authorize "
            "provider access, delivery, deployment, or execution"
        ),
        "requested_lanes": [{"lane": "gold_live_runtime"}],
        "request_path": str(paths["source_refresh_request_path"]),
        "request_receipt_sha256": "b" * 64,
        "request_receipt_persisted": True,
        "verification_path": str(
            paths["source_refresh_verification_path"]
        ),
        "verification_receipt_sha256": "c" * 64,
        "verification_receipt_persisted": True,
        "progress": {"current_evidence_verified": True},
        "action_required": False,
        "interrupt_operator": False,
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


def _source_handoff_report(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": safe_tick.source_handoff.VERIFY_SCHEMA,
        "status": "verified",
        "handoff_state": "producer_pickup_available",
        "handoff_id": "pq-source-refresh-handoff-test",
        "handoff_available": True,
        "next_action": (
            "an independently governed producer may pick up each work item, "
            "refresh only its named artifacts, and let the next approved OODA "
            "cycle verify settlement; this handoff grants no authority"
        ),
        "work_items": [{"lane": "gold_live_runtime"}],
        "handoff_path": str(paths["source_refresh_handoff_path"]),
        "handoff_receipt_sha256": "d" * 64,
        "handoff_receipt_persisted": True,
        "verification_path": str(
            paths["source_refresh_handoff_verification_path"]
        ),
        "verification_receipt_sha256": "e" * 64,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "request_binding_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "handoff_confers_authority": False,
        "producer_claim_recorded": False,
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


def _source_claims_report(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": safe_tick.source_claims.VERIFY_SCHEMA,
        "status": "verified",
        "claim_state": "unclaimed",
        "settlement_state": "awaiting_current_evidence",
        "handoff_id": "pq-source-refresh-handoff-test",
        "producer_claim_recorded": False,
        "next_action": (
            "await an independently signed producer claim or refreshed source "
            "evidence; do not interrupt the operator or infer pickup"
        ),
        "claims": [],
        "claim_directory": {
            "required": False,
            "present": False,
        },
        "receipt_path": str(paths["source_refresh_claim_receipt_path"]),
        "lifecycle_receipt_sha256": "f" * 64,
        "lifecycle_receipt_persisted": True,
        "verification_path": str(
            paths["source_refresh_claim_verification_path"]
        ),
        "verification_receipt_sha256": "1" * 64,
        "verification_receipt_persisted": True,
        "progress": {
            "expected_claim_count": 1,
            "claim_count": 0,
            "current_evidence_verified": True,
            "handoff_binding_verified": True,
            "lifecycle_integrity_verified": True,
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


def _source_settlement_report(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": safe_tick.source_settlement.VERIFY_SCHEMA,
        "status": "verified",
        "settlement_state": "no_prior_work",
        "producer_completion_recorded": False,
        "settlement_attributed": False,
        "next_action": "continue monitoring",
        "settlements": [],
        "completion_directory": {"required": False, "present": False},
        "receipt_path": str(paths["source_refresh_settlement_receipt_path"]),
        "settlement_receipt_sha256": "2" * 64,
        "settlement_receipt_persisted": True,
        "verification_path": str(
            paths["source_refresh_settlement_verification_path"]
        ),
        "verification_receipt_sha256": "3" * 64,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "settlement_integrity_verified": True,
            "current_source_binding_verified": True,
            "completion_directory_binding_verified": True,
            "settled_count": 0,
            "expected_work_item_count": 0,
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


def _source_trust_intake_report(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": safe_tick.trust_intake.VERIFY_SCHEMA,
        "status": "verified",
        "request_status": "not_required",
        "intake_state": "not_required",
        "request_id": "pqtrustintake_test",
        "request_staged": False,
        "requested_lanes": [],
        "next_action": "continue verifying producer claims under current public-key trust",
        "candidate_requirements": {
            "algorithm": "Ed25519",
            "private_key_material_allowed": False,
            "out_of_band_identity_verification_required": True,
        },
        "receipt_path": str(paths["source_refresh_trust_intake_receipt_path"]),
        "intake_receipt_sha256": "4" * 64,
        "intake_receipt_persisted": True,
        "verification_path": str(
            paths["source_refresh_trust_intake_verification_path"]
        ),
        "verification_receipt_sha256": "5" * 64,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "claim_binding_verified": True,
            "trust_registry_binding_verified": True,
            "intake_integrity_verified": True,
            "candidate_evidence_count": 0,
        },
        "action_required": False,
        "interrupt_operator": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
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


def _staged_source_trust_request_report(
    paths: dict[str, Path],
) -> dict[str, object]:
    report = _source_trust_intake_report(paths)
    report.update(
        {
            "request_status": "staged",
            "intake_state": "awaiting_producer_public_key_evidence",
            "request_staged": True,
            "requested_lanes": ["scene_video_provider_refresh"],
            "next_action": (
                "obtain producer-owned Ed25519 public-key metadata for the "
                "stale scene-video lane"
            ),
        }
    )
    return report


def _source_trust_candidate_report(paths: dict[str, Path]) -> dict[str, object]:
    report = _source_trust_intake_report(paths)
    candidate = {
        "producer_id": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "public_key_sha256": "6" * 64,
        "lanes": ["gold_live_runtime"],
        "candidate_receipt_sha256": "7" * 64,
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "proof_of_possession_verified": True,
        "out_of_band_identity_verification_required": True,
        "candidate_confers_authority": False,
        "trust_enrollment_authorized": False,
    }
    report.update(
        {
            "request_status": "staged",
            "intake_state": "candidate_ready_for_operator_review",
            "request_staged": True,
            "requested_lanes": ["gold_live_runtime"],
            "candidate_review_id": "pqtrustreview_000000000000000000000000",
            "review_decision_options": [
                "confirm_identity_verified",
                "reject_candidate",
                "defer",
            ],
            "candidates": [candidate],
            "candidate_coverage": {
                "covered_lanes": ["gold_live_runtime"],
                "remaining_lanes": [],
                "all_requested_lanes_covered": True,
            },
            "next_action": "verify candidate identity out of band",
            "action_required": True,
            "interrupt_operator": True,
            "operator_review_required": True,
            "public_key_candidate_recorded": True,
        }
    )
    report["progress"] = {
        **dict(report["progress"]),
        "candidate_evidence_count": 1,
    }
    return report


@pytest.fixture(autouse=True)
def _verified_post_claim_components(monkeypatch) -> None:
    monkeypatch.setattr(
        safe_tick,
        "_load_operator_projection",
        _operator_projection_for_test,
    )
    monkeypatch.setattr(
        safe_tick,
        "_materialize_source_trust_notification_projection",
        _trust_notification_projection_for_test,
    )
    monkeypatch.setattr(
        safe_tick.source_settlement,
        "materialize_current_settlement_bundle",
        lambda **kwargs: _source_settlement_report(
            {
                "source_refresh_settlement_receipt_path": kwargs["receipt_path"],
                "source_refresh_settlement_verification_path": kwargs[
                    "verification_path"
                ],
            }
        ),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **kwargs: _source_trust_intake_report(
            {
                "source_refresh_trust_intake_receipt_path": kwargs[
                    "receipt_path"
                ],
                "source_refresh_trust_intake_verification_path": kwargs[
                    "verification_path"
                ],
            }
        ),
    )


def test_safe_tick_serializes_stage_and_evaluate_only_cycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    paths.pop("refresh_public_origin_observation")
    calls: dict[str, dict[str, object]] = {}

    def observe(**kwargs) -> dict[str, object]:
        calls["observation"] = kwargs
        return {
            "status": "blocked",
            "action_required": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }

    def stage_once(**kwargs) -> dict[str, object]:
        calls["stage"] = kwargs
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**kwargs) -> dict[str, object]:
        calls["cycle"] = kwargs
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.public_origin,
        "write_public_origin_observation",
        observe,
    )
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )

    result = safe_tick.run_safe_tick(
        **paths,
        now=NOW,
    )

    assert result["status"] == "ready"
    assert result["updated_at"] == NOW.isoformat()
    assert result["receipt_persisted"] is True
    assert result["next_action"] == (
        "await an independently signed producer claim or refreshed source "
        "evidence; do not interrupt the operator or infer pickup"
    )
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": True,
        "operator_presentation_verified": True,
            "runtime_review_verified": True,
            "runtime_control_verified": True,
        "source_refresh_request_verified": True,
        "source_refresh_handoff_verified": True,
        "source_refresh_claims_verified": True,
        "source_refresh_trust_intake_verified": True,
        "source_refresh_trust_decision_verified": True,
        "source_refresh_trust_notification_verified": True,
        "source_refresh_settlement_verified": True,
        "source_refresh_request_staged": True,
        "current_evidence_verified": True,
    }
    assert calls["stage"]["now"] == NOW
    assert calls["stage"]["public_origin_observation_path"] == paths[
        "public_origin_observation_path"
    ].resolve()
    assert calls["observation"] == {
        "receipt_path": paths["public_origin_observation_path"].resolve(),
        "origin": "https://propertyquarry.com",
        "now": NOW,
    }
    assert calls["cycle"]["now"] == NOW
    assert calls["cycle"]["send"] is False
    assert calls["cycle"]["require_approval_manifest"] is True
    assert result["automatic_execution_allowed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    assert result["components"]["source_refresh_request"] == {
        "status": "producer_refresh_staged",
        "request_id": "pq-source-refresh-test",
        "requested_lane_count": 1,
        "request_path": str(paths["source_refresh_request_path"]),
        "request_receipt_sha256": "b" * 64,
        "verification_path": str(paths["source_refresh_verification_path"]),
        "verification_receipt_sha256": "c" * 64,
        "current_evidence_verified": True,
        "producer_dispatch_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }
    assert result["components"]["runtime_recovery_review"] == {
        "schema": safe_tick.RUNTIME_REVIEW_COMPONENT_SCHEMA,
        "status": "not_required",
        "updated_at": NOW.isoformat(),
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
    assert result["components"]["source_refresh_handoff"] == {
        "status": "producer_pickup_available",
        "handoff_id": "pq-source-refresh-handoff-test",
        "work_item_count": 1,
        "handoff_path": str(paths["source_refresh_handoff_path"]),
        "handoff_receipt_sha256": "d" * 64,
        "verification_path": str(
            paths["source_refresh_handoff_verification_path"]
        ),
        "verification_receipt_sha256": "e" * 64,
        "current_evidence_verified": True,
        "handoff_confers_authority": False,
        "producer_claim_recorded": False,
        "producer_dispatch_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }
    assert result["components"]["source_refresh_claims"] == {
        "status": "unclaimed",
        "settlement_state": "awaiting_current_evidence",
        "handoff_id": "pq-source-refresh-handoff-test",
        "claim_count": 0,
        "expected_claim_count": 1,
        "claims": [],
        "receipt_path": str(paths["source_refresh_claim_receipt_path"]),
        "lifecycle_receipt_sha256": "f" * 64,
        "verification_path": str(
            paths["source_refresh_claim_verification_path"]
        ),
        "verification_receipt_sha256": "1" * 64,
        "current_evidence_verified": True,
        "claim_confers_authority": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }
    assert result["components"]["source_refresh_trust_intake"] == {
        "status": "not_required",
        "request_id": "pqtrustintake_test",
        "request_staged": False,
        "requested_lanes": [],
        "candidate_evidence_count": 0,
        "candidate_review_id": "",
        "review_decision_options": [],
        "candidates": [],
        "candidate_coverage": {},
        "receipt_path": str(paths["source_refresh_trust_intake_receipt_path"]),
        "intake_receipt_sha256": "4" * 64,
        "verification_path": str(
            paths["source_refresh_trust_intake_verification_path"]
        ),
        "verification_receipt_sha256": "5" * 64,
        "current_evidence_verified": True,
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "review_decision": {
            "schema": safe_tick.trust_decision.VERIFY_SCHEMA,
            "status": "not_required",
            "review_state": "not_required",
            "updated_at": NOW.isoformat(),
            "blocking_reason": "",
            "next_action": "await a verified producer public-key candidate",
            "candidate_review_id": "",
            "decision_id": "",
            "decision": "",
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
        },
        "trust_enrollment_preview_authorized": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "producer_dispatch_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }
    assert result["components"]["source_refresh_trust_decision"] == {
        "status": "not_required",
        "review_state": "not_required",
        "candidate_review_id": "",
        "decision_id": "",
        "decision": "",
        "decision_path": "",
        "current_evidence_verified": True,
        "action_required": False,
        "interrupt_operator": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }
    assert stat.S_IMODE(paths["tick_receipt_path"].stat().st_mode) == 0o600
    assert json.loads(paths["tick_receipt_path"].read_text(encoding="utf-8")) == result


def test_safe_tick_default_origin_refresh_failure_blocks_before_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    paths.pop("refresh_public_origin_observation")
    monkeypatch.setattr(
        safe_tick.public_origin,
        "write_public_origin_observation",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("origin unavailable")),
    )
    monkeypatch.setattr(
        safe_tick.stage,
        "run_stage_iteration",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a failed current observation must stop before staging")
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_public_origin_observation_failed"
    )
    assert result["error_type"] == "OSError"
    assert result["progress"]["signal_stage_completed"] is False
    assert result["progress"]["current_evidence_verified"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["receipt_persisted"] is True


@pytest.mark.parametrize(
    ("argv", "refresh_expected"),
    [
        ([], True),
        (["--no-refresh-public-origin-observation"], False),
    ],
)
def test_safe_tick_cli_origin_refresh_default_and_explicit_opt_out(
    argv: list[str],
    refresh_expected: bool,
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}

    def run_once(**kwargs) -> dict[str, object]:
        observed.update(kwargs)
        return {"status": "ready"}

    monkeypatch.setattr(safe_tick, "run_safe_tick", run_once)

    assert safe_tick.main(argv) == 0
    assert observed["refresh_public_origin_observation"] is refresh_expected
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_safe_tick_projects_verified_candidate_as_review_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: _source_trust_candidate_report(paths),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "ready"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["next_action"] == "verify candidate identity out of band"
    component = result["components"]["source_refresh_trust_intake"]
    assert component["status"] == "candidate_ready_for_operator_review"
    assert component["candidate_evidence_count"] == 1
    assert component["action_required"] is True
    assert component["interrupt_operator"] is True
    assert component["operator_review_required"] is True
    decision_component = result["components"]["source_refresh_trust_decision"]
    assert decision_component["status"] == "pending"
    assert decision_component["review_state"] == "candidate_review_pending"
    assert decision_component["action_required"] is True
    assert decision_component["interrupt_operator"] is False
    assert decision_component["trust_enrollment_preview_authorized"] is False
    notification_component = result["components"][
        "source_refresh_trust_notification"
    ]
    assert notification_component["status"] == "verified"
    assert notification_component["notification_status"] == "action_required"
    assert notification_component["action_required"] is True
    assert notification_component["interrupt_operator"] is True
    assert notification_component["current_evidence_verified"] is True
    assert notification_component["delivery_authorized"] is False
    assert notification_component["delivery_attempted"] is False
    assert notification_component["sent"] is False
    assert component["trust_enrollment_authorized"] is False
    assert component["trust_registry_modified"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["protected_operation_executed"] is False

    def retained_notification(**kwargs) -> dict[str, object]:
        report = _trust_notification_projection_for_test(**kwargs)
        report.update(
            {
                "notification_status": "deduplicated",
                "next_action": (
                    "await an immutable candidate decision or a new "
                    "verified review"
                ),
                "presentation_status": "already_presented",
                "interrupt_operator": False,
            }
        )
        return report

    monkeypatch.setattr(
        safe_tick,
        "_materialize_source_trust_notification_projection",
        retained_notification,
    )
    repeated = safe_tick.run_safe_tick(**paths, now=NOW + timedelta(seconds=1))

    assert repeated["status"] == "ready"
    assert repeated["action_required"] is True
    assert repeated["interrupt_operator"] is False
    assert repeated["next_action"] == (
        "await an immutable candidate decision or a new verified review"
    )
    repeated_intake = repeated["components"]["source_refresh_trust_intake"]
    assert repeated_intake["action_required"] is True
    assert repeated_intake["interrupt_operator"] is False
    repeated_notification = repeated["components"][
        "source_refresh_trust_notification"
    ]
    assert repeated_notification["notification_status"] == "deduplicated"
    assert repeated_notification["action_required"] is True
    assert repeated_notification["interrupt_operator"] is False
    assert repeated_notification["delivery_authorized"] is False
    assert repeated_notification["sent"] is False


def test_safe_tick_fails_closed_on_invalid_trust_presentation_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    _write_private(
        paths["source_refresh_trust_presentation_state_path"],
        {"schema": "forged-presentation-state"},
    )

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: _source_trust_candidate_report(paths),
    )
    monkeypatch.setattr(
        safe_tick,
        "_materialize_source_trust_notification_projection",
        REAL_TRUST_NOTIFICATION_MATERIALIZER,
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_source_refresh_trust_notification_failed"
    )
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["automatic_execution_allowed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    notification_receipt = json.loads(
        paths["source_refresh_trust_notification_path"].read_text(
            encoding="utf-8"
        )
    )
    assert notification_receipt["status"] == "blocked"
    assert notification_receipt["interrupt_operator"] is False
    assert notification_receipt["delivery_authorized"] is False
    assert notification_receipt["sent"] is False
    assert notification_receipt["trust_enrollment_authorized"] is False
    assert notification_receipt["trust_registry_modified"] is False


def test_safe_tick_keeps_current_runtime_action_ahead_of_source_maintenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    ordering: list[str] = []

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _runtime_outage_cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    def stage_runtime_review(**_kwargs) -> dict[str, object]:
        ordering.append("runtime_review")
        return _runtime_review_projection_for_test(paths)

    def load_operator_projection(**kwargs) -> dict[str, object]:
        ordering.append("operator_projection")
        return _operator_projection_for_test(**kwargs)

    def stage_runtime_control(**_kwargs) -> dict[str, object]:
        ordering.append("runtime_control")
        return _runtime_control_report_for_test()

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: _staged_source_trust_request_report(paths),
    )
    monkeypatch.setattr(
        safe_tick,
        "_materialize_runtime_review_projection",
        stage_runtime_review,
    )
    monkeypatch.setattr(
        safe_tick,
        "_load_operator_projection",
        load_operator_projection,
    )
    monkeypatch.setattr(
        safe_tick.runtime_control,
        "stage_current_runtime_control",
        stage_runtime_control,
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "ready"
    assert ordering == [
        "runtime_review",
        "operator_projection",
        "runtime_control",
    ]
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["next_action"].startswith("reply exactly 'Authorize pqar_")
    runtime_component = result["components"]["runtime_recovery_review"]
    assert runtime_component["status"] == "verified"
    assert runtime_component["review_packet_staged"] is True
    assert runtime_component["action_required"] is False
    assert runtime_component["interrupt_operator"] is False
    assert runtime_component["authorization_recorded"] is False
    assert runtime_component["deployment_or_restart_authorized"] is False
    assert result["components"]["source_refresh_trust_intake"][
        "request_staged"
    ] is True
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["protected_operation_executed"] is False


def test_runtime_review_projection_stages_and_verifies_without_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    calls: dict[str, dict[str, object]] = {}
    order: list[str] = []
    packet = {
        "schema": safe_tick.runtime_review.SCHEMA,
        "generated_at": NOW.isoformat(),
        "status": "review_ready",
    }

    def materialize(**kwargs) -> dict[str, object]:
        order.append("review")
        calls["materialize"] = kwargs
        _write_private(kwargs["write_path"], packet)
        return packet

    def verify(**kwargs) -> dict[str, object]:
        calls["verify"] = kwargs
        packet_sha256 = safe_tick.runtime_review._sha256(
            kwargs["packet_path"].read_bytes()
        )
        return {
            "schema": safe_tick.runtime_review.VERIFY_SCHEMA,
            "status": "verified",
            "updated_at": NOW.isoformat(),
            "blocking_reason": "live_runtime_tunnel_unavailable",
            "next_action": "review exact recovery and record authority separately",
            "progress": {"current_evidence_verified": True},
            "configuration_proposal": {
                "authorization_required": True,
                "execution_authorized": False,
                "recovery_preview": {
                    "preview_only": True,
                    "command_recorded": True,
                    "authorization_recorded": False,
                    "execution_authorized": False,
                },
            },
            "packet_sha256": packet_sha256,
            "execution_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }

    monkeypatch.setattr(
        safe_tick.runtime_review,
        "materialize_local_runtime_binding_candidate",
        lambda **_kwargs: order.append("local_runtime_candidate") or {
            "status": "ready_for_merge_review",
            "configuration_merge_authorized": False,
            "configuration_merged": False,
            "deployment_or_restart_authorized": False,
            "secret_values_recorded": False,
        },
    )
    monkeypatch.setattr(
        safe_tick.runtime_review,
        "materialize_current_review_packet",
        materialize,
    )
    monkeypatch.setattr(
        safe_tick.runtime_review,
        "verify_current_review_packet",
        verify,
    )
    presentation_context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {"change_id": "test-runtime-recovery"},
    }
    monkeypatch.setattr(
        safe_tick.runtime_review,
        "operator_presentation_context",
        lambda _verification: dict(presentation_context),
    )
    projection = safe_tick._materialize_runtime_review_projection(
        action_source={
            "pending_actions": [
                {
                    "lane": "gold_live_runtime",
                    "reason": "live_runtime_tunnel_unavailable",
                }
            ]
        },
        packet_path=paths["runtime_review_packet_path"],
        verification_path=paths["runtime_review_verification_path"],
        cycle_receipt_path=paths["cycle_receipt_path"],
        signal_dir=paths["signal_dir"],
        public_origin_observation_path=paths[
            "public_origin_observation_path"
        ],
        release_manifest_path=paths[
            "runtime_review_release_manifest_path"
        ],
        deployment_env_path=paths[
            "runtime_review_deployment_env_path"
        ],
        project="property",
        now=NOW,
    )

    assert projection["status"] == "verified"
    assert projection["review_packet_staged"] is True
    assert projection["current_evidence_verified"] is True
    assert projection["action_required"] is False
    assert projection["interrupt_operator"] is False
    assert projection["authorization_required"] is True
    assert projection["authorization_recorded"] is False
    assert projection["execution_authorized"] is False
    assert projection["deployment_or_restart_authorized"] is False
    assert projection["provider_quota_consumption_allowed"] is False
    assert projection["delivery_authorized"] is False
    assert projection["presentation_context"] == presentation_context
    assert order == ["local_runtime_candidate", "review"]
    assert calls["materialize"]["now"] == NOW
    assert calls["materialize"]["live_mobile_receipt_path"] == paths[
        "public_origin_observation_path"
    ].resolve()
    assert calls["verify"]["now"] == NOW
    assert stat.S_IMODE(
        paths["runtime_review_packet_path"].stat().st_mode
    ) == 0o600
    assert stat.S_IMODE(
        paths["runtime_review_verification_path"].stat().st_mode
    ) == 0o600


def test_runtime_review_context_is_hydrated_before_novelty_comparison(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    source_digest = "a" * 64
    action = {
        "lane": "gold_live_runtime",
        "reason": "live_runtime_tunnel_unavailable",
        "source_generated_at": NOW.isoformat(),
        "safe_next_action": "review tunnel recovery",
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
        "provider_quota_consumption_allowed": False,
    }

    def raw_status() -> dict[str, object]:
        return {
            "schema": safe_tick.operator_status.SCHEMA,
            "status": "action_required",
            "action_required": True,
            "interrupt_operator": True,
            "updated_at": NOW.isoformat(),
            "blocking_reason": "live_runtime_tunnel_unavailable",
            "next_action": "review tunnel recovery",
            "actions": [dict(action)],
            "progress": {"current_snapshot_hashes_verified": True},
            "source_cycle_receipt_sha256": source_digest,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operation_executed": False,
        }

    baseline = safe_tick.operator_status.apply_operator_presentation_state(
        raw_status(),
        state_path=paths["operator_presentation_state_path"],
        now=NOW,
    )
    baseline_receipt = safe_tick.operator_status.record_operator_presentation(
        baseline,
        state_path=paths["operator_presentation_state_path"],
        expected_source_cycle_receipt_sha256=source_digest,
        now=NOW,
    )
    assert baseline_receipt["status"] == "recorded"
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {"change_id": "propertyquarry_edge_connector_recovery"},
    }
    monkeypatch.setattr(
        safe_tick.operator_status,
        "load_operator_status",
        lambda **_kwargs: raw_status(),
    )

    projected = REAL_LOAD_OPERATOR_PROJECTION(
        cycle_receipt_path=paths["cycle_receipt_path"],
        signal_dir=paths["signal_dir"],
        presentation_state_path=paths["operator_presentation_state_path"],
        cycle_evidence={"sha256": source_digest},
        runtime_review_report={
            "status": "verified",
            "presentation_context": context,
        },
        now=NOW,
    )

    assert projected["status"] == "pending_action"
    assert projected["action_required"] is True
    assert projected["interrupt_operator"] is False
    assert projected["actions"] == []
    assert projected["presentation"]["context_hydration_state_updated"] is True
    assert projected["presentation"]["context_hydrated_lanes"] == [
        "gold_live_runtime"
    ]
    persisted = json.loads(
        paths["operator_presentation_state_path"].read_text(encoding="utf-8")
    )
    identity = persisted["active_presentations"]["gold_live_runtime"]
    assert identity["presentation_context_digest"] == (
        safe_tick.operator_status._presentation_context_digest(context)
    )


def test_safe_tick_fails_closed_when_required_runtime_review_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _runtime_outage_cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick,
        "_materialize_runtime_review_projection",
        lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("current_runtime_review_not_admissible")
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_runtime_review_failed"
    assert result["next_action"] == (
        "repair the current evaluate-only runtime review receipt binding "
        "before requesting deployment or restart authority"
    )
    assert result["progress"]["runtime_review_verified"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["protected_operation_executed"] is False


def test_runtime_novelty_delegation_preserves_another_lane_interrupt() -> None:
    runtime_action = {
        "lane": "gold_live_runtime",
        "reason": "live_runtime_tunnel_unavailable",
        "safe_next_action": "review the exact runtime authorization",
    }
    provider_action = {
        "lane": "scene_video_provider_refresh",
        "reason": "provider_account_material_required",
        "safe_next_action": "review the provider account material",
    }
    projected = safe_tick._delegate_runtime_operator_novelty(
        {
            "status": "action_required",
            "action_required": True,
            "interrupt_operator": True,
            "blocking_reason": (
                "live_runtime_tunnel_unavailable,"
                "provider_account_material_required"
            ),
            "next_action": "review each listed action",
            "actions": [runtime_action, provider_action],
            "pending_actions": [runtime_action, provider_action],
            "presentation": {
                "pending_action_count": 2,
                "novel_action_count": 2,
            },
        },
        runtime_review_report={
            "status": "verified",
            "presentation_context": {
                "schema": (
                    "propertyquarry.ooda_operator_presentation_context.v1"
                ),
                "lane": "gold_live_runtime",
                "scope": {"change_id": "runtime-recovery"},
            },
        },
        runtime_control_report={
            "status": "verified",
            "current_evidence_verified": True,
            "action_required": True,
            "interrupt_operator": False,
            "blocking_reason": "live_runtime_tunnel_unavailable",
            "next_action": "await the current runtime decision",
        },
    )

    assert projected["status"] == "action_required"
    assert projected["interrupt_operator"] is True
    assert projected["actions"] == [provider_action]
    assert projected["next_action"] == "review the provider account material"
    assert projected["presentation"]["novel_action_count"] == 1
    assert projected["runtime_control_delegation"][
        "source_novel_action_count"
    ] == 1


def test_safe_tick_suppresses_a_retained_runtime_action_at_top_level(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _runtime_outage_cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick,
        "_load_operator_projection",
        _operator_projection_for_test,
    )
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: _staged_source_trust_request_report(paths),
    )
    monkeypatch.setattr(
        safe_tick,
        "_materialize_runtime_review_projection",
        lambda **_kwargs: _runtime_review_projection_for_test(paths),
    )
    monkeypatch.setattr(
        safe_tick.runtime_control,
        "stage_current_runtime_control",
        lambda **_kwargs: _runtime_control_report_for_test(
            interrupt_operator=False,
            next_action=(
                "await an explicit operator decision or a verified action change"
            ),
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "ready"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["next_action"] == (
        "await an explicit operator decision or a verified action change"
    )
    presentation = result["components"]["operator_presentation"]
    assert presentation["status"] == "pending_action"
    assert presentation["action_required"] is True
    assert presentation["interrupt_operator"] is False
    assert presentation["source_interrupt_operator"] is True
    assert presentation["pending_action_count"] == 1
    assert presentation["novel_action_count"] == 0
    assert presentation["source_novel_action_count"] == 1
    assert presentation["runtime_control_delegation"] == {
        "active": True,
        "lane": "gold_live_runtime",
        "pending_action_count": 1,
        "source_novel_action_count": 1,
        "effective_novel_action_count": 0,
        "runtime_control_interrupt_operator": False,
        "current_evidence_verified": True,
    }
    assert presentation["current_evidence_verified"] is True
    assert presentation["delivery_state_updated"] is False
    assert presentation["provider_quota_consumed"] is False
    assert presentation["protected_operation_executed"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["protected_operation_executed"] is False

    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: _source_trust_candidate_report(paths),
    )
    novel_trust_review = safe_tick.run_safe_tick(
        **paths,
        now=NOW + timedelta(seconds=1),
    )

    assert novel_trust_review["status"] == "ready"
    assert novel_trust_review["action_required"] is True
    assert novel_trust_review["interrupt_operator"] is True
    assert novel_trust_review["next_action"] == (
        "verify candidate identity out of band"
    )
    assert novel_trust_review["components"]["operator_presentation"][
        "status"
    ] == "pending_action"
    assert novel_trust_review["components"]["operator_presentation"][
        "interrupt_operator"
    ] is False
    assert novel_trust_review["components"][
        "source_refresh_trust_notification"
    ]["notification_status"] == "action_required"
    assert novel_trust_review["components"][
        "source_refresh_trust_notification"
    ]["interrupt_operator"] is True
    assert novel_trust_review["deployment_or_restart_authorized"] is False
    assert novel_trust_review["provider_quota_consumption_allowed"] is False
    assert novel_trust_review["delivery_authorized"] is False
    assert novel_trust_review["protected_operation_executed"] is False


def test_safe_tick_resolves_review_without_enrolling_trust(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    candidate_report = _source_trust_candidate_report(paths)
    receipt = safe_tick.trust_decision.build_candidate_review_decision(
        candidate_report,
        decision="confirm_identity_verified",
        decider_id="operator.local",
        acknowledged_public_key_sha256s=["6" * 64],
        identity_verification_method="trusted_channel",
        identity_evidence_ref="ticket:IDENTITY-123",
        now=NOW,
    )
    verified_decision = (
        safe_tick.trust_decision.verify_candidate_review_decision(
            receipt,
            report=candidate_report,
            now=NOW,
        )
    )
    verified_decision["progress"]["current_evidence_verified"] = True

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: candidate_report,
    )
    monkeypatch.setattr(
        safe_tick.trust_decision,
        "verify_candidate_review_decision_for_report",
        lambda *_args, **_kwargs: verified_decision,
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "ready"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["next_action"] == (
        "stage an exact trust-registry enrollment preview for separate "
        "operator authorization; do not modify the registry or enroll trust"
    )
    intake = result["components"]["source_refresh_trust_intake"]
    decision_component = result["components"][
        "source_refresh_trust_decision"
    ]
    assert intake["status"] == "candidate_identity_verified_preview_required"
    assert intake["operator_review_required"] is False
    assert intake["trust_enrollment_preview_authorized"] is True
    assert intake["trust_enrollment_authorized"] is False
    assert intake["trust_registry_modified"] is False
    assert decision_component["status"] == "verified"
    assert decision_component["identity_verification_asserted"] is True
    assert decision_component["trust_enrollment_preview_authorized"] is True
    assert decision_component["trust_enrollment_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["protected_operation_executed"] is False


def test_safe_tick_fails_closed_before_evaluation_when_staging_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        safe_tick.stage,
        "run_stage_iteration",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("private detail")),
    )
    monkeypatch.setattr(
        safe_tick.cycle,
        "run_cycle_once",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a failed stage must not be evaluated")
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_signal_stage_failed"
    assert result["error_type"] == "ValueError"
    assert "private detail" not in json.dumps(result)
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["progress"]["current_evidence_verified"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["receipt_persisted"] is True


def test_safe_tick_preserves_exact_quiet_notification_state_incident(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def incident_cycle(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        report.update(
            {
                "status": "notification_state_invalid",
                "notification_state_status": "invalid",
                "notification_state_sha256": "c" * 64,
                "notification_state_admissible": False,
                "would_send": False,
                "notification_count": 0,
                "message_ids": [],
                "delivery_mode": "",
                "state_updated": False,
                "action_required_count": 0,
                "novel_action_count": 0,
                "active_action_count_before": 0,
                "active_action_count_projected": 0,
                "active_action_count_after": 0,
                "actions": [],
                "next_action": (
                    "repair or explicitly replace the private notification incident ledger "
                    "before another send-enabled cycle"
                ),
            }
        )
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", incident_cycle)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an incident tick must stop before source-refresh staging")
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_notification_state_not_admissible"
    )
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["next_action"] == (
        "repair or explicitly replace the private notification incident ledger before "
        "another send-enabled cycle"
    )
    assert result["notification_state"] == {
        "status": "invalid",
        "sha256": "c" * 64,
        "admissible": False,
        "state_updated": False,
    }
    assert result["progress"]["signal_stage_completed"] is True
    assert result["progress"]["evaluate_only_cycle_completed"] is True
    assert result["progress"]["current_evidence_verified"] is False
    assert result["components"]["signal_stage"]["current_evidence_verified"] is True
    assert result["components"]["evaluate_only_cycle"]["current_evidence_verified"] is True
    assert result["delivery_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["protected_operation_executed"] is False
    assert result["receipt_persisted"] is True


def test_safe_tick_rejects_any_cycle_that_claims_delivery_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def unsafe_cycle(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        report["delivery_authorized"] = True
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", unsafe_cycle)

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_evaluate_only_cycle_failed"
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": False,
            "runtime_review_verified": False,
            "runtime_control_verified": False,
        "source_refresh_request_verified": False,
        "source_refresh_handoff_verified": False,
        "source_refresh_claims_verified": False,
        "source_refresh_trust_intake_verified": False,
        "source_refresh_trust_decision_verified": False,
        "source_refresh_trust_notification_verified": False,
        "source_refresh_settlement_verified": False,
        "current_evidence_verified": False,
    }
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["delivery_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False


def test_safe_tick_busy_lock_does_not_run_or_overwrite_a_competing_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    descriptor = safe_tick._acquire_tick_lock(paths["tick_lock_path"])
    assert descriptor is not None
    monkeypatch.setattr(
        safe_tick.stage,
        "run_stage_iteration",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a competing tick must not stage")
        ),
    )

    try:
        result = safe_tick.run_safe_tick(**paths, now=NOW)
    finally:
        safe_tick._release_tick_lock(descriptor)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_already_running"
    assert result.get("receipt_persisted") is None
    assert not paths["tick_receipt_path"].exists()
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False


def test_safe_tick_blocks_when_reversible_refresh_request_cannot_be_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "schema": safe_tick.source_refresh.VERIFY_SCHEMA,
            "status": "blocked",
            "request_state": "blocked",
            "action_required": False,
            "interrupt_operator": False,
        },
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_source_refresh_request_failed"
    )
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": True,
            "runtime_review_verified": True,
            "runtime_control_verified": False,
        "source_refresh_request_verified": False,
        "source_refresh_handoff_verified": False,
        "source_refresh_claims_verified": False,
        "source_refresh_trust_intake_verified": False,
        "source_refresh_trust_decision_verified": False,
        "source_refresh_trust_notification_verified": False,
        "source_refresh_settlement_verified": False,
        "current_evidence_verified": False,
    }
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_preserves_prior_chain_when_settlement_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_settlement,
        "materialize_current_settlement_bundle",
        lambda **_kwargs: {
            "schema": safe_tick.source_settlement.VERIFY_SCHEMA,
            "status": "blocked",
            "settlement_state": "blocked",
            "action_required": False,
            "interrupt_operator": False,
            "completion_confers_authority": False,
        },
    )
    must_not_replace = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("blocked settlement must preserve the prior chain")
    )
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        must_not_replace,
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        must_not_replace,
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        must_not_replace,
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_source_refresh_settlement_failed"
    )
    assert result["progress"]["source_refresh_settlement_verified"] is False
    assert result["progress"]["current_evidence_verified"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_blocks_when_producer_handoff_cannot_be_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "schema": safe_tick.source_handoff.VERIFY_SCHEMA,
            "status": "blocked",
            "handoff_state": "blocked",
            "action_required": False,
            "interrupt_operator": False,
            "handoff_confers_authority": False,
        },
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_source_refresh_handoff_failed"
    )
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": True,
            "runtime_review_verified": True,
            "runtime_control_verified": False,
        "source_refresh_request_verified": False,
        "source_refresh_handoff_verified": False,
        "source_refresh_claims_verified": False,
        "source_refresh_trust_intake_verified": False,
        "source_refresh_trust_decision_verified": False,
        "source_refresh_trust_notification_verified": False,
        "source_refresh_settlement_verified": False,
        "current_evidence_verified": False,
    }
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_blocks_when_signed_claim_lifecycle_cannot_be_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    observed: dict[str, object] = {}

    def blocked_claims(**kwargs) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "schema": safe_tick.source_claims.VERIFY_SCHEMA,
            "status": "blocked",
            "claim_state": "blocked",
            "action_required": False,
            "interrupt_operator": False,
            "claim_confers_authority": False,
        }

    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        blocked_claims,
    )

    result = safe_tick.run_safe_tick(
        **paths,
        require_source_refresh_claim_dir=True,
        now=NOW,
    )

    assert observed["require_claim_dir"] is True
    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_source_refresh_claims_failed"
    assert result["progress"]["source_refresh_claims_verified"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_blocks_when_trust_intake_cannot_be_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: {
            "schema": safe_tick.trust_intake.VERIFY_SCHEMA,
            "status": "blocked",
            "intake_state": "blocked",
            "action_required": False,
            "interrupt_operator": False,
            "trust_registry_modified": False,
        },
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "safe_tick_source_refresh_trust_intake_failed"
    )
    assert result["progress"]["source_refresh_trust_intake_verified"] is False
    assert result["progress"]["current_evidence_verified"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_receipt_failure_clears_current_authority_and_action_signals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)

    def stage_once(**_kwargs) -> dict[str, object]:
        report = _stage_report()
        _write_private(paths["stage_receipt_path"], report)
        return report

    def cycle_once(**_kwargs) -> dict[str, object]:
        report = _cycle_report()
        report["operator_action_required"] = True
        report["interrupt_operator"] = True
        _write_private(paths["cycle_receipt_path"], report)
        return report

    monkeypatch.setattr(safe_tick.stage, "run_stage_iteration", stage_once)
    monkeypatch.setattr(safe_tick.cycle, "run_cycle_once", cycle_once)
    monkeypatch.setattr(
        safe_tick.source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: _source_refresh_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: _source_handoff_report(paths),
    )
    monkeypatch.setattr(
        safe_tick.source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: _source_claims_report(paths),
    )
    monkeypatch.setattr(
        safe_tick,
        "atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("tick receipt directory unavailable")
        ),
    )

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "safe_tick_receipt_persistence_failed"
    assert result["receipt_persisted"] is False
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": True,
        "operator_presentation_verified": True,
            "runtime_review_verified": True,
            "runtime_control_verified": True,
        "source_refresh_request_verified": True,
        "source_refresh_handoff_verified": True,
        "source_refresh_claims_verified": True,
        "source_refresh_trust_intake_verified": True,
        "source_refresh_trust_decision_verified": True,
        "source_refresh_trust_notification_verified": True,
        "source_refresh_settlement_verified": True,
        "source_refresh_request_staged": True,
        "current_evidence_verified": False,
    }
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["automatic_execution_allowed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_safe_tick_publishes_current_silent_revocation_for_missing_producer(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths["gold_receipt_path"].unlink()

    result = safe_tick.run_safe_tick(**paths, now=NOW)

    assert result["status"] == "ready"
    assert result["updated_at"] == NOW.isoformat()
    assert result["receipt_persisted"] is True
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["progress"] == {
        "signal_stage_completed": True,
        "evaluate_only_cycle_completed": True,
        "operator_presentation_verified": True,
            "runtime_review_verified": True,
            "runtime_control_verified": True,
        "source_refresh_request_verified": True,
        "source_refresh_handoff_verified": True,
        "source_refresh_claims_verified": True,
        "source_refresh_trust_intake_verified": True,
        "source_refresh_trust_decision_verified": True,
        "source_refresh_trust_notification_verified": True,
        "source_refresh_settlement_verified": True,
        "source_refresh_request_staged": True,
        "current_evidence_verified": True,
    }
    assert result["source_evidence_posture"]["status"] == (
        "waiting_for_fresh_sources"
    )
    assert result["source_evidence_posture"]["progress"] == {
        "expected_lane_count": 2,
        "current_lane_count": 0,
        "stale_lane_count": 0,
        "unavailable_lane_count": 2,
    }
    stage_receipt = json.loads(
        paths["stage_receipt_path"].read_text(encoding="utf-8")
    )
    assert stage_receipt["status"] == "ready"
    assert stage_receipt["publication_status"] == "revoked"
    assert stage_receipt["receipt_persisted"] is True
    cycle_receipt = json.loads(
        paths["cycle_receipt_path"].read_text(encoding="utf-8")
    )
    assert cycle_receipt["status"] == "silent"
    assert cycle_receipt["publication_status"] == "revoked"
    assert cycle_receipt["signal_approval"]["approved"] is False
    assert cycle_receipt["signal_approval"]["revocation_verified"] is True
    assert cycle_receipt["delivery_authorized"] is False
    assert cycle_receipt["delivery_attempted"] is False
    assert cycle_receipt["sent"] is False
    assert result["automatic_execution_allowed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
