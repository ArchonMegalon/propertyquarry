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
from scripts import propertyquarry_ooda_source_refresh_settlement as settlement


NOW = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
NEXT = NOW + timedelta(minutes=10)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _projection(*, current: bool, at: datetime) -> dict[str, object]:
    lanes = []
    for lane in refresh._LANE_ARTIFACTS:
        lanes.append(
            {
                "lane": lane,
                "status": "current" if current else "stale",
                "reason": (
                    "no_actionable_provider_refresh"
                    if current
                    else "source_receipt_not_fresh"
                ),
                "source_generated_at": (at - timedelta(minutes=1)).isoformat(),
                "source_artifacts": list(refresh._LANE_ARTIFACTS[lane]),
                "producer_authority": "external_receipt_producer",
                "producer_refresh_required": not current,
                "automatic_source_refresh_allowed": False,
            }
        )
    return {
        "schema": operator_status.SCHEMA,
        "status": "ready" if current else "waiting_for_evidence",
        "updated_at": at.isoformat(),
        "blocking_reason": "" if current else "approved_source_evidence_not_current",
        "source_cycle_receipt_sha256": ("b" if at == NEXT else "a") * 64,
        "progress": {"current_snapshot_hashes_verified": True},
        "signal_approval": {
            "approved": True,
            "reason": "approved_projection_manifest_verified",
            "policy": approved.POLICY,
            "manifest_generated_at": at.isoformat(),
        },
        "source_evidence": {
            "schema": approved.SOURCE_EVIDENCE_SCHEMA,
            "status": "verified_current" if current else "waiting_for_fresh_sources",
            "blocking_reason": "" if current else "source_receipts_stale",
            "next_action": "producer-owned source next action",
            "recovery_mode": "none" if current else "await_producer_receipts",
            "progress": {
                "expected_lane_count": 2,
                "current_lane_count": 2 if current else 0,
                "stale_lane_count": 0 if current else 2,
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


def _signals(tmp_path: Path, *, at: datetime) -> tuple[Path, dict[str, str]]:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o755, exist_ok=True)
    rows: dict[str, dict[str, object]] = {}
    digests: dict[str, str] = {}
    for index, (artifact, filename) in enumerate(approved.SIGNAL_FILENAMES.items()):
        raw = approved.canonical_json_bytes(
            {
                "artifact": artifact,
                "generation": at.isoformat(),
                "value": index,
            }
        )
        path = signal_dir / filename
        path.write_bytes(raw)
        path.chmod(0o644)
        digest = hashlib.sha256(raw).hexdigest()
        digests[artifact] = digest
        rows[artifact] = {
            "filename": filename,
            "sha256": digest,
            "bytes": len(raw),
            "source_sha256": digest,
            "source_generated_at": at.isoformat(),
            "source_contract": f"test.{artifact}.v1",
        }
    manifest = {
        "schema": approved.SCHEMA,
        "generated_at": at.isoformat(),
        "status": "approved_projection",
        "approval": {
            "decision": "approved_projection",
            "authority": "operator_owned_read_only_ingress",
            "policy": approved.POLICY,
            "notification_policy": "action_required_only",
            "automatic_execution_allowed": False,
            "provider_quota_consumption_allowed": False,
            "protected_operations": approved.PROTECTED_OPERATIONS,
        },
        "publication": {
            "commit_file": "manifest.json",
            "commit_order": "signals_then_manifest",
        },
        "signals": rows,
    }
    manifest_path = signal_dir / "manifest.json"
    manifest_path.write_bytes(approved.canonical_json_bytes(manifest))
    manifest_path.chmod(0o644)
    return signal_dir, digests


def _trust_registry(tmp_path: Path) -> tuple[Path, Ed25519PrivateKey]:
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


def _prior_bundle(tmp_path: Path) -> dict[str, object]:
    projection = _projection(current=False, at=NOW)
    request_path = tmp_path / "request.json"
    request_verification_path = tmp_path / "request-verification.json"
    handoff_path = tmp_path / "handoff.json"
    handoff_verification_path = tmp_path / "handoff-verification.json"
    assert refresh.materialize_source_refresh_request_bundle(
        projection,
        request_path=request_path,
        verification_path=request_verification_path,
        now=NOW,
    )["status"] == "verified"
    assert handoff.materialize_source_refresh_handoff_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=handoff_verification_path,
        now=NOW,
    )["status"] == "verified"
    return {
        "projection": projection,
        "handoff_path": handoff_path,
        "handoff_verification_path": handoff_verification_path,
        "handoff": json.loads(handoff_path.read_text(encoding="utf-8")),
        "claim_lifecycle_path": tmp_path / "claims.json",
        "claim_verification_path": tmp_path / "claims-verification.json",
        "request_path": request_path,
        "request_verification_path": request_verification_path,
    }


def _claim(
    bundle: dict[str, object],
    claim_dir: Path,
    private_key: Ed25519PrivateKey,
    index: int,
) -> Path:
    handoff_payload = bundle["handoff"]
    work_item = handoff_payload["work_items"][index]
    payload = {
        "schema": claims.CLAIM_SCHEMA,
        "issuer": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "claim_id": f"pqscr_claim_{index + 1:024x}",
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
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        "nonce": _b64(bytes([index + 1]) * 18),
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
    claim_dir.mkdir(mode=0o755, exist_ok=True)
    path = claim_dir / f"{work_item['work_item_id']}.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path


def _materialize_claims(
    bundle: dict[str, object],
    *,
    trust_path: Path,
    claim_dir: Path,
) -> None:
    result = claims.materialize_claim_lifecycle_bundle(
        bundle["projection"],
        request_path=bundle["request_path"],
        request_verification_path=bundle["request_verification_path"],
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=claim_dir,
        receipt_path=bundle["claim_lifecycle_path"],
        verification_path=bundle["claim_verification_path"],
        require_claim_dir=True,
        now=NOW,
    )
    assert result["status"] == "verified"


def _completion(
    bundle: dict[str, object],
    completion_dir: Path,
    private_key: Ed25519PrivateKey,
    digests: dict[str, str],
    index: int,
    *,
    wrong_digest: bool = False,
    corrupt_signature: bool = False,
    completion_id: str | None = None,
    nonce: str | None = None,
    completed_at: datetime | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Path:
    handoff_payload = bundle["handoff"]
    work_item = handoff_payload["work_items"][index]
    claim_path = Path(bundle["claim_dir"]) / f"{work_item['work_item_id']}.json"
    claim_payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload = {
        "schema": settlement.COMPLETION_SCHEMA,
        "issuer": "receipt-producer.test",
        "key_id": "receipt-producer.test.2026-08",
        "completion_id": completion_id or f"pqsrr_completion_{index + 1:024x}",
        "claim_id": claim_payload["claim_id"],
        "claim_receipt_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest(),
        "handoff_id": handoff_payload["handoff_id"],
        "handoff_receipt_sha256": hashlib.sha256(
            bundle["handoff_path"].read_bytes()
        ).hexdigest(),
        "request_id": handoff_payload["request_id"],
        "semantic_request_sha256": handoff_payload["semantic_request_sha256"],
        "work_item_id": work_item["work_item_id"],
        "lane": work_item["lane"],
        "completed_operation": "refresh_receipts",
        "artifact_receipts": [
            {
                "artifact": artifact,
                "approved_signal_sha256": (
                    "f" * 64 if wrong_digest and artifact == work_item["required_artifacts"][0]
                    else digests[artifact]
                ),
            }
            for artifact in work_item["required_artifacts"]
        ],
        "completed_at": (completed_at or NOW + timedelta(minutes=8)).isoformat(),
        "issued_at": (issued_at or NOW + timedelta(minutes=9)).isoformat(),
        "expires_at": (expires_at or NOW + timedelta(minutes=29)).isoformat(),
        "nonce": nonce or _b64(bytes([index + 11]) * 18),
        "completion": {
            "state": "completed",
            "consumer_reverification_required": True,
            "completion_confers_authority": False,
            "provider_operation_authorized": False,
            "delivery_authorized": False,
        },
    }
    payload["signature"] = _b64(private_key.sign(claims._canonical(payload)))
    if corrupt_signature:
        payload["signature"] = _b64(b"x" * 64)
    completion_dir.mkdir(mode=0o755, exist_ok=True)
    path = completion_dir / f"{work_item['work_item_id']}.json"
    path.write_bytes(claims._canonical(payload))
    path.chmod(0o644)
    return path


def _settle(
    bundle: dict[str, object],
    *,
    projection: dict[str, object],
    signal_dir: Path,
    trust_path: Path,
    completion_dir: Path,
) -> dict[str, object]:
    return settlement.materialize_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        claim_lifecycle_path=bundle["claim_lifecycle_path"],
        claim_verification_path=bundle["claim_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=bundle["claim_dir"],
        completion_dir=completion_dir,
        receipt_path=bundle["settlement_path"],
        verification_path=bundle["settlement_verification_path"],
        require_claim_dir=True,
        require_completion_dir=True,
        now=NEXT,
    )


def _prepared(
    tmp_path: Path,
    *,
    claim_count: int,
) -> tuple[dict[str, object], Path, Ed25519PrivateKey]:
    bundle = _prior_bundle(tmp_path)
    trust_path, private_key = _trust_registry(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    claim_dir.mkdir(mode=0o755)
    bundle["claim_dir"] = claim_dir
    bundle["settlement_path"] = tmp_path / "settlement.json"
    bundle["settlement_verification_path"] = tmp_path / "settlement-verification.json"
    for index in range(claim_count):
        _claim(bundle, claim_dir, private_key, index)
    _materialize_claims(bundle, trust_path=trust_path, claim_dir=claim_dir)
    return bundle, trust_path, private_key


def test_no_prior_work_is_verified_without_attribution(tmp_path: Path) -> None:
    projection = _projection(current=False, at=NEXT)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    result = settlement.materialize_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        handoff_path=tmp_path / "absent-handoff.json",
        handoff_verification_path=tmp_path / "absent-handoff-verification.json",
        claim_lifecycle_path=tmp_path / "absent-claims.json",
        claim_verification_path=tmp_path / "absent-claims-verification.json",
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claim-dir",
        completion_dir=completion_dir,
        receipt_path=tmp_path / "settlement.json",
        verification_path=tmp_path / "settlement-verification.json",
        require_completion_dir=True,
        now=NEXT,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "no_prior_work"
    assert result["settlement_attributed"] is False
    assert result["producer_completion_recorded"] is False


def test_current_operator_action_does_not_block_source_settlement(
    tmp_path: Path,
) -> None:
    projection = _projection(current=True, at=NEXT)
    projection["status"] = "action_required"
    projection["actions"] = [
        {
            "lane": "gold_live_runtime",
            "reason": "live_runtime_host_admission_rejected",
        }
    ]
    signal_dir, _digests = _signals(tmp_path, at=NEXT)

    result = settlement.materialize_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        handoff_path=tmp_path / "absent-handoff.json",
        handoff_verification_path=tmp_path / "absent-handoff-verification.json",
        claim_lifecycle_path=tmp_path / "absent-claims.json",
        claim_verification_path=tmp_path / "absent-claims-verification.json",
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claim-dir",
        completion_dir=tmp_path / "absent-completion-dir",
        receipt_path=tmp_path / "settlement.json",
        verification_path=tmp_path / "settlement-verification.json",
        now=NEXT,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "no_prior_work"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["producer_dispatch_authorized"] is False
    assert result["delivery_authorized"] is False


def test_prior_unclaimed_work_remains_unclaimed(tmp_path: Path) -> None:
    bundle, trust_path, _private_key = _prepared(tmp_path, claim_count=0)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    result = _settle(
        bundle,
        projection=_projection(current=False, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "unclaimed"
    assert result["progress"]["unclaimed_count"] == 2
    assert result["settlement_attributed"] is False


def test_unconfigured_trust_is_an_explicit_non_authoritative_settlement_state(
    tmp_path: Path,
) -> None:
    bundle = _prior_bundle(tmp_path)
    claim_dir = tmp_path / "claims-inbox"
    claim_dir.mkdir(mode=0o755)
    bundle["claim_dir"] = claim_dir
    bundle["settlement_path"] = tmp_path / "settlement.json"
    bundle["settlement_verification_path"] = (
        tmp_path / "settlement-verification.json"
    )
    _materialize_claims(
        bundle,
        trust_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=claim_dir,
    )
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)

    result = _settle(
        bundle,
        projection=_projection(current=False, at=NEXT),
        signal_dir=signal_dir,
        trust_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "producer_trust_unconfigured"
    assert result["settlement_attributed"] is False
    assert result["producer_completion_recorded"] is False
    assert result["trust"]["producer_trust_ready"] is False
    assert result["trust"]["active_producer_count"] == 0
    assert result["trust"]["missing_lanes"] == sorted(refresh._LANE_ARTIFACTS)
    assert "enroll ACTIVE Ed25519 producer public keys" in result["next_action"]
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["execution_authorized"] is False


def test_expired_unclaimed_prior_chain_can_be_safely_superseded(
    tmp_path: Path,
) -> None:
    bundle, trust_path, _private_key = _prepared(tmp_path, claim_count=0)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    result = settlement.materialize_settlement_bundle(
        _projection(current=False, at=NEXT),
        signal_dir=signal_dir,
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        claim_lifecycle_path=bundle["claim_lifecycle_path"],
        claim_verification_path=bundle["claim_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=bundle["claim_dir"],
        completion_dir=completion_dir,
        receipt_path=bundle["settlement_path"],
        verification_path=bundle["settlement_verification_path"],
        require_claim_dir=True,
        require_completion_dir=True,
        now=NEXT,
        max_age_seconds=60,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "unclaimed"
    assert result["progress"]["expected_work_item_count"] == 2


def test_signed_completion_waits_for_current_consumer_evidence(tmp_path: Path) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=1)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    _completion(bundle, completion_dir, private_key, digests, 0)
    result = _settle(
        bundle,
        projection=_projection(current=False, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "completion_verified_awaiting_current_evidence"
    assert result["progress"]["completion_signature_count"] == 1
    assert result["settlement_attributed"] is False


def test_all_signed_completions_settle_only_against_exact_current_hashes(
    tmp_path: Path,
) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=2)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    for index in range(2):
        _completion(bundle, completion_dir, private_key, digests, index)
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "settled_attributed"
    assert result["progress"]["settled_count"] == 2
    assert result["settlement_attributed"] is True
    assert all(row["settlement_attributed"] for row in result["settlements"])
    assert result["execution_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert stat.S_IMODE(Path(bundle["settlement_path"]).stat().st_mode) == 0o600


def test_current_evidence_without_completion_is_explicitly_unattributed(
    tmp_path: Path,
) -> None:
    bundle, trust_path, _private_key = _prepared(tmp_path, claim_count=1)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "current_evidence_verified_unattributed"
    assert result["progress"]["current_evidence_unattributed_count"] == 2
    assert result["settlement_attributed"] is False


@pytest.mark.parametrize("mutation", ["digest", "signature"])
def test_wrong_artifact_digest_or_signature_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=1)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    _completion(
        bundle,
        completion_dir,
        private_key,
        digests,
        0,
        wrong_digest=mutation == "digest",
        corrupt_signature=mutation == "signature",
    )
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "blocked"
    assert result["settlement_state"] == "blocked"
    assert result["settlement_attributed"] is False


def test_completion_without_recorded_claim_fails_closed(tmp_path: Path) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=0)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    claim_path = _claim(bundle, bundle["claim_dir"], private_key, 0)
    completion_dir = tmp_path / "completions"
    _completion(bundle, completion_dir, private_key, digests, 0)
    claim_path.unlink()
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "blocked"
    assert result["settlement_state"] == "blocked"


def test_claim_arriving_after_prior_lifecycle_is_reverified_not_discarded(
    tmp_path: Path,
) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=0)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    _claim(bundle, bundle["claim_dir"], private_key, 0)
    completion_dir = tmp_path / "completions"
    _completion(bundle, completion_dir, private_key, digests, 0)
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "verified"
    assert result["progress"]["completion_signature_count"] == 1
    assert result["progress"]["settled_count"] == 1
    assert any(
        row["settlement_state"] == "settled_attributed"
        for row in result["settlements"]
    )


def test_inspection_is_non_mutating_and_detects_completion_inbox_change(
    tmp_path: Path,
) -> None:
    bundle, trust_path, _private_key = _prepared(tmp_path, claim_count=0)
    projection = _projection(current=False, at=NEXT)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    materialized = _settle(
        bundle,
        projection=projection,
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )
    assert materialized["status"] == "verified"
    targets = (
        Path(bundle["settlement_path"]),
        Path(bundle["settlement_verification_path"]),
    )
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in targets]
    inspected = settlement.inspect_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        trust_registry_path=trust_path,
        completion_dir=completion_dir,
        receipt_path=bundle["settlement_path"],
        verification_path=bundle["settlement_verification_path"],
        require_completion_dir=True,
        now=NEXT,
    )
    after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in targets]
    assert inspected["status"] == "verified"
    assert before == after

    work_item_id = bundle["handoff"]["work_items"][0]["work_item_id"]
    changed = completion_dir / f"{work_item_id}.json"
    changed.write_text("{}\n", encoding="utf-8")
    changed.chmod(0o644)
    rejected = settlement.inspect_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        trust_registry_path=trust_path,
        completion_dir=completion_dir,
        receipt_path=bundle["settlement_path"],
        verification_path=bundle["settlement_verification_path"],
        require_completion_dir=True,
        now=NEXT,
    )
    assert rejected["status"] == "blocked"


def test_duplicate_completion_id_and_nonce_are_replay_and_fail_closed(
    tmp_path: Path,
) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=2)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    duplicate_id = "pqsrr_completion_000000000000000000000001"
    duplicate_nonce = _b64(b"r" * 18)
    for index in range(2):
        _completion(
            bundle,
            completion_dir,
            private_key,
            digests,
            index,
            completion_id=duplicate_id,
            nonce=duplicate_nonce,
        )
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "blocked"
    assert result["settlement_attributed"] is False


def test_expired_completion_lease_fails_closed(tmp_path: Path) -> None:
    bundle, trust_path, private_key = _prepared(tmp_path, claim_count=1)
    signal_dir, digests = _signals(tmp_path, at=NEXT)
    completion_dir = tmp_path / "completions"
    _completion(
        bundle,
        completion_dir,
        private_key,
        digests,
        0,
        completed_at=NOW + timedelta(minutes=6),
        issued_at=NOW + timedelta(minutes=7),
        expires_at=NOW + timedelta(minutes=9),
    )
    result = _settle(
        bundle,
        projection=_projection(current=True, at=NEXT),
        signal_dir=signal_dir,
        trust_path=trust_path,
        completion_dir=completion_dir,
    )

    assert result["status"] == "blocked"
    assert result["producer_completion_recorded"] is False


def test_verified_revocation_is_honest_absence_of_current_source_evidence(
    tmp_path: Path,
) -> None:
    projection = _projection(current=False, at=NEXT)
    projection["signal_approval"] = {
        "approved": False,
        "reason": "approved_projection_manifest_revoked",
        "revocation_verified": True,
        "policy": approved.POLICY,
        "manifest_generated_at": NEXT.isoformat(),
    }
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o755)
    manifest = {
        "schema": approved.REVOCATION_SCHEMA,
        "generated_at": NEXT.isoformat(),
        "status": "revoked",
        "reason": "producer_source_unavailable",
        "revocation": {
            "decision": "revoked",
            "authority": "operator_owned_read_only_ingress",
            "policy": approved.POLICY,
            "notification_policy": "action_required_only",
        },
    }
    manifest_path = signal_dir / "manifest.json"
    manifest_path.write_bytes(approved.canonical_json_bytes(manifest))
    manifest_path.chmod(0o644)
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir(mode=0o755)
    result = settlement.materialize_settlement_bundle(
        projection,
        signal_dir=signal_dir,
        handoff_path=tmp_path / "absent-handoff.json",
        handoff_verification_path=tmp_path / "absent-handoff-v.json",
        claim_lifecycle_path=tmp_path / "absent-claims.json",
        claim_verification_path=tmp_path / "absent-claims-v.json",
        trust_registry_path=claims.DEFAULT_TRUST_REGISTRY_PATH,
        claim_dir=tmp_path / "absent-claims",
        completion_dir=completion_dir,
        receipt_path=tmp_path / "settlement.json",
        verification_path=tmp_path / "settlement-v.json",
        require_completion_dir=True,
        now=NEXT,
    )

    assert result["status"] == "verified"
    assert result["settlement_state"] == "no_prior_work"
    assert result["source_manifest_state"] == "revoked"
    assert result["settlement_attributed"] is False


def test_required_external_directories_fail_closed(tmp_path: Path) -> None:
    bundle, trust_path, _private_key = _prepared(tmp_path, claim_count=0)
    signal_dir, _digests = _signals(tmp_path, at=NEXT)
    result = settlement.materialize_settlement_bundle(
        _projection(current=False, at=NEXT),
        signal_dir=signal_dir,
        handoff_path=bundle["handoff_path"],
        handoff_verification_path=bundle["handoff_verification_path"],
        claim_lifecycle_path=bundle["claim_lifecycle_path"],
        claim_verification_path=bundle["claim_verification_path"],
        trust_registry_path=trust_path,
        claim_dir=tmp_path / "missing-claims",
        completion_dir=tmp_path / "missing-completions",
        receipt_path=bundle["settlement_path"],
        verification_path=bundle["settlement_verification_path"],
        require_claim_dir=True,
        require_completion_dir=True,
        now=NEXT,
    )
    assert result["status"] == "blocked"
    assert result["interrupt_operator"] is False
