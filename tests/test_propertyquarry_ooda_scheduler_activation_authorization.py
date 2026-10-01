from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_operator_status as presentation
from scripts import propertyquarry_ooda_scheduler_activation_authorization as authorization
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness


NOW = datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc)


def _readiness(
    *,
    state: str = "ready_for_authorization",
    now: datetime = NOW,
    receipt_digest: str = "a" * 64,
    render_digit: str = "3",
) -> dict[str, object]:
    ready = state == "ready_for_authorization"
    not_required = state == "not_required"
    preflight_status = "ready" if ready else (
        "not_run" if not_required else "blocked"
    )
    blocking_reason = (
        "explicit_scheduler_activation_authorization_required"
        if ready
        else (
            "scheduler_continuity_active"
            if not_required
            else "release_worktree_not_clean"
        )
    )
    return {
        "schema": readiness.VERIFY_SCHEMA,
        "status": "verified",
        "readiness_state": state,
        "updated_at": now.isoformat(),
        "readiness_observed_at": now.isoformat(),
        "readiness_receipt_sha256": receipt_digest,
        "blocking_reason": blocking_reason,
        "next_action": "test next action",
        "scope": {
            **authorization._EXPECTED_SCOPE,
            "compose_project": "property",
        },
        "source_evidence": {
            "activation_preflight": {
                "status": preflight_status,
                "runtime_commit_sha": "f" * 40 if ready else "",
                "envelope_commit_sha": "1" * 40 if ready else "",
                "web_image_digest": (
                    "sha256:" + ("2" * 64) if ready else ""
                ),
                "render_image_digest": (
                    "sha256:" + (render_digit * 64) if ready else ""
                ),
                "build_performed": False,
                "deployment_or_restart_performed": False,
                "provider_quota_consumed": False,
                "delivery_attempted": False,
            }
        },
        "source_bindings": {
            "continuity_receipt_sha256": "b" * 64,
            "runtime_observation_sha256": "c" * 64,
            "preflight_observation_sha256": "d" * 64,
            "preflight_script_sha256": "e" * 64,
        },
        "action_required": ready,
        "interrupt_operator": False,
        "progress": {
            "activation_preflight_current": True,
            "activation_preflight_passed": ready,
            "current_evidence_verified": True,
            "receipt_integrity_verified": True,
        },
        "authorization_required": ready,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "verification_receipt_persisted": True,
    }


def test_blocked_or_active_readiness_suppresses_request_staging(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"

    blocked = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(state="blocked"),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )
    active = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(state="not_required"),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert blocked["status"] == "ready"
    assert blocked["handoff_state"] == "authorization_not_ready"
    assert blocked["action_required"] is False
    assert blocked["interrupt_operator"] is False
    assert blocked["actions"] == []
    assert active["handoff_state"] == "authorization_not_required"
    assert active["action_required"] is False
    assert not request_path.exists()
    assert not verification_path.exists()


def test_ready_handoff_persists_private_exact_expiring_request(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"

    handoff = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert handoff["status"] == "action_required"
    assert handoff["handoff_state"] == "authorization_request_staged"
    assert handoff["request_id"].startswith("pqsar_")
    assert handoff["action_required"] is True
    assert handoff["interrupt_operator"] is False
    assert handoff["authorization_required"] is True
    assert handoff["authorization_recorded"] is False
    assert handoff["automatic_execution_allowed"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["deployment_or_restart_performed"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False
    assert handoff["actions"][0]["lane"] == "scheduler_activation"
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600


def test_request_verification_rejects_stale_tampered_or_rebound_sources() -> None:
    source = _readiness()
    request = authorization.build_scheduler_activation_authorization_request(
        activation_readiness=source,
        now=NOW,
    )
    verified = authorization.verify_scheduler_activation_authorization_request(
        request,
        activation_readiness=source,
        now=NOW,
    )
    assert verified["status"] == "verified"
    assert verified["request_id"] == request["request_id"]

    stale = authorization.verify_scheduler_activation_authorization_request(
        request,
        activation_readiness=source,
        now=NOW + timedelta(seconds=901),
    )
    assert stale["blocking_reason"] == (
        "scheduler_activation_authorization_not_fresh"
    )

    tampered = dict(request)
    tampered["deployment_or_restart_authorized"] = True
    normalized = dict(tampered)
    normalized.pop("integrity")
    tampered["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": authorization._sha256(
            authorization._canonical(normalized)
        ),
    }
    invalid = authorization.verify_scheduler_activation_authorization_request(
        tampered,
        activation_readiness=source,
        now=NOW,
    )
    assert invalid["blocking_reason"] == (
        "scheduler_activation_authorization_source_binding_mismatch"
    )

    rebound_source = _readiness(receipt_digest="9" * 64)
    rebound = authorization.verify_scheduler_activation_authorization_request(
        request,
        activation_readiness=rebound_source,
        now=NOW,
    )
    assert rebound["status"] == "verified"
    assert rebound["binding_mode"] == "stable_scope_current_readiness"

    changed_source = _readiness(render_digit="4")
    changed = authorization.verify_scheduler_activation_authorization_request(
        request,
        activation_readiness=changed_source,
        now=NOW,
    )
    assert changed["blocking_reason"] == (
        "scheduler_activation_authorization_source_binding_mismatch"
    )
    assert changed["deployment_or_restart_authorized"] is False


def test_presentation_dedupes_evidence_refresh_but_not_semantic_change(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"
    state_path = tmp_path / "presentation-state.json"
    first = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )
    projected_first = presentation.apply_operator_presentation_state(
        first,
        state_path=state_path,
    )
    assert projected_first["interrupt_operator"] is True
    assert len(projected_first["actions"]) == 1
    recorded = presentation.record_operator_presentation(
        projected_first,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW,
    )
    assert recorded["status"] == "recorded"

    refreshed_at = NOW + timedelta(seconds=1)
    refreshed = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(
            now=refreshed_at,
            receipt_digest="9" * 64,
        ),
        request_path=request_path,
        verification_path=verification_path,
        now=refreshed_at,
    )
    projected_refreshed = presentation.apply_operator_presentation_state(
        refreshed,
        state_path=state_path,
    )
    assert refreshed["request_id"] == first["request_id"]
    assert refreshed["request_sha256"] == first["request_sha256"]
    assert refreshed["request_state"] == "retained_current"
    assert projected_refreshed["status"] == "pending_action"
    assert projected_refreshed["interrupt_operator"] is False
    assert projected_refreshed["actions"] == []
    assert len(projected_refreshed["pending_actions"]) == 1

    changed_at = NOW + timedelta(seconds=2)
    changed = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(
            now=changed_at,
            receipt_digest="8" * 64,
            render_digit="4",
        ),
        request_path=request_path,
        verification_path=verification_path,
        now=changed_at,
    )
    projected_changed = presentation.apply_operator_presentation_state(
        changed,
        state_path=state_path,
    )
    assert changed["request_id"] != first["request_id"]
    assert projected_changed["interrupt_operator"] is True
    assert len(projected_changed["actions"]) == 1


def test_readiness_revocation_clears_presentation_without_overwriting_request(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"
    state_path = tmp_path / "presentation-state.json"
    first = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )
    projected = presentation.apply_operator_presentation_state(
        first,
        state_path=state_path,
    )
    presentation.record_operator_presentation(
        projected,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="a" * 64,
        now=NOW,
    )
    request_bytes = request_path.read_bytes()

    revoked_at = NOW + timedelta(seconds=1)
    revoked = authorization.stage_current_scheduler_activation_authorization_handoff(
        activation_readiness=_readiness(
            state="blocked",
            now=revoked_at,
            receipt_digest="7" * 64,
        ),
        request_path=request_path,
        verification_path=verification_path,
        now=revoked_at,
    )
    projected_revoked = presentation.apply_operator_presentation_state(
        revoked,
        state_path=state_path,
    )
    cleared = presentation.record_operator_presentation(
        projected_revoked,
        state_path=state_path,
        expected_source_cycle_receipt_sha256="7" * 64,
        now=revoked_at,
    )

    assert revoked["action_required"] is False
    assert revoked["interrupt_operator"] is False
    assert request_path.read_bytes() == request_bytes
    assert cleared["status"] == "recorded"
    assert cleared["active_presentation_count"] == 0
