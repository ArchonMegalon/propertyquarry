from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as preview


NOW = datetime(2026, 8, 27, 0, 15, tzinfo=timezone.utc)


def _registry(*, path: Path | None = None) -> dict[str, object]:
    registry = {
        "schema": preview.claims.TRUST_SCHEMA,
        "status": "UNCONFIGURED",
        "rotation_epoch": 0,
        "producers": [],
    }
    registry = preview._with_integrity(registry)
    if path is not None:
        path.write_bytes(preview._canonical(registry))
    return {
        "status": "UNCONFIGURED",
        "rotation_epoch": 0,
        "producers": [],
        "trust_registry_sha256": preview._sha256(
            preview._canonical(registry)
        ),
    }


def _intake(registry: dict[str, object]) -> dict[str, object]:
    public_key = b"p" * 32
    public_key_value = base64.urlsafe_b64encode(public_key).decode().rstrip("=")
    public_key_sha256 = preview._sha256(public_key)
    candidate = {
        "producer_id": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "algorithm": "Ed25519",
        "public_key": public_key_value,
        "public_key_sha256": public_key_sha256,
        "lanes": ["gold_live_runtime"],
        "requested_status": "ACTIVE",
        "request_id": "pqtrustintake_0123456789abcdef01234567",
        "semantic_request_sha256": "8" * 64,
        "trust_registry_sha256": registry["trust_registry_sha256"],
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "candidate_path": "/private/candidate.json",
        "candidate_receipt_sha256": "7" * 64,
        "signed_payload_sha256": "6" * 64,
        "nonce_sha256": "5" * 64,
        "proof_of_possession_verified": True,
        "out_of_band_identity_verification_required": True,
        "candidate_confers_authority": False,
        "trust_enrollment_authorized": False,
        "provider_operation_authorized": False,
        "delivery_authorized": False,
        "private_key_material_recorded": False,
    }
    return {
        "schema": preview.trust_intake.VERIFY_SCHEMA,
        "status": "verified",
        "intake_state": "candidate_ready_for_operator_review",
        "request_id": "pqtrustintake_0123456789abcdef01234567",
        "semantic_request_sha256": "8" * 64,
        "candidate_review_id": "pqtrustreview_0123456789abcdef01234567",
        "review_decision_options": list(preview.trust_decision.DECISIONS),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "candidates": [candidate],
        "candidate_coverage": {
            "covered_lanes": ["gold_live_runtime"],
            "remaining_lanes": [],
            "all_requested_lanes_covered": True,
        },
        "source_binding": {
            "trust_registry_sha256": registry["trust_registry_sha256"],
        },
        "next_action": "verify candidate identity out of band",
        "intake_receipt_sha256": "9" * 64,
        "verification_receipt_sha256": "a" * 64,
        "progress": {"candidate_evidence_count": 1},
        "action_required": True,
        "interrupt_operator": True,
        "operator_review_required": True,
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


def _decision(intake: dict[str, object]) -> dict[str, object]:
    candidate = dict(list(intake["candidates"])[0])
    receipt = preview.trust_decision.build_candidate_review_decision(
        intake,
        decision="confirm_identity_verified",
        decider_id="operator.local",
        acknowledged_public_key_sha256s=[candidate["public_key_sha256"]],
        identity_verification_method="trusted_channel",
        identity_evidence_ref="ticket:IDENTITY-123",
        now=NOW,
    )
    verified = preview.trust_decision.verify_candidate_review_decision(
        receipt,
        report=intake,
        now=NOW,
    )
    verified["decision_sha256"] = preview._sha256(
        preview._canonical(receipt)
    )
    return verified


def test_confirmation_stages_exact_valid_registry_without_authority(
    tmp_path: Path,
) -> None:
    registry = _registry()
    intake = _intake(registry)
    decision = _decision(intake)

    receipt = preview.build_enrollment_preview(
        intake,
        decision,
        registry,
        now=NOW,
    )
    verified = preview.verify_enrollment_preview(
        receipt,
        intake=intake,
        decision=decision,
        trust_registry=registry,
        now=NOW,
    )

    assert receipt["status"] == "preview_staged"
    assert receipt["preview_id"].startswith("pqtrustpreview_")
    assert receipt["current_registry"]["rotation_epoch"] == 0
    assert receipt["proposed_registry"]["status"] == "ACTIVE"
    assert receipt["proposed_registry"]["rotation_epoch"] == 1
    assert len(receipt["proposed_registry"]["producers"]) == 1
    assert receipt["authorization_scope"]["authorization_recorded"] is False
    assert verified["status"] == "verified"
    assert verified["preview_state"] == "preview_staged"
    assert verified["action_required"] is True
    assert verified["interrupt_operator"] is True
    assert verified["trust_enrollment_preview_authorized"] is True
    assert verified["trust_enrollment_authorized"] is False
    assert verified["trust_registry_modified"] is False
    assert verified["delivery_authorized"] is False
    assert verified["protected_operation_executed"] is False

    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_bytes(
        preview._canonical(receipt["proposed_registry"])
    )
    loaded = preview.claims.load_producer_trust_registry(proposed_path)
    assert loaded["status"] == "ACTIVE"
    assert loaded["rotation_epoch"] == 1
    assert loaded["trust_registry_sha256"] == receipt[
        "proposed_trust_registry_sha256"
    ]


def test_pending_and_negative_decisions_do_not_stage_registry() -> None:
    registry = _registry()
    intake = _intake(registry)
    pending = preview.trust_decision.verify_candidate_review_decision_for_report(
        intake,
        decision_dir=Path("/definitely/missing"),
        now=NOW,
    )

    pending_receipt = preview.build_enrollment_preview(
        intake,
        pending,
        registry,
        now=NOW,
    )
    assert pending_receipt["status"] == "awaiting_decision"
    assert pending_receipt["proposed_registry"] == {}
    assert pending_receipt["action_required"] is False

    rejected_receipt = preview.trust_decision.build_candidate_review_decision(
        intake,
        decision="reject_candidate",
        decider_id="operator.local",
        now=NOW,
    )
    rejected = preview.trust_decision.verify_candidate_review_decision(
        rejected_receipt,
        report=intake,
        now=NOW,
    )
    rejected["decision_sha256"] = preview._sha256(
        preview._canonical(rejected_receipt)
    )
    negative = preview.build_enrollment_preview(
        intake,
        rejected,
        registry,
        now=NOW,
    )
    assert negative["status"] == "not_required"
    assert negative["proposed_registry"] == {}
    assert negative["trust_enrollment_preview_authorized"] is False


def test_registry_or_preview_drift_fails_closed() -> None:
    registry = _registry()
    intake = _intake(registry)
    decision = _decision(intake)
    receipt = preview.build_enrollment_preview(
        intake,
        decision,
        registry,
        now=NOW,
    )

    receipt["trust_registry_modified"] = True
    invalid = preview.verify_enrollment_preview(
        receipt,
        intake=intake,
        decision=decision,
        trust_registry=registry,
        now=NOW,
    )
    assert invalid["status"] == "blocked"
    assert invalid["trust_registry_modified"] is False

    drifted = dict(registry)
    drifted["trust_registry_sha256"] = "f" * 64
    rebound = preview.verify_enrollment_preview(
        preview.build_enrollment_preview(
            intake,
            decision,
            registry,
            now=NOW,
        ),
        intake=intake,
        decision=decision,
        trust_registry=drifted,
        now=NOW,
    )
    assert rebound["status"] == "blocked"
    assert rebound["blocking_reason"] == (
        "trust_enrollment_preview_source_not_admissible"
    )


def test_materialized_preview_is_private_and_inspect_rebinds_current_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_path = tmp_path / "trust.json"
    registry = _registry(path=registry_path)
    intake = _intake(registry)
    decision = _decision(intake)
    receipt_path = tmp_path / "preview.json"
    verification_path = tmp_path / "preview-verification.json"

    materialized = preview.materialize_enrollment_preview(
        intake,
        decision,
        trust_registry_path=registry_path,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert materialized["status"] == "verified"
    assert materialized["preview_state"] == "preview_staged"
    assert materialized["preview_receipt_persisted"] is True
    assert materialized["verification_receipt_persisted"] is True
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    assert json.loads(receipt_path.read_text())["trust_registry_modified"] is False

    monkeypatch.setattr(
        preview.trust_intake,
        "inspect_trust_intake_bundle",
        lambda **_kwargs: intake,
    )
    monkeypatch.setattr(
        preview.trust_decision,
        "verify_candidate_review_decision_for_report",
        lambda *_args, **_kwargs: decision,
    )
    inspected = preview.inspect_current_enrollment_preview(
        trust_registry_path=registry_path,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert inspected["status"] == "verified"
    assert inspected["preview_id"] == materialized["preview_id"]

    tampered = json.loads(verification_path.read_text())
    tampered["trust_registry_modified"] = True
    verification_path.write_text(json.dumps(tampered))
    blocked = preview.inspect_current_enrollment_preview(
        trust_registry_path=registry_path,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert blocked["status"] == "blocked"
    assert blocked["trust_registry_modified"] is False
