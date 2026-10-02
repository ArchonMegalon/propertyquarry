from __future__ import annotations

import base64
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_handoff as handoff
from scripts import propertyquarry_ooda_source_refresh_request as refresh


NOW = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def _lane(
    lane: str,
    *,
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "lane": lane,
        "status": status,
        "reason": reason,
        "source_generated_at": (NOW - timedelta(minutes=30)).isoformat(),
        "source_artifacts": list(refresh._LANE_ARTIFACTS[lane]),
        "producer_authority": "external_receipt_producer",
        "producer_refresh_required": status != "current",
        "automatic_source_refresh_allowed": False,
    }


def _projection(*, current: bool = False) -> dict[str, object]:
    lanes = [
        _lane(
            "gold_live_runtime",
            status="current" if current else "stale",
            reason="gold_not_blocked" if current else "gold_receipt_not_fresh",
        ),
        _lane(
            "scene_video_provider_refresh",
            status="current" if current else "stale",
            reason=(
                "no_actionable_provider_refresh"
                if current
                else "source_receipt_not_fresh"
            ),
        ),
    ]
    current_count = sum(row["status"] == "current" for row in lanes)
    stale_count = sum(row["status"] == "stale" for row in lanes)
    return {
        "schema": operator_status.SCHEMA,
        "status": "ready" if current else "waiting_for_evidence",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "" if current else "approved_source_evidence_not_current",
        "source_cycle_receipt_sha256": "a" * 64,
        "progress": {"current_snapshot_hashes_verified": True},
        "signal_approval": {
            "approved": True,
            "reason": "approved_projection_manifest_verified",
            "policy": approved.POLICY,
            "manifest_generated_at": NOW.isoformat(),
        },
        "source_evidence": {
            "schema": approved.SOURCE_EVIDENCE_SCHEMA,
            "status": "verified_current" if current else "waiting_for_fresh_sources",
            "blocking_reason": "" if current else "source_receipts_stale",
            "next_action": "producer-owned source next action",
            "recovery_mode": "none" if current else "await_producer_receipts",
            "progress": {
                "expected_lane_count": 2,
                "current_lane_count": current_count,
                "stale_lane_count": stale_count,
                "unavailable_lane_count": 0,
            },
            "lanes": lanes,
            "automatic_source_refresh_allowed": False,
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "protected_operation_executed": False,
        },
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "actions": [],
    }


def _bundle(tmp_path: Path, *, current: bool = False) -> dict[str, object]:
    projection = _projection(current=current)
    request_path = tmp_path / "request.json"
    request_verification_path = tmp_path / "request-verification.json"
    handoff_path = tmp_path / "handoff.json"
    handoff_verification_path = tmp_path / "handoff-verification.json"
    request = refresh.materialize_source_refresh_request_bundle(
        projection,
        request_path=request_path,
        verification_path=request_verification_path,
        now=NOW,
    )
    assert request["status"] == "verified"
    staged = handoff.materialize_source_refresh_handoff_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=handoff_verification_path,
        now=NOW,
    )
    assert staged["status"] == "verified"
    return {
        "projection": projection,
        "request_path": request_path,
        "request_verification_path": request_verification_path,
        "handoff_path": handoff_path,
        "handoff_verification_path": handoff_verification_path,
        "handoff": json.loads(handoff_path.read_text(encoding="utf-8")),
        "receipt_path": tmp_path / "claims.json",
        "verification_path": tmp_path / "claims-verification.json",
    }


def _materialize(
    bundle: dict[str, object],
    *,
    trust_registry_path: Path,
    claim_dir: Path,
    require_claim_dir: bool = False,
) -> dict[str, object]:
    return claims.materialize_claim_lifecycle_bundle(
        bundle["projection"],
        request_path=bundle["request_path"],
        request_verification_path=bundle["request_verification_path"],
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        trust_registry_path=trust_registry_path,
        claim_dir=claim_dir,
        receipt_path=bundle["receipt_path"],
        verification_path=bundle["verification_path"],
        require_claim_dir=require_claim_dir,
        now=NOW,
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _trust_registry(
    tmp_path: Path,
) -> tuple[Path, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
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
                    "lanes": sorted(refresh._LANE_ARTIFACTS),
                    "status": "ACTIVE",
                }
            ],
        }
    )
    path = tmp_path / "trust.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path, private_key


def _claim(
    bundle: dict[str, object],
    claim_dir: Path,
    private_key: Ed25519PrivateKey,
    index: int,
    *,
    claim_id: str | None = None,
    nonce: str | None = None,
    issued_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> Path:
    handoff_payload = bundle["handoff"]
    work_item = handoff_payload["work_items"][index]
    payload = {
        "schema": claims.CLAIM_SCHEMA,
        "issuer": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "claim_id": claim_id or f"pqscr_claim_{index + 1:024x}",
        "handoff_id": handoff_payload["handoff_id"],
        "handoff_receipt_sha256": hashlib.sha256(
            bundle["handoff_path"].read_bytes()
        ).hexdigest(),
        "request_id": handoff_payload["request_id"],
        "semantic_request_sha256": handoff_payload["semantic_request_sha256"],
        "work_item_id": work_item["work_item_id"],
        "lane": work_item["lane"],
        "requested_operation": "refresh_receipts",
        "required_artifacts": work_item["required_artifacts"],
        "issued_at": issued_at.isoformat(),
        "expires_at": (expires_at or issued_at + timedelta(minutes=20)).isoformat(),
        "nonce": nonce or _b64(bytes([index + 1]) * 18),
        "claim": {
            "state": "claimed",
            "completion_receipt_required": True,
            "consumer_reverification_required": True,
            "claim_confers_authority": False,
            "provider_operation_authorized": False,
            "delivery_authorized": False,
        },
    }
    payload["signature"] = _b64(private_key.sign(claims._canonical(payload)))
    claim_dir.mkdir(parents=True, exist_ok=True)
    claim_dir.chmod(0o755)
    path = claim_dir / f"{work_item['work_item_id']}.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path


def test_unconfigured_trust_is_distinct_from_a_claimable_unclaimed_queue(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    result = _materialize(
        bundle,
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claims",
    )

    assert result["status"] == "verified"
    assert result["claim_state"] == "producer_trust_unconfigured"
    assert result["settlement_state"] == "awaiting_producer_trust"
    assert result["producer_claim_recorded"] is False
    assert result["claim_directory"]["present"] is False
    assert result["progress"]["claim_count"] == 0
    assert result["progress"]["expected_claim_count"] == 2
    assert result["progress"]["producer_trust_ready"] is False
    assert result["progress"]["active_producer_count"] == 0
    assert result["progress"]["missing_trust_lane_count"] == 2
    assert result["trust"]["status"] == "UNCONFIGURED"
    assert result["trust"]["missing_lanes"] == sorted(refresh._LANE_ARTIFACTS)
    assert "enroll ACTIVE Ed25519 producer public keys" in result["next_action"]
    assert "private keys remain producer-owned" in result["next_action"]
    assert result["interrupt_operator"] is False
    assert result["claim_confers_authority"] is False
    assert result["producer_refresh_authorized"] is False
    assert result["delivery_authorized"] is False
    assert stat.S_IMODE(bundle["receipt_path"].stat().st_mode) == 0o600
    assert stat.S_IMODE(bundle["verification_path"].stat().st_mode) == 0o600


def test_required_claim_directory_missing_fails_closed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    result = _materialize(
        bundle,
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claims",
        require_claim_dir=True,
    )

    assert result["status"] == "blocked"
    assert result["claim_state"] == "blocked"
    assert result["interrupt_operator"] is False
    assert result["producer_refresh_authorized"] is False


@pytest.mark.parametrize(
    ("claim_count", "expected_state"),
    [(1, "partially_claimed"), (2, "claimed")],
)
def test_valid_signed_claims_are_bound_and_sanitized(
    tmp_path: Path,
    claim_count: int,
    expected_state: str,
) -> None:
    bundle = _bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    for index in range(claim_count):
        _claim(bundle, claim_dir, private_key, index)

    result = _materialize(
        bundle,
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )

    assert result["status"] == "verified"
    assert result["claim_state"] == expected_state
    assert result["progress"]["claim_count"] == claim_count
    assert result["progress"]["claim_signatures_verified"] == claim_count
    assert result["producer_claim_recorded"] is True
    assert all("signature" not in row and "nonce" not in row for row in result["claims"])
    assert all(row["claim_confers_authority"] is False for row in result["claims"])
    assert result["producer_refresh_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False


@pytest.mark.parametrize("mutation", ["signature", "binding", "stale"])
def test_invalid_signature_binding_or_expiry_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    bundle = _bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    claim_path = _claim(
        bundle,
        claim_dir,
        private_key,
        0,
        issued_at=NOW - timedelta(hours=2) if mutation == "stale" else NOW,
        expires_at=NOW - timedelta(hours=1) if mutation == "stale" else None,
    )
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    if mutation == "signature":
        payload["signature"] = _b64(b"x" * 64)
    elif mutation == "binding":
        payload["handoff_receipt_sha256"] = "f" * 64
    claim_path.write_bytes(claims._canonical(payload))
    claim_path.chmod(0o644)

    result = _materialize(
        bundle,
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )

    assert result["status"] == "blocked"
    assert result["producer_claim_recorded"] is False
    assert result["claim_confers_authority"] is False
    assert result["interrupt_operator"] is False


def test_unconfigured_trust_rejects_a_present_claim(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    _claim(bundle, claim_dir, private_key, 0)

    result = _materialize(
        bundle,
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )

    assert result["status"] == "blocked"
    assert result["claim_state"] == "blocked"


def test_registry_schema_is_exactly_bound(tmp_path: Path) -> None:
    trust_path, _private_key = _trust_registry(tmp_path)
    payload = json.loads(trust_path.read_text(encoding="utf-8"))
    payload["schema"] = "propertyquarry.ooda_source_refresh_producer_trust.v0"
    payload = claims._integrity_bound(
        {key: value for key, value in payload.items() if key != "integrity"}
    )
    trust_path.write_bytes(claims._canonical(payload))
    trust_path.chmod(0o644)

    with pytest.raises(
        ValueError,
        match="source_refresh_producer_trust_not_admissible",
    ):
        claims.load_producer_trust_registry(trust_path)


def test_duplicate_claim_id_or_nonce_is_replay_and_fails_closed(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    duplicate_id = "pqscr_claim_000000000000000000000001"
    duplicate_nonce = _b64(b"r" * 18)
    _claim(
        bundle,
        claim_dir,
        private_key,
        0,
        claim_id=duplicate_id,
        nonce=duplicate_nonce,
    )
    _claim(
        bundle,
        claim_dir,
        private_key,
        1,
        claim_id=duplicate_id,
        nonce=duplicate_nonce,
    )

    result = _materialize(
        bundle,
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )

    assert result["status"] == "blocked"
    assert result["claim_state"] == "blocked"


def test_inspection_is_non_mutating_and_detects_inbox_changes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    claim_dir.mkdir(mode=0o755)
    materialized = _materialize(
        bundle,
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )
    assert materialized["status"] == "verified"
    before = [
        (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (bundle["receipt_path"], bundle["verification_path"])
    ]
    inspected = claims.inspect_claim_lifecycle_bundle(
        bundle["projection"],
        request_path=bundle["request_path"],
        request_verification_path=bundle["request_verification_path"],
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        receipt_path=bundle["receipt_path"],
        verification_path=bundle["verification_path"],
        require_claim_dir=True,
        now=NOW,
    )
    assert inspected["status"] == "verified"
    assert before == [
        (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (bundle["receipt_path"], bundle["verification_path"])
    ]

    _claim(bundle, claim_dir, private_key, 0)
    changed = claims.inspect_claim_lifecycle_bundle(
        bundle["projection"],
        request_path=bundle["request_path"],
        request_verification_path=bundle["request_verification_path"],
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        receipt_path=bundle["receipt_path"],
        verification_path=bundle["verification_path"],
        require_claim_dir=True,
        now=NOW,
    )
    assert changed["status"] == "blocked"


def test_inspection_rejects_a_claim_after_its_signature_lease_expires(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    _claim(
        bundle,
        claim_dir,
        private_key,
        0,
        expires_at=NOW + timedelta(minutes=20),
    )
    materialized = _materialize(
        bundle,
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        require_claim_dir=True,
    )
    assert materialized["status"] == "verified"

    inspected = claims.inspect_claim_lifecycle_bundle(
        bundle["projection"],
        request_path=bundle["request_path"],
        request_verification_path=bundle["request_verification_path"],
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        receipt_path=bundle["receipt_path"],
        verification_path=bundle["verification_path"],
        require_claim_dir=True,
        now=NOW + timedelta(minutes=21),
    )

    assert inspected["status"] == "blocked"
    assert inspected["producer_claim_recorded"] is False
    assert inspected["claim_confers_authority"] is False


def test_current_sources_settle_without_attributing_a_producer_claim(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, current=True)
    result = _materialize(
        bundle,
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claims",
    )

    assert result["status"] == "verified"
    assert result["claim_state"] == "not_required"
    assert result["settlement_state"] == "current_evidence_verified"
    assert result["producer_claim_recorded"] is False
    assert result["claims"] == []
    assert result["progress"]["expected_claim_count"] == 0


def test_inspect_cli_never_materializes(monkeypatch, tmp_path: Path, capsys) -> None:
    observed: dict[str, object] = {}

    def inspect(**kwargs):
        observed.update(kwargs)
        return {
            "schema": claims.VERIFY_SCHEMA,
            "status": "verified",
            "claim_state": "unclaimed",
        }

    monkeypatch.setattr(
        claims,
        "inspect_current_claim_lifecycle_bundle",
        inspect,
    )
    monkeypatch.setattr(
        claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("inspect must not materialize")
        ),
    )
    result = claims.main(
        [
            "--inspect",
            "--cycle-receipt",
            str(tmp_path / "cycle"),
            "--claim-dir",
            str(tmp_path / "claims"),
            "--require-claim-dir",
        ]
    )

    assert result == 0
    assert observed["require_claim_dir"] is True
    assert observed["claim_dir"] == tmp_path / "claims"
    assert json.loads(capsys.readouterr().out)["claim_state"] == "unclaimed"
