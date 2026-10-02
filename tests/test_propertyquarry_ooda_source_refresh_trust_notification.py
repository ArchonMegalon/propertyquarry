from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_source_refresh_trust_decision as decision
from scripts import propertyquarry_ooda_source_refresh_trust_intake as intake
from scripts import propertyquarry_ooda_source_refresh_trust_notification as notification


NOW = datetime(2026, 8, 26, 23, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
REVIEW_ID = "pqtrustreview_" + "d" * 24


def _candidate_intake() -> dict[str, object]:
    return {
        "schema": intake.VERIFY_SCHEMA,
        "status": "verified",
        "intake_state": "candidate_ready_for_operator_review",
        "request_id": "pqtrustintake_" + "e" * 24,
        "semantic_request_sha256": SHA_A,
        "candidate_review_id": REVIEW_ID,
        "review_decision_options": list(decision.DECISIONS),
        "next_action": "verify identity and record an immutable decision",
        "intake_receipt_sha256": SHA_B,
        "verification_receipt_sha256": SHA_C,
        "candidates": [
            {
                "producer_id": "producer-a",
                "key_id": "key-a",
                "public_key_sha256": SHA_A,
                "lanes": ["gold_live_runtime"],
                "candidate_receipt_sha256": SHA_B,
                "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
                "proof_of_possession_verified": True,
                "out_of_band_identity_verification_required": True,
                "candidate_confers_authority": False,
                "trust_enrollment_authorized": False,
            }
        ],
        "progress": {"candidate_evidence_count": 1},
        "action_required": True,
        "interrupt_operator": True,
        "operator_review_required": True,
        "identity_verification_asserted": False,
        "public_key_candidate_recorded": True,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "producer_dispatch_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "protected_operation_executed": False,
    }


def _pending_decision() -> dict[str, object]:
    return {
        "schema": decision.VERIFY_SCHEMA,
        "status": "pending",
        "review_state": "candidate_review_pending",
        "candidate_review_id": REVIEW_ID,
        "trust_intake_verification_sha256": SHA_C,
        "decision": "",
        "progress": {
            "current_evidence_verified": True,
            "candidate_review_verified": True,
            "candidate_decision_recorded": False,
        },
        "action_required": True,
        "interrupt_operator": False,
        "operator_review_required": True,
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
    }


def _not_required_intake() -> dict[str, object]:
    return {
        "status": "verified",
        "intake_state": "awaiting_producer_public_key_evidence",
        "candidate_review_id": "",
        "intake_receipt_sha256": SHA_A,
        "verification_receipt_sha256": SHA_B,
        "candidates": [],
        "action_required": False,
        "public_key_candidate_recorded": False,
    }


def _not_required_decision() -> dict[str, object]:
    return {
        "schema": decision.VERIFY_SCHEMA,
        "status": "not_required",
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
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "protected_operation_executed": False,
    }


def test_no_candidate_persists_a_current_not_required_receipt(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "notification.json"
    report = notification.run_candidate_notification(
        _not_required_intake(),
        _not_required_decision(),
        receipt_path=receipt_path,
        now=NOW,
    )

    assert report["status"] == "not_required"
    assert report["receipt_persisted"] is True
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    verified = notification.verify_notification_receipt(
        persisted,
        intake=_not_required_intake(),
        decision=_not_required_decision(),
        now=NOW,
    )
    assert verified["status"] == "verified"
    assert verified["notification_status"] == "not_required"
    assert verified["delivery_authorized"] is False


def test_evaluate_only_candidate_is_actionable_without_recording_presentation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "presentation.json"
    receipt_path = tmp_path / "notification.json"
    report = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=state_path,
        receipt_path=receipt_path,
        now=NOW,
    )

    assert report["status"] == "action_required"
    assert report["action_required"] is True
    assert report["interrupt_operator"] is True
    assert report["would_send"] is True
    assert report["delivery_authorized"] is False
    assert report["sent"] is False
    assert not state_path.exists()
    assert "confirm, reject, or defer" in report["message_preview"]
    assert "private" not in report["message_preview"].lower()


def test_recorded_operator_presentation_immediately_refreshes_interrupt_receipt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "presentation.json"
    receipt_path = tmp_path / "notification.json"
    intake_report = _candidate_intake()
    decision_report = _pending_decision()

    initial = notification.run_candidate_notification(
        intake_report,
        decision_report,
        presentation_state_path=state_path,
        receipt_path=receipt_path,
        now=NOW,
    )
    assert initial["status"] == "action_required"
    assert initial["interrupt_operator"] is True

    presentation = intake.record_candidate_presentation(
        intake_report,
        state_path=state_path,
        expected_candidate_review_id=REVIEW_ID,
        now=NOW,
    )
    assert presentation["status"] == "recorded"

    refreshed = notification.run_candidate_notification(
        intake_report,
        decision_report,
        presentation_state_path=state_path,
        receipt_path=receipt_path,
        now=NOW + timedelta(seconds=1),
    )
    assert refreshed["status"] == "deduplicated"
    assert refreshed["action_required"] is True
    assert refreshed["operator_review_required"] is True
    assert refreshed["interrupt_operator"] is False
    assert refreshed["would_send"] is False
    assert refreshed["delivery_authorized"] is False
    assert refreshed["delivery_attempted"] is False
    assert refreshed["sent"] is False
    assert "message_preview" not in refreshed

    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    verified = notification.verify_notification_receipt(
        persisted,
        intake=intake_report,
        decision=decision_report,
        presentation_state_path=state_path,
        now=NOW + timedelta(seconds=2),
    )
    assert verified["status"] == "verified"
    assert verified["notification_status"] == "deduplicated"
    assert verified["action_required"] is True
    assert verified["interrupt_operator"] is False


def test_runtime_record_transition_is_exact_bound_and_evaluate_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    intake_report = _candidate_intake()
    decision_report = _pending_decision()
    monkeypatch.setattr(
        intake,
        "inspect_trust_intake_bundle",
        lambda **_kwargs: dict(intake_report),
    )
    monkeypatch.setattr(
        decision,
        "verify_candidate_review_decision_for_report",
        lambda *_args, **_kwargs: dict(decision_report),
    )
    state_path = tmp_path / "runtime-presentation.json"
    receipt_path = tmp_path / "runtime-notification.json"

    report = notification.record_current_candidate_notification_presentation(
        expected_candidate_review_id=REVIEW_ID,
        presentation_state_path=state_path,
        receipt_path=receipt_path,
        now=NOW,
    )

    assert report["status"] == "verified"
    assert report["notification_status"] == "deduplicated"
    assert report["presentation_transition_recorded"] is True
    assert report["presentation_receipt"]["status"] == "recorded"
    assert report["presentation_receipt"]["candidate_review_id"] == REVIEW_ID
    assert report["action_required"] is True
    assert report["interrupt_operator"] is False
    assert report["delivery_authorized"] is False
    assert report["delivery_attempted"] is False
    assert report["sent"] is False
    assert report["trust_enrollment_authorized"] is False
    assert report["trust_registry_modified"] is False

    wrong_state_path = tmp_path / "wrong-presentation.json"
    wrong_receipt_path = tmp_path / "wrong-notification.json"
    rejected = notification.record_current_candidate_notification_presentation(
        expected_candidate_review_id="pqtrustreview_" + "f" * 24,
        presentation_state_path=wrong_state_path,
        receipt_path=wrong_receipt_path,
        now=NOW,
    )
    assert rejected["status"] == "blocked"
    assert rejected["blocking_reason"] == (
        "source_refresh_trust_candidate_presentation_binding_mismatch"
    )
    assert rejected["presentation_transition_recorded"] is False
    assert rejected["interrupt_operator"] is False
    assert rejected["delivery_authorized"] is False
    assert rejected["trust_registry_modified"] is False
    assert not wrong_state_path.exists()
    assert not wrong_receipt_path.exists()

    monkeypatch.setattr(
        intake,
        "inspect_trust_intake_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(
            OSError("/private/operator/path/must-not-leak")
        ),
    )
    unavailable = (
        notification.record_current_candidate_notification_presentation(
            expected_candidate_review_id=REVIEW_ID,
            presentation_state_path=tmp_path / "unavailable-presentation.json",
            receipt_path=tmp_path / "unavailable-notification.json",
            now=NOW,
        )
    )
    assert unavailable["status"] == "blocked"
    assert unavailable["blocking_reason"] == (
        "source_refresh_trust_candidate_notification_presentation_unavailable"
    )
    assert "private/operator" not in json.dumps(unavailable)


def test_record_presentation_cli_routes_the_exact_review_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def record(**kwargs):
        captured.update(kwargs)
        return {
            "schema": notification.VERIFY_SCHEMA,
            "status": "verified",
            "notification_status": "deduplicated",
            "interrupt_operator": False,
        }

    monkeypatch.setattr(
        notification,
        "record_current_candidate_notification_presentation",
        record,
    )
    state_path = tmp_path / "presentation.json"
    receipt_path = tmp_path / "notification.json"

    exit_code = notification.main(
        [
            "--record-presentation",
            "--expected-candidate-review-id",
            REVIEW_ID,
            "--presentation-state",
            str(state_path),
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == 0
    assert captured["expected_candidate_review_id"] == REVIEW_ID
    assert captured["presentation_state_path"] == state_path
    assert captured["receipt_path"] == receipt_path
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "verified"
    assert output["notification_status"] == "deduplicated"
    assert output["interrupt_operator"] is False


def test_send_records_delivery_and_suppresses_repeat_alert(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "presentation.json"
    receipt_path = tmp_path / "notification.json"
    lock_path = tmp_path / "send.lock"
    deliveries: list[dict[str, object]] = []

    def deliver(**kwargs):
        deliveries.append(kwargs)
        return {"delivery_mode": "telegram", "message_ids": ["message-1"]}

    report = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=state_path,
        lock_path=lock_path,
        receipt_path=receipt_path,
        principal_id="operator-a",
        send=True,
        now=NOW,
        deliver=deliver,
    )

    assert report["status"] == "completed"
    assert report["delivery_authorized"] is True
    assert report["delivery_attempted"] is True
    assert report["sent"] is True
    assert report["presentation_recorded"] is True
    assert report["action_required"] is True
    assert report["interrupt_operator"] is False
    assert len(deliveries) == 1
    assert "sha256:" + SHA_A in str(deliveries[0]["text"])
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    verified = notification.verify_notification_receipt(
        persisted,
        intake=_candidate_intake(),
        decision=_pending_decision(),
        presentation_state_path=state_path,
        now=NOW + timedelta(seconds=1),
    )
    assert verified["status"] == "verified"
    assert verified["notification_status"] == "completed"
    assert verified["action_required"] is True
    assert verified["interrupt_operator"] is False

    repeated = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=state_path,
        lock_path=lock_path,
        receipt_path=receipt_path,
        principal_id="operator-a",
        send=True,
        now=NOW + timedelta(seconds=1),
        deliver=deliver,
    )
    assert repeated["status"] == "deduplicated"
    assert repeated["interrupt_operator"] is False
    assert repeated["delivery_authorized"] is False
    assert repeated["sent"] is False
    assert len(deliveries) == 1


def test_delivery_failure_does_not_suppress_a_future_retry(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "presentation.json"
    report = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=state_path,
        lock_path=tmp_path / "send.lock",
        receipt_path=tmp_path / "notification.json",
        principal_id="operator-a",
        send=True,
        now=NOW,
        deliver=lambda **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    assert report["status"] == "delivery_failed"
    assert report["delivery_attempted"] is True
    assert report["sent"] is False
    assert not state_path.exists()


def test_send_requires_an_explicit_principal_and_safe_pending_decision(
    tmp_path: Path,
) -> None:
    attempted = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("inadmissible authority must fail before delivery")
    )
    missing_principal = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=tmp_path / "presentation.json",
        receipt_path=tmp_path / "missing-principal.json",
        principal_id="",
        send=True,
        now=NOW,
        deliver=attempted,
    )
    assert missing_principal["status"] == "blocked"
    assert missing_principal["blocking_reason"] == "delivery_authority_incomplete"
    assert missing_principal["delivery_authorized"] is False

    unsafe_decision = {
        **_pending_decision(),
        "trust_registry_modified": True,
    }
    unsafe = notification.run_candidate_notification(
        _candidate_intake(),
        unsafe_decision,
        presentation_state_path=tmp_path / "presentation.json",
        receipt_path=tmp_path / "unsafe.json",
        principal_id="operator-a",
        send=True,
        now=NOW,
        deliver=attempted,
    )
    assert unsafe["status"] == "blocked"
    assert unsafe["delivery_authorized"] is False
    assert unsafe["trust_registry_modified"] is False


def test_resolved_confirm_allows_only_the_existing_preview_boundary(
    tmp_path: Path,
) -> None:
    resolved = {
        **_pending_decision(),
        "status": "verified",
        "review_state": "identity_verified",
        "decision": "confirm_identity_verified",
        "next_action": "stage an enrollment preview",
        "progress": {
            "current_evidence_verified": True,
            "candidate_review_verified": True,
            "candidate_decision_recorded": True,
        },
        "action_required": False,
        "operator_review_required": False,
        "identity_verification_asserted": True,
        "trust_enrollment_preview_authorized": True,
    }
    report = notification.run_candidate_notification(
        _candidate_intake(),
        resolved,
        receipt_path=tmp_path / "notification.json",
        send=True,
        now=NOW,
        deliver=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolved review must not deliver")
        ),
    )

    assert report["status"] == "resolved"
    assert report["trust_enrollment_preview_authorized"] is True
    assert report["trust_enrollment_authorized"] is False
    assert report["trust_registry_modified"] is False
    assert report["delivery_authorized"] is False


def test_tampered_or_stale_notification_receipts_fail_closed(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "notification.json"
    notification.run_candidate_notification(
        _not_required_intake(),
        _not_required_decision(),
        receipt_path=receipt_path,
        now=NOW,
    )
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    persisted["delivery_authorized"] = True

    tampered = notification.verify_notification_receipt(
        persisted,
        intake=_not_required_intake(),
        decision=_not_required_decision(),
        now=NOW,
    )
    assert tampered["status"] == "blocked"

    original = json.loads(receipt_path.read_text(encoding="utf-8"))
    stale = notification.verify_notification_receipt(
        original,
        intake=_not_required_intake(),
        decision=_not_required_decision(),
        now=NOW + timedelta(hours=1),
        max_age_seconds=60,
    )
    assert stale["status"] == "blocked"


def test_rehashed_but_unbound_operator_message_fails_closed(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "notification.json"
    report = notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=tmp_path / "presentation.json",
        receipt_path=receipt_path,
        now=NOW,
    )
    assert report["status"] == "action_required"
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    persisted["message_preview"] = "Unbound operator instruction"
    receipt_path.write_bytes(
        notification._canonical(notification._with_integrity(persisted))
    )

    verified = notification.verify_notification_receipt(
        json.loads(receipt_path.read_text(encoding="utf-8")),
        intake=_candidate_intake(),
        decision=_pending_decision(),
        presentation_state_path=tmp_path / "presentation.json",
        now=NOW + timedelta(seconds=1),
    )

    assert verified["status"] == "blocked"
    assert verified["blocking_reason"] == (
        "source_refresh_trust_candidate_notification_receipt_not_admissible"
    )
    assert verified["delivery_attempted"] is False


def test_rehashed_but_unsafe_authority_fields_fail_closed(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "notification.json"
    notification.run_candidate_notification(
        _candidate_intake(),
        _pending_decision(),
        presentation_state_path=tmp_path / "presentation.json",
        receipt_path=receipt_path,
        now=NOW,
    )
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    persisted["automatic_source_refresh_allowed"] = True
    persisted["secret_values_recorded"] = True
    receipt_path.write_bytes(
        notification._canonical(notification._with_integrity(persisted))
    )

    verified = notification.verify_notification_receipt(
        json.loads(receipt_path.read_text(encoding="utf-8")),
        intake=_candidate_intake(),
        decision=_pending_decision(),
        presentation_state_path=tmp_path / "presentation.json",
        now=NOW + timedelta(seconds=1),
    )

    assert verified["status"] == "blocked"
    assert verified["blocking_reason"] == (
        "source_refresh_trust_candidate_notification_receipt_not_admissible"
    )


def test_inspection_rebinds_the_persisted_receipt_to_current_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "notification.json"
    current_intake = _not_required_intake()
    current_decision = _not_required_decision()
    notification.run_candidate_notification(
        current_intake,
        current_decision,
        receipt_path=receipt_path,
        now=NOW,
    )
    monkeypatch.setattr(
        intake,
        "inspect_trust_intake_bundle",
        lambda **_kwargs: dict(current_intake),
    )
    monkeypatch.setattr(
        decision,
        "verify_candidate_review_decision_for_report",
        lambda *_args, **_kwargs: dict(current_decision),
    )

    report = notification.inspect_current_candidate_notification(
        receipt_path=receipt_path,
        now=NOW,
    )

    assert report["status"] == "verified"
    assert report["notification_status"] == "not_required"
    assert report["notification_receipt_sha256"]
    assert report["progress"]["candidate_review_binding_verified"] is True


def test_operator_fallback_materializes_and_reports_the_trust_alert_receipt() -> None:
    root = Path(__file__).resolve().parents[1]
    operator_summary = (root / "scripts/operator_summary.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "from scripts.propertyquarry_ooda_source_refresh_trust_notification import ("
        in operator_summary
    )
    assert (
        "source_refresh_trust_notification_path = Path(" in operator_summary
    )
    assert (
        "source_refresh_trust_notification = run_candidate_notification("
        in operator_summary
    )
    assert (
        "receipt_path=source_refresh_trust_notification_path"
        in operator_summary
    )
    record_call = operator_summary.index(
        "trust_candidate_presentation_receipt = record_candidate_presentation("
    )
    refresh_call = operator_summary.index(
        "refreshed_source_refresh_trust_notification = (",
        record_call,
    )
    assert record_call < refresh_call
    assert (
        'refreshed_source_refresh_trust_notification.get("status")\n'
        '                == "deduplicated"'
        in operator_summary[refresh_call:]
    )
    assert (
        'refreshed_source_refresh_trust_notification.get(\n'
        '                    "interrupt_operator"\n'
        "                )\n"
        "                is False"
        in operator_summary[refresh_call:]
    )
    runtime_refresh_call = operator_summary.index(
        "refreshed_runtime_trust_notification = (",
        record_call,
    )
    assert record_call < runtime_refresh_call
    assert "record_presentation: bool = False" in operator_summary
    assert '"--record-presentation"' in operator_summary
    assert '"--expected-candidate-review-id"' in operator_summary
    assert "record_presentation=True" in operator_summary[runtime_refresh_call:]
    assert (
        'refreshed_runtime_trust_notification.get(\n'
        '                    "notification_status"'
        in operator_summary[runtime_refresh_call:]
    )
    assert "send=False" in operator_summary
    assert '"trust candidate alert: "' in operator_summary
    assert '"trust alert receipt:  "' in operator_summary
