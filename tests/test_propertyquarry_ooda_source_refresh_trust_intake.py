from __future__ import annotations

import base64
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts import propertyquarry_ooda_source_refresh_trust_intake as intake


NOW = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _active_trust_registry(tmp_path: Path) -> Path:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = claims._integrity_bound(
        {
            "schema": claims.TRUST_SCHEMA,
            "status": "ACTIVE",
            "rotation_epoch": 1,
            "producers": [
                {
                    "producer_id": "receipt-producer.test",
                    "key_id": "receipt-producer.test.2026-08",
                    "algorithm": "Ed25519",
                    "public_key": _b64(public_key),
                    "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
                    "lanes": sorted(source_refresh._LANE_ARTIFACTS),
                    "status": "ACTIVE",
                }
            ],
        }
    )
    path = tmp_path / "trust.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path


def _claim_verification(
    trust_registry: dict[str, object],
    *,
    trust_ready: bool,
) -> dict[str, object]:
    lanes = sorted(source_refresh._LANE_ARTIFACTS)
    payload = {
        "schema": claims.VERIFY_SCHEMA,
        "status": "verified",
        "claim_state": (
            "unclaimed" if trust_ready else "producer_trust_unconfigured"
        ),
        "settlement_state": (
            "awaiting_current_evidence"
            if trust_ready
            else "awaiting_producer_trust"
        ),
        "updated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        "handoff_id": "pq-source-refresh-handoff-test",
        "request_id": "pq-source-refresh-test",
        "lifecycle_receipt_sha256": "a" * 64,
        "trust": {
            "status": trust_registry["status"],
            "rotation_epoch": trust_registry["rotation_epoch"],
            "trust_registry_sha256": trust_registry[
                "trust_registry_sha256"
            ],
            "configured": trust_ready,
            "active_producer_count": 1 if trust_ready else 0,
            "required_lanes": lanes,
            "trusted_lanes": lanes if trust_ready else [],
            "missing_lanes": [] if trust_ready else lanes,
            "producer_trust_ready": trust_ready,
        },
        "progress": {
            "current_evidence_verified": True,
            "handoff_binding_verified": True,
            "lifecycle_integrity_verified": True,
        },
        **claims._safety_fields(),
    }
    return claims._integrity_bound(payload)


def _write_claim_verification(
    tmp_path: Path,
    trust_registry_path: Path,
    *,
    trust_ready: bool,
) -> Path:
    registry = claims.load_producer_trust_registry(trust_registry_path)
    payload = _claim_verification(registry, trust_ready=trust_ready)
    path = tmp_path / "claims-verification.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o600)
    return path


def _write_candidate(
    candidate_dir: Path,
    request: dict[str, object],
    *,
    lanes: list[str] | None = None,
    request_id: str | None = None,
    corrupt_signature: bool = False,
) -> Path:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_sha256 = hashlib.sha256(public_key).hexdigest()
    payload = {
        "schema": intake.CANDIDATE_SCHEMA,
        "producer_id": "receipt-producer.candidate",
        "key_id": "receipt-producer.candidate.2026-08",
        "algorithm": "Ed25519",
        "public_key": _b64(public_key),
        "public_key_sha256": public_key_sha256,
        "lanes": sorted(lanes or source_refresh._LANE_ARTIFACTS),
        "requested_status": "ACTIVE",
        "request_id": request_id or request["request_id"],
        "semantic_request_sha256": request["semantic_request_sha256"],
        "trust_registry_sha256": request["source_binding"][
            "trust_registry_sha256"
        ],
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        "nonce": _b64(b"candidate-proof-01"),
        "candidate": {
            "state": "proposed",
            "producer_controls_private_key": True,
            "out_of_band_identity_verification_required": True,
            "candidate_confers_authority": False,
            "trust_enrollment_authorized": False,
            "provider_operation_authorized": False,
            "delivery_authorized": False,
            "private_key_material_included": False,
        },
    }
    signature = private_key.sign(claims._canonical(payload))
    payload["proof_signature"] = _b64(
        b"x" * 64 if corrupt_signature else signature
    )
    candidate_dir.mkdir(mode=0o755, exist_ok=True)
    path = candidate_dir / f"{public_key_sha256}.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path


def test_missing_public_key_trust_stages_non_authoritative_intake(
    tmp_path: Path,
) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    receipt_path = tmp_path / "trust-intake.json"
    verification_path = tmp_path / "trust-intake-verification.json"

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=tmp_path / "public-key-candidates",
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["request_status"] == "staged"
    assert result["intake_state"] == "awaiting_producer_public_key_evidence"
    assert result["request_staged"] is True
    assert result["requested_lanes"] == sorted(source_refresh._LANE_ARTIFACTS)
    assert result["candidate_requirements"]["algorithm"] == "Ed25519"
    assert result["candidate_requirements"]["private_key_material_allowed"] is False
    assert result["candidate_requirements"][
        "out_of_band_identity_verification_required"
    ] is True
    assert result["progress"]["candidate_evidence_count"] == 0
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["public_key_candidate_recorded"] is False
    assert result["private_key_material_requested"] is False
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False
    assert result["producer_dispatch_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    inspected = intake.inspect_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=tmp_path / "public-key-candidates",
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert inspected["status"] == "verified"
    assert inspected["intake_receipt_sha256"] == result["intake_receipt_sha256"]


def test_request_identity_survives_equivalent_fresh_claim_receipts(
    tmp_path: Path,
) -> None:
    trust_registry = claims.load_producer_trust_registry(
        claims.DEFAULT_TRUST_REGISTRY_PATH
    )
    first_claim = _claim_verification(trust_registry, trust_ready=False)
    first = intake.build_trust_intake(
        first_claim,
        claim_verification_receipt_sha256="1" * 64,
        trust_registry=trust_registry,
        candidate_dir=tmp_path / "public-key-candidates",
        now=NOW,
    )
    _write_candidate(tmp_path / "public-key-candidates", first)

    refreshed_claim = dict(first_claim)
    refreshed_claim.pop("integrity")
    refreshed_claim.update(
        {
            "updated_at": (NOW + timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=21)).isoformat(),
            "lifecycle_receipt_sha256": "b" * 64,
        }
    )
    refreshed_claim = claims._integrity_bound(refreshed_claim)
    refreshed = intake.build_trust_intake(
        refreshed_claim,
        claim_verification_receipt_sha256="2" * 64,
        trust_registry=trust_registry,
        candidate_dir=tmp_path / "public-key-candidates",
        now=NOW + timedelta(minutes=1),
    )

    assert refreshed["request_id"] == first["request_id"]
    assert (
        refreshed["semantic_request_sha256"]
        == first["semantic_request_sha256"]
    )
    assert (
        refreshed["source_binding"]["claim_verification_receipt_sha256"]
        != first["source_binding"]["claim_verification_receipt_sha256"]
    )
    assert refreshed["intake_state"] == "candidate_ready_for_operator_review"
    assert refreshed["progress"]["candidate_evidence_count"] == 1


def test_ready_trust_does_not_stage_an_intake_request(tmp_path: Path) -> None:
    trust_path = _active_trust_registry(tmp_path)
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=True,
    )

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=tmp_path / "public-key-candidates",
        receipt_path=tmp_path / "trust-intake.json",
        verification_path=tmp_path / "trust-intake-verification.json",
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["request_status"] == "not_required"
    assert result["intake_state"] == "not_required"
    assert result["request_staged"] is False
    assert result["requested_lanes"] == []
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False


def test_valid_proof_of_possession_stages_genuine_operator_review(
    tmp_path: Path,
) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    candidate_dir = tmp_path / "public-key-candidates"
    receipt_path = tmp_path / "trust-intake.json"
    verification_path = tmp_path / "trust-intake-verification.json"
    request = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert request["intake_state"] == "awaiting_producer_public_key_evidence"
    _write_candidate(candidate_dir, request)

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["intake_state"] == "candidate_ready_for_operator_review"
    assert result["candidate_review_id"].startswith("pqtrustreview_")
    assert result["review_decision_options"] == [
        "confirm_identity_verified",
        "reject_candidate",
        "defer",
    ]
    assert result["progress"]["candidate_evidence_count"] == 1
    assert result["progress"]["candidate_proof_signature_count"] == 1
    assert result["candidate_coverage"]["all_requested_lanes_covered"] is True
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["operator_review_required"] is True
    assert result["public_key_candidate_recorded"] is True
    assert result["candidates"][0]["proof_of_possession_verified"] is True
    assert "proof_signature" not in result["candidates"][0]
    assert "nonce" not in result["candidates"][0]
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    presentation_path = tmp_path / "candidate-presentation.json"
    novel = intake.apply_candidate_presentation_state(
        result,
        state_path=presentation_path,
    )
    assert novel["interrupt_operator"] is True
    assert novel["presentation"]["state"] == "novel"
    recorded = intake.record_candidate_presentation(
        result,
        state_path=presentation_path,
        expected_candidate_review_id=result["candidate_review_id"],
        now=NOW,
    )
    assert recorded["status"] == "recorded"
    assert recorded["trust_registry_modified"] is False
    assert stat.S_IMODE(presentation_path.stat().st_mode) == 0o600
    repeated = intake.apply_candidate_presentation_state(
        result,
        state_path=presentation_path,
    )
    assert repeated["action_required"] is True
    assert repeated["interrupt_operator"] is False
    assert repeated["presentation"]["state"] == "already_presented"


def test_invalid_candidate_proof_fails_closed(tmp_path: Path) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    candidate_dir = tmp_path / "public-key-candidates"
    receipt_path = tmp_path / "trust-intake.json"
    verification_path = tmp_path / "trust-intake-verification.json"
    request = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    _write_candidate(candidate_dir, request, corrupt_signature=True)

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False


def test_valid_candidate_for_prior_request_is_non_current_and_silent(
    tmp_path: Path,
) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    candidate_dir = tmp_path / "public-key-candidates"
    receipt_path = tmp_path / "trust-intake.json"
    verification_path = tmp_path / "trust-intake-verification.json"
    request = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    _write_candidate(
        candidate_dir,
        request,
        request_id="pqtrustintake_000000000000000000000000",
    )

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        candidate_dir=candidate_dir,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["intake_state"] == "awaiting_producer_public_key_evidence"
    assert result["progress"]["candidate_evidence_count"] == 0
    assert result["progress"]["non_current_candidate_file_count"] == 1
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False


def test_tampered_claim_verification_fails_closed(tmp_path: Path) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload.pop("integrity")
    payload["trust"]["missing_lanes"] = ["gold_live_runtime"]
    claim_path.write_bytes(claims._canonical(claims._integrity_bound(payload)))
    claim_path.chmod(0o600)

    result = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        receipt_path=tmp_path / "trust-intake.json",
        verification_path=tmp_path / "trust-intake-verification.json",
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["intake_state"] == "blocked"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["trust_registry_modified"] is False


def test_tampered_intake_receipt_fails_closed_on_inspection(tmp_path: Path) -> None:
    trust_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    claim_path = _write_claim_verification(
        tmp_path,
        trust_path,
        trust_ready=False,
    )
    receipt_path = tmp_path / "trust-intake.json"
    verification_path = tmp_path / "trust-intake-verification.json"
    materialized = intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert materialized["status"] == "verified"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["requested_lanes"] = ["gold_live_runtime"]
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path.chmod(0o600)

    result = intake.inspect_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_path,
        receipt_path=receipt_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["trust_registry_modified"] is False
