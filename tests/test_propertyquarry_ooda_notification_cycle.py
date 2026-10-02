from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_notification_cycle as cycle


NOW = datetime(2026, 8, 26, 4, 15, tzinfo=timezone.utc)
GOLD_SOURCE_TIME = "2026-08-26T04:05:17+00:00"
GOLD_GENERATED_AT = "2026-08-26T04:10:58+00:00"
SCENE_PACKET_TIME = "2026-08-26T04:10:00+00:00"
SCENE_SOURCE_TIME = "2026-08-26T04:09:59+00:00"
SCENE_RUNTIME_TIME = "2026-08-26T04:10:05+00:00"
SCENE_SOURCE_REF = "/data/artifacts/property-scene-video-readiness.json"
RAW_SECRET = "raw-secret-marker-must-not-escape"


def _gold_action_receipt() -> dict[str, object]:
    return {
        "schema": "propertyquarry.gold_status.v1",
        "status": "blocked",
        "generated_at": GOLD_GENERATED_AT,
        "live_mobile_surfaces": {
            "route_probe_blocked": True,
            "runtime_blocker": RAW_SECRET,
            "operator_action": {
                "required": True,
                "interrupt_operator": True,
                "source_fresh": True,
                "source_generated_at": GOLD_SOURCE_TIME,
                "reason": "live_runtime_host_admission_rejected",
                "runtime_blocker": RAW_SECRET,
                "reversible_next_action": RAW_SECRET,
                "notification_policy": "action_required_only",
                "provider_quota_consumption_allowed": False,
                "consent_gate": {
                    "required": True,
                    "automatic_execution_allowed": False,
                    "protected_operations": [
                        "runtime_configuration_change",
                        "deployment_or_restart",
                    ],
                },
            },
        },
    }


def _gold_clear_receipt() -> dict[str, object]:
    return {
        "schema": "propertyquarry.gold_status.v1",
        "status": "pass",
        "generated_at": GOLD_GENERATED_AT,
        "ready_for_notification": True,
    }


def _fresh_cli_gold_receipt() -> dict[str, object]:
    generated_at = datetime.now(timezone.utc).isoformat()
    receipt = _gold_action_receipt()
    receipt["generated_at"] = generated_at
    receipt["live_mobile_surfaces"]["operator_action"]["source_generated_at"] = generated_at
    return receipt


def _scene_packet(*, actionable: bool = True, stale: bool = False) -> dict[str, object]:
    packet_time = "2026-07-10T01:14:57+00:00" if stale else SCENE_PACKET_TIME
    source_time = "2026-07-10T01:14:56+00:00" if stale else SCENE_SOURCE_TIME
    gap = 1 if actionable else 0
    return {
        "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
        "generated_at": packet_time,
        "source_receipt": SCENE_SOURCE_REF,
        "source_receipt_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_receipt_generated_at": source_time,
        "providers": [
            {
                "provider": "magicfit",
                "expected_account_count": 2,
                "runtime_account_count": 2 - gap,
                "visible_account_gap": gap,
                "credit_state": "funded",
                "credit_refresh_required": False,
                "runtime_blockers": [RAW_SECRET],
                "post_refresh_checks": [RAW_SECRET],
            },
            {
                "provider": "omagic",
                "expected_account_count": 8,
                "runtime_account_count": 8,
                "visible_account_gap": 0,
                "credit_state": "funded",
                "credit_refresh_required": False,
            },
        ],
    }


def _scene_verifier(*, stale: bool = False) -> dict[str, object]:
    return {
        "status": "pass",
        "generated_at": "2026-07-10T01:14:57+00:00" if stale else SCENE_PACKET_TIME,
        "provider_count": 2,
        "checked_providers": ["magicfit", "omagic"],
        "blockers": [],
    }


def _scene_runtime(*, actionable: bool = True, stale: bool = False) -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_runtime_status.v1",
        "generated_at": "2026-07-10T01:14:56+00:00" if stale else SCENE_RUNTIME_TIME,
        "source_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_kind": "receipt_file",
        "source_ref": SCENE_SOURCE_REF,
        "summary": {
            "action_required_count": 1 if actionable else 0,
            "action_required_providers": ["magicfit"] if actionable else [],
        },
        "providers": [
            {
                "provider": "magicfit",
                "status": "blocked" if actionable else "ready",
                "attention_required": actionable,
            },
            {
                "provider": "omagic",
                "status": "ready",
                "attention_required": False,
            },
        ],
    }


def _evidence() -> dict[str, dict[str, object]]:
    return {
        name: {"path": f"/{name}.json", "status": "ready", "sha256": name * 4}
        for name in (
            "gold_receipt",
            "scene_packet",
            "scene_verifier",
            "scene_runtime_status",
        )
    }


def _run_cycle(
    *,
    tmp_path: Path,
    gold_receipt: dict[str, object] | None = None,
    scene_packet: dict[str, object] | None = None,
    scene_verifier: dict[str, object] | None = None,
    scene_runtime: dict[str, object] | None = None,
    send: bool = False,
    deliver=None,
) -> dict[str, object]:
    return cycle.build_cycle_report(
        gold_receipt=gold_receipt or _gold_action_receipt(),
        scene_packet=scene_packet or _scene_packet(stale=True),
        scene_verifier=scene_verifier or _scene_verifier(stale=True),
        scene_runtime_status=scene_runtime or _scene_runtime(stale=True),
        input_evidence=_evidence(),
        state_path=tmp_path / "cycle-state.json",
        principal_id="cf-email:tibor.girschele@gmail.com",
        base_url="https://propertyquarry.com",
        send=send,
        now=NOW,
        deliver=deliver,
    )


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cycle_defaults_to_evaluation_only_and_never_delivers(tmp_path: Path) -> None:
    report = _run_cycle(
        tmp_path=tmp_path,
        deliver=lambda **kwargs: (_ for _ in ()).throw(AssertionError("evaluation must not deliver")),
    )

    assert report["status"] == "action_required"
    assert report["execution_mode"] == "evaluate_only"
    assert report["delivery_authorized"] is False
    assert report["delivery_attempted"] is False
    assert report["sent"] is False
    assert report["would_send"] is True
    assert report["novel_action_count"] == 1
    assert report["notification_count"] == 0
    assert report["automatic_execution_allowed"] is False
    assert report["provider_quota_consumption_allowed"] is False
    assert report["source_evidence_posture"]["status"] == "waiting_for_fresh_sources"
    assert report["source_evidence_posture"]["progress"] == {
        "expected_lane_count": 2,
        "current_lane_count": 1,
        "stale_lane_count": 1,
        "unavailable_lane_count": 0,
    }
    assert RAW_SECRET not in json.dumps(report)
    assert not (tmp_path / "cycle-state.json").exists()


def test_cycle_rejects_untyped_gold_projection(tmp_path: Path) -> None:
    receipt = _gold_action_receipt()
    receipt["schema"] = "untrusted.gold.projection"

    report = _run_cycle(tmp_path=tmp_path, gold_receipt=receipt)

    gold_lane = next(row for row in report["lanes"] if row["lane"] == "gold_live_runtime")
    assert gold_lane["reason"] == "gold_contract_not_admissible"
    assert gold_lane["action_required"] is False
    assert report["operator_action_required"] is False
    assert report["sent"] is False


def test_cycle_consolidates_two_actions_into_one_factual_delivery(tmp_path: Path) -> None:
    deliveries: list[dict[str, object]] = []

    report = _run_cycle(
        tmp_path=tmp_path,
        scene_packet=_scene_packet(),
        scene_verifier=_scene_verifier(),
        scene_runtime=_scene_runtime(),
        send=True,
        deliver=lambda **kwargs: deliveries.append(kwargs)
        or {"delivery_mode": "principal_binding", "message_ids": ["6101"]},
    )

    assert report["status"] == "completed"
    assert report["action_required_count"] == 2
    assert report["novel_action_count"] == 2
    assert report["notification_count"] == 1
    assert report["sent"] is True
    assert len(deliveries) == 1
    assert deliveries[0]["url_buttons"] == [[("Open PropertyQuarry", "https://propertyquarry.com")]]
    assert "1. Live runtime" in str(deliveries[0]["text"])
    assert "2. Scene-video providers" in str(deliveries[0]["text"])
    assert "automatic execution disabled" in str(deliveries[0]["text"])
    assert RAW_SECRET not in str(deliveries[0]["text"])
    state_path = tmp_path / "cycle-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["active_actions"]) == 2
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert report["source_evidence_posture"]["status"] == "verified_current"


def test_cycle_does_not_republish_a_stale_scene_action_as_current(
    tmp_path: Path,
) -> None:
    report = _run_cycle(
        tmp_path=tmp_path,
        gold_receipt=_gold_clear_receipt(),
        scene_packet=_scene_packet(stale=True),
        scene_verifier=_scene_verifier(stale=True),
        scene_runtime=_scene_runtime(stale=True),
    )

    assert report["status"] == "silent"
    assert report["action_required_count"] == 0
    assert report["operator_action_required"] is False
    assert report["interrupt_operator"] is False
    assert report["source_evidence_posture"]["status"] == "waiting_for_fresh_sources"
    assert report["next_action"] == report["source_evidence_posture"]["next_action"]
    scene_lane = next(
        row
        for row in report["source_evidence_posture"]["lanes"]
        if row["lane"] == "scene_video_provider_refresh"
    )
    assert scene_lane["status"] == "stale"
    assert scene_lane["reason"] == "source_receipt_not_fresh"


def test_cycle_dedupes_active_incidents_without_delivery(tmp_path: Path) -> None:
    first = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": ["6102"]},
    )
    assert first["sent"] is True

    second = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: (_ for _ in ()).throw(AssertionError("active incident must dedupe")),
    )

    assert second["status"] == "deduplicated"
    assert second["operator_action_required"] is True
    assert second["interrupt_operator"] is False
    assert second["novel_action_count"] == 0
    assert second["sent"] is False


def test_invalid_incident_ledger_cannot_suppress_send_or_be_overwritten(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "cycle-state.json"
    first = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **_kwargs: {
            "delivery_mode": "principal_binding",
            "message_ids": ["valid-baseline"],
        },
    )
    assert first["status"] == "completed"
    assert first["notification_state_status"] == "ready"
    valid_state = json.loads(state_path.read_text(encoding="utf-8"))

    forged_states: list[dict[str, object]] = []
    extra_authority = json.loads(json.dumps(valid_state))
    extra_authority["delivery_authorized"] = True
    forged_states.append(extra_authority)

    unknown_lane = json.loads(json.dumps(valid_state))
    unknown_lane["active_actions"]["untrusted_lane"] = "a" * 64
    forged_states.append(unknown_lane)

    invalid_digest = json.loads(json.dumps(valid_state))
    invalid_digest["active_actions"]["gold_live_runtime"] = "not-a-digest"
    forged_states.append(invalid_digest)

    future_timestamp = json.loads(json.dumps(valid_state))
    future_timestamp["updated_at"] = (NOW + timedelta(seconds=31)).isoformat()
    forged_states.append(future_timestamp)

    false_delivery_identity = json.loads(json.dumps(valid_state))
    false_delivery_identity["last_message_ids"] = []
    forged_states.append(false_delivery_identity)

    for forged_state in forged_states:
        cycle.gold_notify._write_notification_state(state_path, forged_state)
        forged_bytes = state_path.read_bytes()

        evaluate = _run_cycle(
            tmp_path=tmp_path,
            send=False,
            deliver=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("evaluate-only invalid-ledger recovery must not deliver")
            ),
        )
        assert evaluate["status"] == "action_required"
        assert evaluate["execution_mode"] == "evaluate_only"
        assert evaluate["delivery_authorized"] is False
        assert evaluate["delivery_attempted"] is False
        assert evaluate["sent"] is False
        assert evaluate["interrupt_operator"] is True
        assert evaluate["novel_action_count"] == 1
        assert evaluate["notification_state_status"] == "invalid"
        assert evaluate["notification_state_admissible"] is False
        assert state_path.read_bytes() == forged_bytes

        send_requested = _run_cycle(
            tmp_path=tmp_path,
            send=True,
            deliver=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("invalid ledger must block delivery")
            ),
        )
        assert send_requested["status"] == "action_required"
        assert send_requested["execution_mode"] == "evaluate_only"
        assert send_requested["delivery_authorized"] is False
        assert send_requested["delivery_attempted"] is False
        assert send_requested["sent"] is False
        assert send_requested["send_requested"] is True
        assert send_requested["notification_state_status"] == "invalid"
        assert state_path.read_bytes() == forged_bytes


def test_invalid_incident_ledger_blocks_clear_state_mutation_without_interrupt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "cycle-state.json"
    cycle.gold_notify._write_notification_state(
        state_path,
        {
            "schema": cycle.NOTIFICATION_STATE_SCHEMA,
            "updated_at": NOW.isoformat(),
            "active_actions": {"untrusted_lane": "a" * 64},
            "last_cycle_status": "completed",
            "last_delivery_mode": "principal_binding",
            "last_message_ids": ["forged"],
        },
    )
    forged_bytes = state_path.read_bytes()

    report = _run_cycle(
        tmp_path=tmp_path,
        gold_receipt=_gold_clear_receipt(),
        scene_packet=_scene_packet(actionable=False),
        scene_verifier=_scene_verifier(),
        scene_runtime=_scene_runtime(actionable=False),
        send=True,
        deliver=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid clear ledger must not deliver")
        ),
    )

    assert report["status"] == "notification_state_invalid"
    assert report["execution_mode"] == "evaluate_only"
    assert report["delivery_authorized"] is False
    assert report["delivery_attempted"] is False
    assert report["sent"] is False
    assert report["interrupt_operator"] is False
    assert report["actions"] == []
    assert report["send_requested"] is True
    assert report["notification_state_status"] == "invalid"
    assert state_path.read_bytes() == forged_bytes


def test_cycle_cli_returns_nonzero_for_invalid_incident_ledger(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    gold_path = _write_json(tmp_path / "gold.json", _gold_clear_receipt())
    packet_path = _write_json(
        tmp_path / "packet.json", _scene_packet(actionable=False)
    )
    verifier_path = _write_json(tmp_path / "verifier.json", _scene_verifier())
    runtime_path = _write_json(
        tmp_path / "runtime.json", _scene_runtime(actionable=False)
    )
    state_path = tmp_path / "state.json"
    receipt_path = tmp_path / "receipt.json"
    cycle.gold_notify._write_notification_state(
        state_path,
        {
            "schema": cycle.NOTIFICATION_STATE_SCHEMA,
            "updated_at": NOW.isoformat(),
            "active_actions": {"untrusted_lane": "a" * 64},
            "last_cycle_status": "completed",
            "last_delivery_mode": "principal_binding",
            "last_message_ids": ["forged"],
        },
    )
    original_state = state_path.read_bytes()
    monkeypatch.setattr(
        cycle.gold_notify,
        "deliver_notification_for_principal",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid ledger must not deliver")
        ),
    )

    exit_code = cycle.main(
        [
            "--gold-receipt",
            str(gold_path),
            "--scene-packet",
            str(packet_path),
            "--scene-verifier",
            str(verifier_path),
            "--scene-runtime-status",
            str(runtime_path),
            "--state-file",
            str(state_path),
            "--write",
            str(receipt_path),
        ]
    )

    assert exit_code == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "notification_state_invalid"
    assert receipt["execution_mode"] == "evaluate_only"
    assert receipt["interrupt_operator"] is False
    assert receipt["delivery_attempted"] is False
    assert state_path.read_bytes() == original_state


def test_cycle_clears_only_from_fresh_verified_evidence_then_allows_recurrence(tmp_path: Path) -> None:
    first = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": ["6103"]},
    )
    assert first["sent"] is True

    clear = _run_cycle(
        tmp_path=tmp_path,
        gold_receipt=_gold_clear_receipt(),
        scene_packet=_scene_packet(actionable=False),
        scene_verifier=_scene_verifier(),
        scene_runtime=_scene_runtime(actionable=False),
        send=True,
        deliver=lambda **kwargs: (_ for _ in ()).throw(AssertionError("clear evidence must stay silent")),
    )

    assert clear["status"] == "silent"
    assert clear["sent"] is False
    assert clear["state_updated"] is True
    state = json.loads((tmp_path / "cycle-state.json").read_text(encoding="utf-8"))
    assert state["active_actions"] == {}

    recurrence = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": ["6104"]},
    )
    assert recurrence["sent"] is True


def test_cycle_delivery_failure_is_sanitized_and_does_not_advance_state(tmp_path: Path) -> None:
    report = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: (_ for _ in ()).throw(RuntimeError(RAW_SECRET)),
    )

    assert report["status"] == "delivery_failed"
    assert report["delivery_attempted"] is True
    assert report["delivery_error_code"] == "RuntimeError"
    assert report["sent"] is False
    assert RAW_SECRET not in json.dumps(report)
    assert not (tmp_path / "cycle-state.json").exists()


def test_cycle_rejects_incomplete_delivery_receipt(tmp_path: Path) -> None:
    report = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": []},
    )

    assert report["status"] == "delivery_failed"
    assert report["delivery_error_code"] == "delivery_receipt_incomplete"
    assert report["sent"] is False
    assert not (tmp_path / "cycle-state.json").exists()


def test_cycle_rejects_malformed_delivery_receipts_without_crashing(
    tmp_path: Path,
) -> None:
    malformed_receipts = [
        None,
        "not-a-receipt",
        {"delivery_mode": "principal_binding", "message_ids": "6101"},
        {"delivery_mode": "principal binding", "message_ids": ["6101"]},
        {"delivery_mode": "principal_binding", "message_ids": ["6101", "6101"]},
        {"delivery_mode": "principal_binding", "message_ids": [" 6101"]},
        {"delivery_mode": "principal_binding", "message_ids": [6101]},
    ]

    for malformed in malformed_receipts:
        report = _run_cycle(
            tmp_path=tmp_path,
            send=True,
            deliver=lambda **_kwargs: malformed,
        )
        assert report["status"] == "delivery_failed"
        assert report["delivery_error_code"] == "delivery_receipt_incomplete"
        assert report["sent"] is False
        assert report["state_updated"] is False
        assert not (tmp_path / "cycle-state.json").exists()


def test_cycle_reports_delivery_that_could_not_be_recorded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cycle.gold_notify,
        "_write_notification_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(RAW_SECRET)),
    )

    report = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": ["6105"]},
    )

    assert report["status"] == "delivery_unrecorded"
    assert report["sent"] is True
    assert report["state_updated"] is False
    assert report["state_error_code"] == "OSError"
    assert RAW_SECRET not in json.dumps(report)


def test_cycle_verifies_the_exact_incident_ledger_after_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_private_state = cycle.gold_notify._write_notification_state

    def write_forged_state(path: Path, _payload: dict[str, object]) -> None:
        write_private_state(
            path,
            {
                "schema": cycle.NOTIFICATION_STATE_SCHEMA,
                "updated_at": NOW.isoformat(),
                "active_actions": {"untrusted_lane": "a" * 64},
                "last_cycle_status": "completed",
                "last_delivery_mode": "principal_binding",
                "last_message_ids": ["forged"],
            },
        )

    monkeypatch.setattr(
        cycle.gold_notify,
        "_write_notification_state",
        write_forged_state,
    )

    report = _run_cycle(
        tmp_path=tmp_path,
        send=True,
        deliver=lambda **_kwargs: {
            "delivery_mode": "principal_binding",
            "message_ids": ["delivered-but-unrecorded"],
        },
    )

    assert report["status"] == "delivery_unrecorded"
    assert report["sent"] is True
    assert report["state_updated"] is False
    assert report["state_error_code"] == "state_verification_failed"
    assert report["notification_state_status"] == "invalid"
    assert report["notification_state_admissible"] is False
    assert len(str(report["notification_state_sha256"])) == 64


def test_cycle_cli_default_writes_private_receipt_without_state_or_delivery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    gold_path = _write_json(tmp_path / "gold.json", _fresh_cli_gold_receipt())
    packet_path = _write_json(tmp_path / "packet.json", _scene_packet(stale=True))
    verifier_path = _write_json(tmp_path / "verifier.json", _scene_verifier(stale=True))
    runtime_path = _write_json(tmp_path / "runtime.json", _scene_runtime(stale=True))
    state_path = tmp_path / "state.json"
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(
        cycle.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("default CLI must not deliver")),
    )

    exit_code = cycle.main(
        [
            "--gold-receipt",
            str(gold_path),
            "--scene-packet",
            str(packet_path),
            "--scene-verifier",
            str(verifier_path),
            "--scene-runtime-status",
            str(runtime_path),
            "--state-file",
            str(state_path),
            "--write",
            str(receipt_path),
        ]
    )

    assert exit_code == 0
    stdout = json.loads(capsys.readouterr().out)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert stdout["execution_mode"] == "evaluate_only"
    assert receipt["status"] == "action_required"
    assert receipt["sent"] is False
    assert RAW_SECRET not in json.dumps(receipt)
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert not state_path.exists()


def test_cycle_cli_send_lock_prevents_concurrent_delivery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    gold_path = _write_json(tmp_path / "gold.json", _fresh_cli_gold_receipt())
    packet_path = _write_json(tmp_path / "packet.json", _scene_packet(stale=True))
    verifier_path = _write_json(tmp_path / "verifier.json", _scene_verifier(stale=True))
    runtime_path = _write_json(tmp_path / "runtime.json", _scene_runtime(stale=True))
    state_path = tmp_path / "state.json"
    receipt_path = tmp_path / "receipt.json"
    lock_path = tmp_path / "send.lock"
    lock_descriptor = cycle._acquire_send_lock(lock_path)
    assert lock_descriptor is not None
    monkeypatch.setattr(
        cycle.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("busy cycle must not deliver")),
    )

    try:
        exit_code = cycle.main(
            [
                "--gold-receipt",
                str(gold_path),
                "--scene-packet",
                str(packet_path),
                "--scene-verifier",
                str(verifier_path),
                "--scene-runtime-status",
                str(runtime_path),
                "--state-file",
                str(state_path),
                "--lock-file",
                str(lock_path),
                "--write",
                str(receipt_path),
                "--send",
            ]
        )
    finally:
        cycle._release_send_lock(lock_descriptor)

    assert exit_code == 2
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "send_cycle_busy"
    assert stdout["delivery_attempted"] is False
    assert stdout["interrupt_operator"] is False
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert not state_path.exists()


def test_run_cycle_once_is_silent_transport_wise_and_writes_private_receipt(
    tmp_path: Path,
) -> None:
    gold_path = _write_json(tmp_path / "gold.json", _fresh_cli_gold_receipt())
    packet_path = _write_json(tmp_path / "packet.json", _scene_packet(stale=True))
    verifier_path = _write_json(tmp_path / "verifier.json", _scene_verifier(stale=True))
    runtime_path = _write_json(tmp_path / "runtime.json", _scene_runtime(stale=True))
    receipt_path = tmp_path / "latest.json"

    report = cycle.run_cycle_once(
        gold_receipt=str(gold_path),
        scene_packet=str(packet_path),
        scene_verifier=str(verifier_path),
        scene_runtime_status=str(runtime_path),
        state_file=str(tmp_path / "state.json"),
        lock_file=str(tmp_path / "send.lock"),
        write=str(receipt_path),
        send=False,
        deliver=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("evaluation-only cycle must not deliver")
        ),
    )

    assert report["status"] == "action_required"
    assert report["delivery_authorized"] is False
    assert report["delivery_attempted"] is False
    assert report["sent"] is False
    assert report["would_send"] is True
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == report
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert not (tmp_path / "state.json").exists()
