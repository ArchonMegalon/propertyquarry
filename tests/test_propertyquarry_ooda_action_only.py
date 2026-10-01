from __future__ import annotations

import json
from pathlib import Path

from scripts import propertyquarry_ooda_action_only as action_only


def _status(*, interrupt: bool) -> dict[str, object]:
    action = {
        "lane": "gold_live_runtime",
        "reason": "live_runtime_tunnel_unavailable",
        "safe_next_action": "inspect and stage recovery for review",
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
        "provider_quota_consumption_allowed": False,
    }
    return {
        "status": "action_required" if interrupt else "pending_action",
        "action_required": True,
        "interrupt_operator": interrupt,
        "updated_at": "2026-08-27T08:30:00+00:00",
        "blocking_reason": "live_runtime_tunnel_unavailable",
        "next_action": action["safe_next_action"],
        "actions": [action] if interrupt else [],
        "pending_actions": [action],
        "source_cycle_receipt_sha256": "a" * 64,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
    }


def _trust_source() -> dict[str, object]:
    return {
        "receipt": {
            "generated_at": "2026-08-27T09:50:00+00:00",
            "candidate_review_id": "pqtrustreview_" + "b" * 24,
            "candidate_scope": [
                {
                    "producer_id": "producer-a",
                    "key_id": "key-a",
                    "public_key_sha256": "c" * 64,
                    "lanes": ["gold_live_runtime"],
                    "candidate_receipt_sha256": "d" * 64,
                }
            ],
            "next_action": (
                "verify identity out of band, then record an exact immutable "
                "confirm, reject, or defer decision"
            ),
        },
        "receipt_path": "/private/trust-notification.json",
        "receipt_sha256": "e" * 64,
    }


def test_action_only_prints_and_records_only_a_novel_action(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    status = _status(interrupt=True)
    recorded: dict[str, object] = {}
    projected: dict[str, object] = {}
    settled: dict[str, object] = {}
    verified: dict[str, object] = {}
    monkeypatch.setattr(
        action_only.operator_status,
        "load_operator_status",
        lambda **_kwargs: status,
    )
    monkeypatch.setattr(
        action_only,
        "load_trust_action_source",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a novel current-cycle action must win")
        ),
    )
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {"recovery_preview_sha256": "f" * 64},
    }
    monkeypatch.setattr(
        action_only,
        "load_current_runtime_presentation_context",
        lambda *_args, **_kwargs: {"gold_live_runtime": context},
    )

    def project(source, **kwargs):
        projected["status"] = source
        projected.update(kwargs)
        return source

    monkeypatch.setattr(
        action_only.operator_status,
        "project_operator_presentation_with_context_hydration",
        project,
    )

    def record(projected, **kwargs):
        recorded["status"] = projected
        recorded.update(kwargs)
        return {
            "status": "recorded",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        }

    monkeypatch.setattr(
        action_only.operator_status,
        "record_operator_presentation",
        record,
    )

    def settle(projection, presentation_receipt, **kwargs):
        settled["projection"] = projection
        settled["presentation_receipt"] = presentation_receipt
        settled.update(kwargs)
        return {
            "status": "settled",
            "current_evidence_verified": True,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }

    monkeypatch.setattr(
        action_only,
        "settle_runtime_control_presentation",
        settle,
    )

    def verify_effective(**kwargs):
        verified.update(kwargs)
        return {
            "status": "verified",
            "historical_tick_preserved": True,
            "action_required": True,
            "interrupt_operator": False,
            "presentation_recorded": True,
            "current_evidence_verified": True,
            "verification_receipt_persisted": True,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }

    monkeypatch.setattr(
        action_only,
        "verify_effective_runtime_control",
        verify_effective,
    )
    presentation = tmp_path / "presentation.json"
    control_receipt = tmp_path / "runtime-control.json"
    control_lock = tmp_path / "runtime-control.lock"
    safe_tick = tmp_path / "safe-tick.json"
    effective_verification = tmp_path / "effective-verification.json"

    assert action_only.main(
        [
            "--presentation-state",
            str(presentation),
            "--runtime-control-receipt",
            str(control_receipt),
            "--runtime-control-lock",
            str(control_lock),
            "--safe-tick-receipt",
            str(safe_tick),
            "--runtime-control-effective-verification",
            str(effective_verification),
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "action_required"
    assert output["next_action"] == "inspect and stage recovery for review"
    assert output["consent_gate"] == {
        "required": True,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
    }
    assert output["execution_authorized"] is False
    assert output["deployment_or_restart_authorized"] is False
    assert output["provider_quota_consumption_allowed"] is False
    assert output["delivery_authorized"] is False
    assert projected["presentation_context_by_lane"] == {
        "gold_live_runtime": context
    }
    assert projected["state_path"] == presentation
    assert recorded["status"] == status
    assert recorded["state_path"] == presentation
    assert recorded["expected_source_cycle_receipt_sha256"] == "a" * 64
    assert settled["projection"] == output
    assert settled["presentation_receipt"]["status"] == "recorded"
    assert settled["receipt_path"] == control_receipt
    assert settled["lock_path"] == control_lock
    assert verified["safe_tick_receipt_path"] == safe_tick
    assert verified["runtime_control_receipt_path"] == control_receipt
    assert verified["presentation_state_path"] == presentation
    assert verified["verification_path"] == effective_verification
    assert verified["lock_path"] == control_lock


def _runtime_control_receipt(
    projection: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": action_only._RUNTIME_CONTROL_SCHEMA,
        "status": "verified",
        "updated_at": "2026-08-27T08:30:00+00:00",
        "blocking_reason": "live_runtime_tunnel_unavailable",
        "next_action": projection["next_action"],
        "action_required": True,
        "interrupt_operator": True,
        "presentation_recorded": False,
        "novel_action_count": 1,
        "action_projection": projection,
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


def _presentation_receipt() -> dict[str, object]:
    return {
        "schema": action_only.operator_status.PRESENTATION_RECEIPT_SCHEMA,
        "status": "recorded",
        "recorded_at": "2026-08-27T08:31:00+00:00",
        "state_path": "/private/presentation.json",
        "state_sha256": "b" * 64,
        "source_cycle_receipt_sha256": "a" * 64,
        "presentation_recorded": True,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }


def test_runtime_control_presentation_settlement_silences_current_interrupt(
    tmp_path: Path,
) -> None:
    projection = action_only.build_action_only_projection(
        _status(interrupt=True)
    )
    assert projection is not None
    control = _runtime_control_receipt(projection)
    receipt = tmp_path / "private" / "runtime-control-current.json"
    action_only.atomic_write_bytes(
        receipt,
        action_only.runtime_review._canonical(control),
        overwrite=True,
    )

    result = action_only.settle_runtime_control_presentation(
        projection,
        _presentation_receipt(),
        receipt_path=receipt,
        lock_path=tmp_path / "private" / "safe-tick.lock",
    )

    assert result["status"] == "settled"
    assert result["current_evidence_verified"] is True
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    persisted = json.loads(receipt.read_text(encoding="utf-8"))
    assert persisted["status"] == "verified"
    assert persisted["action_required"] is True
    assert persisted["interrupt_operator"] is False
    assert persisted["presentation_recorded"] is True
    assert persisted["novel_action_count"] == 0
    assert persisted["action_projection"] == {}
    assert persisted["presentation_settlement"] == result
    assert persisted["protected_operation_executed"] is False
    assert persisted["provider_quota_consumption_allowed"] is False
    assert persisted["delivery_authorized"] is False


def test_runtime_control_presentation_settlement_preserves_mismatched_receipt(
    tmp_path: Path,
) -> None:
    projection = action_only.build_action_only_projection(
        _status(interrupt=True)
    )
    assert projection is not None
    control = _runtime_control_receipt(projection)
    receipt = tmp_path / "private" / "runtime-control-current.json"
    action_only.atomic_write_bytes(
        receipt,
        action_only.runtime_review._canonical(control),
        overwrite=True,
    )
    mismatched = _presentation_receipt()
    mismatched["source_cycle_receipt_sha256"] = "c" * 64

    result = action_only.settle_runtime_control_presentation(
        projection,
        mismatched,
        receipt_path=receipt,
        lock_path=tmp_path / "private" / "safe-tick.lock",
    )

    assert result["status"] == "blocked"
    assert result["current_evidence_verified"] is False
    assert json.loads(receipt.read_text(encoding="utf-8")) == control


def test_runtime_control_presentation_settlement_fails_closed_when_tick_runs(
    tmp_path: Path,
) -> None:
    projection = action_only.build_action_only_projection(
        _status(interrupt=True)
    )
    assert projection is not None
    receipt = tmp_path / "private" / "runtime-control-current.json"
    action_only.atomic_write_bytes(
        receipt,
        action_only.runtime_review._canonical(
            _runtime_control_receipt(projection)
        ),
        overwrite=True,
    )
    lock = tmp_path / "private" / "safe-tick.lock"
    descriptor = action_only._acquire_runtime_control_lock(lock)
    assert descriptor is not None
    try:
        result = action_only.settle_runtime_control_presentation(
            projection,
            _presentation_receipt(),
            receipt_path=receipt,
            lock_path=lock,
        )
    finally:
        action_only._release_runtime_control_lock(descriptor)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "runtime_control_settlement_lock_unavailable"
    )
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False


def _projection_with_presentation_identity() -> dict[str, object]:
    projection = action_only.build_action_only_projection(
        _status(interrupt=True)
    )
    assert projection is not None
    projection["actions"][0]["presentation_digest"] = "d" * 64
    projection["actions"][0]["presentation_context_digest"] = "e" * 64
    return projection


def _safe_tick_receipt(
    historical_control: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_safe_tick.v1",
        "status": "ready",
        "updated_at": "2026-08-27T08:30:00+00:00",
        "blocking_reason": "",
        "next_action": historical_control["next_action"],
        "action_required": True,
        "interrupt_operator": True,
        "components": {"runtime_control": historical_control},
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _presentation_state(
    projection: dict[str, object],
) -> dict[str, object]:
    action = projection["actions"][0]
    return {
        "schema": action_only.operator_status.PRESENTATION_STATE_SCHEMA,
        "updated_at": "2026-08-27T08:31:00+00:00",
        "source_cycle_receipt_sha256": "a" * 64,
        "active_presentations": {
            "gold_live_runtime": {
                "action_digest": action["presentation_digest"],
                "presentation_context_digest": action[
                    "presentation_context_digest"
                ],
                "first_presented_at": "2026-08-27T08:31:00+00:00",
                "last_presented_at": "2026-08-27T08:31:00+00:00",
                "source_cycle_receipt_sha256": "a" * 64,
            }
        },
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "protected_operation_executed": False,
    }


def test_effective_runtime_control_verifier_preserves_tick_history(
    tmp_path: Path,
) -> None:
    projection = _projection_with_presentation_identity()
    historical_control = _runtime_control_receipt(projection)
    private = tmp_path / "private"
    control_path = private / "runtime-control-current.json"
    tick_path = private / "safe-tick-latest.json"
    state_path = private / "presentation-state.json"
    verification_path = private / "effective-verification.json"
    lock_path = private / "safe-tick.lock"
    for path, payload in (
        (control_path, historical_control),
        (tick_path, _safe_tick_receipt(historical_control)),
        (state_path, _presentation_state(projection)),
    ):
        action_only.atomic_write_bytes(
            path,
            action_only.runtime_review._canonical(payload),
            overwrite=True,
        )
    presentation_receipt = _presentation_receipt()
    presentation_receipt["state_path"] = str(state_path)
    presentation_receipt["state_sha256"] = action_only.runtime_review._sha256(
        action_only.runtime_review._canonical(
            _presentation_state(projection)
        )
    )
    settlement = action_only.settle_runtime_control_presentation(
        projection,
        presentation_receipt,
        receipt_path=control_path,
        lock_path=lock_path,
    )
    assert settlement["status"] == "settled"

    result = action_only.verify_effective_runtime_control(
        safe_tick_receipt_path=tick_path,
        runtime_control_receipt_path=control_path,
        presentation_state_path=state_path,
        verification_path=verification_path,
        lock_path=lock_path,
    )

    assert result["status"] == "verified"
    assert result["transition"] == "operator_presentation_settled"
    assert result["historical_tick_preserved"] is True
    assert result["historical_tick"]["interrupt_operator"] is True
    assert result["effective_runtime_control"]["interrupt_operator"] is False
    assert result["presentation_ledger"]["action_digest"] == "d" * 64
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["current_evidence_verified"] is True
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert json.loads(verification_path.read_text(encoding="utf-8")) == result
    assert json.loads(tick_path.read_text(encoding="utf-8")) == (
        _safe_tick_receipt(historical_control)
    )


def test_effective_runtime_control_verifier_rejects_ledger_mismatch(
    tmp_path: Path,
) -> None:
    projection = _projection_with_presentation_identity()
    historical_control = _runtime_control_receipt(projection)
    private = tmp_path / "private"
    control_path = private / "runtime-control-current.json"
    tick_path = private / "safe-tick-latest.json"
    state_path = private / "presentation-state.json"
    verification_path = private / "effective-verification.json"
    lock_path = private / "safe-tick.lock"
    state = _presentation_state(projection)
    for path, payload in (
        (control_path, historical_control),
        (tick_path, _safe_tick_receipt(historical_control)),
        (state_path, state),
    ):
        action_only.atomic_write_bytes(
            path,
            action_only.runtime_review._canonical(payload),
            overwrite=True,
        )
    presentation_receipt = _presentation_receipt()
    presentation_receipt["state_path"] = str(state_path)
    presentation_receipt["state_sha256"] = action_only.runtime_review._sha256(
        action_only.runtime_review._canonical(state)
    )
    settlement = action_only.settle_runtime_control_presentation(
        projection,
        presentation_receipt,
        receipt_path=control_path,
        lock_path=lock_path,
    )
    assert settlement["status"] == "settled"
    state["active_presentations"]["gold_live_runtime"][
        "action_digest"
    ] = "f" * 64
    action_only.atomic_write_bytes(
        state_path,
        action_only.runtime_review._canonical(state),
        overwrite=True,
    )

    result = action_only.verify_effective_runtime_control(
        safe_tick_receipt_path=tick_path,
        runtime_control_receipt_path=control_path,
        presentation_state_path=state_path,
        verification_path=verification_path,
        lock_path=lock_path,
    )

    assert result["status"] == "blocked"
    assert result["current_evidence_verified"] is False
    assert result["interrupt_operator"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["verification_receipt_persisted"] is True


def test_action_only_is_silent_for_a_retained_action(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        action_only.operator_status,
        "load_operator_status",
        lambda **_kwargs: _status(interrupt=False),
    )
    monkeypatch.setattr(
        action_only.operator_status,
        "record_operator_presentation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a retained action must not be recorded again")
        ),
    )
    monkeypatch.setattr(
        action_only.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda status, **_kwargs: status,
    )
    monkeypatch.setattr(
        action_only,
        "load_trust_action_source",
        lambda **_kwargs: None,
    )

    assert action_only.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_action_only_surfaces_novel_trust_review_after_retained_cycle(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        action_only.operator_status,
        "load_operator_status",
        lambda **_kwargs: _status(interrupt=False),
    )
    monkeypatch.setattr(
        action_only.operator_status,
        "record_operator_presentation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a trust action must not mutate the cycle ledger")
        ),
    )
    monkeypatch.setattr(
        action_only.operator_status,
        "project_operator_presentation_with_context_hydration",
        lambda status, **_kwargs: status,
    )
    monkeypatch.setattr(
        action_only,
        "load_trust_action_source",
        lambda **_kwargs: _trust_source(),
    )

    def record_trust(**kwargs):
        recorded.update(kwargs)
        return {
            "status": "verified",
            "notification_status": "deduplicated",
            "presentation_transition_recorded": True,
            "action_required": True,
            "interrupt_operator": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
        }

    monkeypatch.setattr(
        action_only.trust_notification,
        "record_current_candidate_notification_presentation",
        record_trust,
    )
    trust_state = tmp_path / "trust-presentation.json"
    notification_receipt = tmp_path / "trust-notification.json"

    assert action_only.main(
        [
            "--trust-presentation-state",
            str(trust_state),
            "--trust-notification-receipt",
            str(notification_receipt),
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "action_required"
    assert output["blocking_reason"] == "producer_identity_review_required"
    assert output["actions"][0]["lane"] == (
        "source_refresh_trust_candidate_review"
    )
    assert output["actions"][0]["candidate_review_id"] == (
        "pqtrustreview_" + "b" * 24
    )
    assert output["actions"][0]["candidate_scope"][0][
        "public_key_sha256"
    ] == "c" * 64
    assert output["consent_gate"] == {
        "required": True,
        "protected_operations": [
            "producer_identity_verification_assertion",
            "immutable_trust_candidate_decision",
            "trust_registry_change",
        ],
    }
    assert output["source"] == {
        "type": "trust_candidate_notification",
        "receipt_path": "/private/trust-notification.json",
        "receipt_sha256": "e" * 64,
    }
    assert output["execution_authorized"] is False
    assert output["deployment_or_restart_authorized"] is False
    assert output["provider_quota_consumption_allowed"] is False
    assert output["delivery_authorized"] is False
    assert recorded["expected_candidate_review_id"] == (
        "pqtrustreview_" + "b" * 24
    )
    assert recorded["presentation_state_path"] == trust_state
    assert recorded["receipt_path"] == notification_receipt


def test_action_only_never_prints_an_action_from_a_blocked_projection(
    monkeypatch,
    capsys,
) -> None:
    blocked = {
        "status": "blocked",
        "action_required": False,
        "interrupt_operator": False,
        "blocking_reason": "operator_presentation_state_not_admissible",
        "actions": [],
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
    }
    monkeypatch.setattr(
        action_only.operator_status,
        "load_operator_status",
        lambda **_kwargs: blocked,
    )
    monkeypatch.setattr(
        action_only.operator_status,
        "record_operator_presentation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a blocked projection must not be recorded")
        ),
    )

    assert action_only.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["status"] == "blocked"
    assert failure["blocking_reason"] == "action_only_projection_unavailable"
    assert failure["execution_authorized"] is False
    assert failure["deployment_or_restart_authorized"] is False
    assert failure["provider_quota_consumption_allowed"] is False
    assert failure["delivery_authorized"] is False


def test_runtime_presentation_context_requires_current_verified_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    verification = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {"recovery_preview_sha256": "b" * 64},
    }

    def verify(**kwargs):
        captured.update(kwargs)
        return verification

    monkeypatch.setattr(
        action_only.runtime_review,
        "verify_current_review_packet",
        verify,
    )
    monkeypatch.setattr(
        action_only.runtime_review,
        "operator_presentation_context",
        lambda value: context if value is verification else {},
    )
    packet = tmp_path / "packet.json"
    env = tmp_path / ".env"

    result = action_only.load_current_runtime_presentation_context(
        _status(interrupt=True),
        packet_path=packet,
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        live_mobile_receipt_path=tmp_path / "mobile.json",
        release_manifest_path=tmp_path / "release.json",
        deployment_env_path=env,
        project="property",
        max_age_seconds=300.0,
    )

    assert result == {"gold_live_runtime": context}
    assert captured["packet_path"] == packet
    assert captured["deployment_env_path"] == env
    assert captured["project"] == "property"


def test_runtime_presentation_context_fails_closed_on_stale_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        action_only.runtime_review,
        "verify_current_review_packet",
        lambda **_kwargs: {
            "status": "blocked",
            "progress": {"current_evidence_verified": False},
            "execution_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        },
    )

    try:
        action_only.load_current_runtime_presentation_context(
            _status(interrupt=True),
            packet_path=tmp_path / "packet.json",
            cycle_receipt_path=tmp_path / "cycle.json",
            signal_dir=tmp_path / "signals",
            live_mobile_receipt_path=tmp_path / "mobile.json",
            release_manifest_path=tmp_path / "release.json",
            deployment_env_path=tmp_path / ".env",
            project="property",
            max_age_seconds=300.0,
        )
    except ValueError as exc:
        assert str(exc) == "runtime_action_review_not_current"
    else:
        raise AssertionError("a stale runtime review must fail closed")


def test_action_only_projects_only_genuine_external_environment_inputs() -> None:
    next_action = (
        "place a private dotenv fragment containing exactly the named keys at "
        "_completion/propertyquarry_ooda_notification_cycle/"
        "deployment-environment-operator-import.env; set mode 0600 and do not "
        "paste values into chat: "
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID, "
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET"
    )
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "deployment_environment_intake": {
                "status": "operator_input_required",
                "blocking_reason": "external_account_material_required",
                "next_action": next_action,
                "operator_external_input_required": True,
                "operator_external_input_keys": [
                    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
                    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
                ],
                "operator_external_input_key_count": 2,
                "operator_import_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-operator-import.env"
                ),
                "operator_import_status": "awaiting_operator_import",
                "operator_import_file_status": "missing",
                "operator_import_ready_for_review": False,
                "operator_merge_review_required": False,
                "operator_merge_review_keys": [],
                "operator_merge_review_key_count": 0,
                "locally_stageable_key_count": 17,
                "locally_staged_key_count": 7,
                "locally_pending_key_count": 10,
                "local_merge_review_required": False,
                "local_merge_review_keys": [],
                "local_merge_review_key_count": 0,
                "local_runtime_candidate_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-local-runtime-bindings.env"
                ),
                "local_runtime_candidate_status": "ready_for_merge_review",
                "local_runtime_candidate_ready_for_merge_review": True,
                "local_runtime_candidate_values_hashed": False,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "configuration_apply_authorized": False,
                "deployment_or_restart_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            }
        },
    }

    projection = action_only.build_action_only_projection(
        _status(interrupt=True),
        presentation_context_by_lane={"gold_live_runtime": context},
    )

    assert projection is not None
    assert projection["blocking_reason"] == "external_account_material_required"
    assert projection["next_action"] == next_action
    assert projection["actions"][0]["safe_next_action"] == next_action
    assert projection["operator_input"] == {
        "lane": "governed_operator_import",
        "drop_path": (
            "_completion/propertyquarry_ooda_notification_cycle/"
            "deployment-environment-operator-import.env"
        ),
        "import_status": "awaiting_operator_import",
        "file_status": "missing",
        "required_keys": [
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        ],
        "required_key_count": 2,
        "locally_stageable_key_count": 17,
        "locally_staged_key_count": 7,
        "locally_pending_key_count": 10,
        "local_runtime_candidate_path": (
            "_completion/propertyquarry_ooda_notification_cycle/"
            "deployment-environment-local-runtime-bindings.env"
        ),
        "local_runtime_candidate_status": "ready_for_merge_review",
        "values_allowed_in_chat": False,
        "configuration_merge_authorized": False,
        "environment_values_recorded": False,
        "secret_values_recorded": False,
    }
    assert projection["execution_authorized"] is False
    assert projection["deployment_or_restart_authorized"] is False


def test_action_only_rejects_malformed_external_input_context() -> None:
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "deployment_environment_intake": {
                "status": "operator_input_required",
                "blocking_reason": "external_account_material_required",
                "next_action": "paste the secret into chat",
                "operator_external_input_required": True,
                "operator_external_input_keys": [
                    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET"
                ],
                "operator_external_input_key_count": 1,
                "operator_import_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-operator-import.env"
                ),
                "operator_import_status": "awaiting_operator_import",
                "operator_import_file_status": "missing",
                "operator_import_ready_for_review": False,
                "operator_merge_review_required": False,
                "operator_merge_review_keys": [],
                "operator_merge_review_key_count": 0,
                "locally_stageable_key_count": 0,
                "locally_staged_key_count": 0,
                "locally_pending_key_count": 0,
                "local_merge_review_required": False,
                "local_merge_review_keys": [],
                "local_merge_review_key_count": 0,
                "local_runtime_candidate_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-local-runtime-bindings.env"
                ),
                "local_runtime_candidate_status": "candidate_missing",
                "local_runtime_candidate_ready_for_merge_review": False,
                "local_runtime_candidate_values_hashed": False,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "configuration_apply_authorized": False,
                "deployment_or_restart_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            }
        }
    }

    try:
        action_only.build_action_only_projection(
            _status(interrupt=True),
            presentation_context_by_lane={"gold_live_runtime": context},
        )
    except ValueError as exc:
        assert str(exc) == "runtime_action_context_not_admissible"
    else:
        raise AssertionError("malformed external input context must fail closed")


def test_action_only_stages_complete_import_for_separate_merge_review() -> None:
    drop_path = (
        "_completion/propertyquarry_ooda_notification_cycle/"
        "deployment-environment-operator-import.env"
    )
    next_action = (
        "review the staged external account import at "
        + drop_path
        + " and record explicit runtime configuration merge authority "
        "separately; no values were recorded or hashed"
    )
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "deployment_environment_intake": {
                "status": "operator_merge_review_required",
                "blocking_reason": (
                    "external_account_material_merge_review_required"
                ),
                "next_action": next_action,
                "operator_external_input_required": False,
                "operator_external_input_keys": [],
                "operator_external_input_key_count": 0,
                "operator_import_path": drop_path,
                "operator_import_status": "ready_for_review",
                "operator_import_file_status": "admissible",
                "operator_import_ready_for_review": True,
                "operator_merge_review_required": True,
                "operator_merge_review_keys": [
                    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
                    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
                ],
                "operator_merge_review_key_count": 2,
                "locally_stageable_key_count": 17,
                "locally_staged_key_count": 7,
                "locally_pending_key_count": 10,
                "local_merge_review_required": False,
                "local_merge_review_keys": [],
                "local_merge_review_key_count": 0,
                "local_runtime_candidate_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-local-runtime-bindings.env"
                ),
                "local_runtime_candidate_status": "ready_for_merge_review",
                "local_runtime_candidate_ready_for_merge_review": True,
                "local_runtime_candidate_values_hashed": False,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "configuration_apply_authorized": False,
                "deployment_or_restart_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            }
        },
    }

    projection = action_only.build_action_only_projection(
        _status(interrupt=True),
        presentation_context_by_lane={"gold_live_runtime": context},
    )

    assert projection is not None
    assert projection["blocking_reason"] == (
        "external_account_material_merge_review_required"
    )
    assert projection["next_action"] == next_action
    assert "operator_input" not in projection
    assert projection["operator_review"] == {
        "lane": "governed_operator_import",
        "drop_path": drop_path,
        "review_keys": [
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        ],
        "review_key_count": 2,
        "locally_stageable_key_count": 17,
        "locally_staged_key_count": 7,
        "locally_pending_key_count": 10,
        "local_runtime_candidate_path": (
            "_completion/propertyquarry_ooda_notification_cycle/"
            "deployment-environment-local-runtime-bindings.env"
        ),
        "local_runtime_candidate_status": "ready_for_merge_review",
        "configuration_merge_authorized": False,
        "configuration_merged": False,
        "environment_values_recorded": False,
        "secret_values_recorded": False,
    }
    assert projection["execution_authorized"] is False


def test_action_only_projects_local_candidate_for_consent_gated_merge() -> None:
    candidate_path = (
        "_completion/propertyquarry_ooda_notification_cycle/"
        "deployment-environment-local-runtime-bindings.env"
    )
    review_keys = sorted(action_only.runtime_review._LOCAL_CANDIDATE_KEYS)
    next_action = (
        "review the staged local deployment candidate at "
        + candidate_path
        + " and record explicit runtime configuration merge authority "
        "separately; no values were recorded or hashed"
    )
    consent_request = action_only.runtime_review._runtime_consent_gate(
        {
            "recovery_preview": {
                "deployment_environment": {
                    "intake_plan": {
                        "local_merge_review_required": True,
                        "local_merge_review_keys": review_keys,
                        "local_merge_review_key_count": len(review_keys),
                        "local_runtime_binding_candidate": {
                            "status": "ready_for_merge_review",
                            "candidate_ready_for_merge_review": True,
                            "path": candidate_path,
                            "schema": (
                                "propertyquarry."
                                "deployment_environment_local_runtime_candidate.v2"
                            ),
                        },
                    }
                }
            }
        }
    )
    context = {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "compose_project": "property",
            "recovery_preview_sha256": consent_request[
                "authorization_scope"
            ]["recovery_preview_sha256"],
            "consent_request": consent_request,
            "deployment_environment_intake": {
                "status": "local_merge_review_required",
                "blocking_reason": (
                    "local_deployment_configuration_merge_review_required"
                ),
                "next_action": next_action,
                "operator_external_input_required": False,
                "operator_external_input_keys": [],
                "operator_external_input_key_count": 0,
                "operator_import_path": (
                    "_completion/propertyquarry_ooda_notification_cycle/"
                    "deployment-environment-operator-import.env"
                ),
                "operator_import_status": "not_required",
                "operator_import_file_status": "not_evaluated",
                "operator_import_ready_for_review": False,
                "operator_merge_review_required": False,
                "operator_merge_review_keys": [],
                "operator_merge_review_key_count": 0,
                "locally_stageable_key_count": 8,
                "locally_staged_key_count": 8,
                "locally_pending_key_count": 0,
                "local_merge_review_required": True,
                "local_merge_review_keys": review_keys,
                "local_merge_review_key_count": 8,
                "local_runtime_candidate_path": candidate_path,
                "local_runtime_candidate_status": "ready_for_merge_review",
                "local_runtime_candidate_ready_for_merge_review": True,
                "local_runtime_candidate_values_hashed": False,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "configuration_apply_authorized": False,
                "deployment_or_restart_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            }
        },
    }
    request_scope = {
        "operation": "runtime_configuration_change",
        "change_id": "local_environment_candidate_merge",
        "recovery_preview_sha256": consent_request[
            "authorization_scope"
        ]["recovery_preview_sha256"],
        "candidate_path": candidate_path,
        "candidate_policy_schema": (
            "propertyquarry."
            "deployment_environment_local_runtime_candidate.v2"
        ),
        "candidate_review_keys": review_keys,
        "candidate_review_key_count": len(review_keys),
        "configuration_merge_policy": "add_missing_keys_only",
        "existing_environment_values_overwrite_allowed": False,
        "rollback_required": True,
        "compose_project": "property",
    }
    request_id = action_only.runtime_authorization._request_instance_identifier(
        {},
        request_scope,
        generated_at="2026-08-27T08:30:00+00:00",
    )
    authorization_request = action_only._runtime_authorization_presentation(
        {
            "schema": action_only.runtime_authorization.HANDOFF_SCHEMA,
            "status": "ready",
            "request_state": "refreshed",
            "request_id": request_id,
            "scope": request_scope,
            "decision_options": ["approve_exact_scope", "reject", "defer"],
            "current_evidence_verified": True,
            "authorization_required": True,
            "authorization_recorded": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        },
        context,
    )
    context["scope"]["authorization_request"] = authorization_request
    authorization_instruction = authorization_request[
        "authorization_instruction"
    ]

    replacement_id = (
        action_only.runtime_authorization._request_instance_identifier(
            {},
            request_scope,
            generated_at="2026-08-27T08:45:01+00:00",
        )
    )
    replacement_request = action_only._runtime_authorization_presentation(
        {
            "schema": action_only.runtime_authorization.HANDOFF_SCHEMA,
            "status": "ready",
            "request_state": "refreshed",
            "request_id": replacement_id,
            "scope": request_scope,
            "decision_options": ["approve_exact_scope", "reject", "defer"],
            "current_evidence_verified": True,
            "authorization_required": True,
            "authorization_recorded": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        },
        context,
    )

    assert replacement_id != request_id
    assert replacement_request["authorization_scope"] == request_scope
    assert replacement_request["authorization_instruction"] != (
        authorization_instruction
    )
    assert replacement_request["authorization_recorded"] is False
    assert replacement_request["execution_authorized"] is False
    assert replacement_request["deployment_or_restart_authorized"] is False

    projection = action_only.build_action_only_projection(
        _status(interrupt=True),
        presentation_context_by_lane={"gold_live_runtime": context},
    )

    assert projection is not None
    assert projection["blocking_reason"] == (
        "local_deployment_configuration_merge_review_required"
    )
    assert projection["next_action"] == authorization_instruction
    assert projection["actions"][0]["safe_next_action"] == authorization_instruction
    assert projection["authorization_request"] == authorization_request
    assert projection["operator_review"] == {
        "lane": "governed_local_configuration",
        "candidate_path": candidate_path,
        "candidate_status": "ready_for_merge_review",
        "review_keys": review_keys,
        "review_key_count": 8,
        "configuration_merge_authorized": False,
        "configuration_merged": False,
        "environment_values_recorded": False,
        "secret_values_recorded": False,
    }
    assert projection["execution_authorized"] is False
    assert projection["deployment_or_restart_authorized"] is False
