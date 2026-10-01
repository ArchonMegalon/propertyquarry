from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_source_refresh_request as refresh


NOW = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def _lane(
    lane: str,
    *,
    status: str,
    reason: str,
    source_generated_at: str,
) -> dict[str, object]:
    return {
        "lane": lane,
        "status": status,
        "reason": reason,
        "source_generated_at": source_generated_at,
        "source_artifacts": list(refresh._LANE_ARTIFACTS[lane]),
        "producer_authority": "external_receipt_producer",
        "producer_refresh_required": status != "current",
        "automatic_source_refresh_allowed": False,
    }


def _projection(
    *,
    cycle_digest: str = "a" * 64,
    gold_status: str = "stale",
    scene_status: str = "stale",
    source_offset_minutes: int = 60,
) -> dict[str, object]:
    source_generated_at = (
        NOW - timedelta(minutes=source_offset_minutes)
    ).isoformat()
    lanes = [
        _lane(
            "gold_live_runtime",
            status=gold_status,
            reason=(
                "gold_not_blocked"
                if gold_status == "current"
                else "gold_receipt_not_fresh"
            ),
            source_generated_at=source_generated_at,
        ),
        _lane(
            "scene_video_provider_refresh",
            status=scene_status,
            reason=(
                "no_actionable_provider_refresh"
                if scene_status == "current"
                else "source_receipt_not_fresh"
            ),
            source_generated_at=source_generated_at,
        ),
    ]
    counts = {
        "current": sum(row["status"] == "current" for row in lanes),
        "stale": sum(row["status"] == "stale" for row in lanes),
        "unavailable": sum(row["status"] == "unavailable" for row in lanes),
    }
    all_current = counts["current"] == len(lanes)
    blocking_reason = "" if all_current else ",".join(
        f"{row['lane']}:{row['reason']}"
        for row in lanes
        if row["status"] != "current"
    )
    return {
        "schema": operator_status.SCHEMA,
        "status": "ready" if all_current else "waiting_for_evidence",
        "updated_at": NOW.isoformat(),
        "blocking_reason": (
            "" if all_current else "approved_source_evidence_not_current"
        ),
        "source_cycle_receipt_sha256": cycle_digest,
        "progress": {"current_snapshot_hashes_verified": True},
        "signal_approval": {
            "approved": True,
            "reason": "approved_projection_manifest_verified",
            "policy": approved.POLICY,
            "manifest_generated_at": NOW.isoformat(),
        },
        "source_evidence": {
            "schema": approved.SOURCE_EVIDENCE_SCHEMA,
            "status": (
                "verified_current" if all_current else "waiting_for_fresh_sources"
            ),
            "blocking_reason": blocking_reason,
            "next_action": "producer-owned source next action",
            "recovery_mode": "none" if all_current else "await_producer_receipts",
            "progress": {
                "expected_lane_count": len(lanes),
                "current_lane_count": counts["current"],
                "stale_lane_count": counts["stale"],
                "unavailable_lane_count": counts["unavailable"],
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


def test_stale_sources_stage_one_generic_reversible_request() -> None:
    request = refresh.build_source_refresh_request(_projection(), now=NOW)

    assert request["status"] == "staged"
    assert request["request_state"] == "producer_refresh_staged"
    assert request["request_id"].startswith("pq-source-refresh-")
    assert [row["lane"] for row in request["requested_lanes"]] == [
        "gold_live_runtime",
        "scene_video_provider_refresh",
    ]
    assert all(
        row["requested_operation"] == "refresh_receipts"
        and row["producer_authority"] == "external_receipt_producer"
        and row["current_reverification_required"] is True
        for row in request["requested_lanes"]
    )
    assert request["reversible_staging"] is True
    assert request["producer_dispatch_authorized"] is False
    assert request["producer_refresh_authorized"] is False
    assert request["action_required"] is False
    assert request["interrupt_operator"] is False
    assert request["provider_quota_consumption_allowed"] is False
    assert request["delivery_authorized"] is False
    assert request["deployment_or_restart_authorized"] is False
    assert request["protected_operation_executed"] is False


def test_semantic_request_id_survives_cycle_timestamp_and_digest_churn() -> None:
    first = refresh.build_source_refresh_request(_projection(), now=NOW)
    churned_projection = _projection(cycle_digest="b" * 64)
    churned_projection["updated_at"] = (NOW + timedelta(minutes=1)).isoformat()
    churned_projection["signal_approval"]["manifest_generated_at"] = (
        NOW + timedelta(minutes=1)
    ).isoformat()
    second = refresh.build_source_refresh_request(
        churned_projection,
        now=NOW + timedelta(minutes=1),
    )

    assert first["request_id"] == second["request_id"]
    assert first["semantic_request_sha256"] == second["semantic_request_sha256"]
    assert first["source_cycle_receipt_sha256"] != second[
        "source_cycle_receipt_sha256"
    ]
    assert first != second


def test_changed_source_identity_creates_a_new_request() -> None:
    first = refresh.build_source_refresh_request(_projection(), now=NOW)
    changed = refresh.build_source_refresh_request(
        _projection(source_offset_minutes=61),
        now=NOW,
    )

    assert first["request_id"] != changed["request_id"]
    assert first["semantic_request_sha256"] != changed[
        "semantic_request_sha256"
    ]


def test_current_sources_publish_verified_not_required_tombstone() -> None:
    request = refresh.build_source_refresh_request(
        _projection(gold_status="current", scene_status="current", source_offset_minutes=1),
        now=NOW,
    )
    verification = refresh.verify_source_refresh_request(
        request,
        operator_projection=_projection(
            gold_status="current",
            scene_status="current",
            source_offset_minutes=1,
        ),
        now=NOW,
    )

    assert request["status"] == "not_required"
    assert request["request_state"] == "current_sources_verified"
    assert request["request_staged"] is False
    assert request["request_id"] == ""
    assert request["requested_lanes"] == []
    assert verification["status"] == "verified"
    assert verification["request_staged"] is False
    assert verification["interrupt_operator"] is False


def test_old_request_fails_current_source_binding_after_sources_clear() -> None:
    request = refresh.build_source_refresh_request(_projection(), now=NOW)
    verified = refresh.verify_source_refresh_request(
        request,
        operator_projection=_projection(
            gold_status="current",
            scene_status="current",
            source_offset_minutes=1,
        ),
        now=NOW,
    )

    assert verified["status"] == "blocked"
    assert verified["blocking_reason"] == (
        "source_refresh_request_source_binding_mismatch"
    )
    assert verified["action_required"] is False
    assert verified["interrupt_operator"] is False


def test_tamper_and_invalid_projection_fail_closed_without_interrupt(
    tmp_path: Path,
) -> None:
    request = refresh.build_source_refresh_request(_projection(), now=NOW)
    request["requested_lanes"][0]["reason"] = "forged"
    tampered = refresh.verify_source_refresh_request(
        request,
        operator_projection=_projection(),
        now=NOW,
    )
    invalid_projection = _projection()
    invalid_projection["progress"]["current_snapshot_hashes_verified"] = False
    blocked = refresh.materialize_source_refresh_request_bundle(
        invalid_projection,
        request_path=tmp_path / "blocked-request.json",
        verification_path=tmp_path / "blocked-verification.json",
        now=NOW,
    )

    assert tampered["status"] == "blocked"
    assert tampered["blocking_reason"] == "source_refresh_request_integrity_invalid"
    assert blocked["status"] == "blocked"
    assert blocked["request_staged"] is False
    assert blocked["request_receipt_persisted"] is True
    assert blocked["verification_receipt_persisted"] is True
    assert blocked["action_required"] is False
    assert blocked["interrupt_operator"] is False
    assert blocked["provider_quota_consumption_allowed"] is False
    assert blocked["delivery_authorized"] is False
    blocked_request = json.loads(
        (tmp_path / "blocked-request.json").read_text(encoding="utf-8")
    )
    assert blocked_request["status"] == "blocked"
    assert blocked_request["request_scope"]["effect"] == "blocked_tombstone"
    assert blocked_request["request_staged"] is False
    assert refresh._integrity_verified(blocked_request)


def test_materialized_bundle_is_private_and_integrity_bound(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"
    result = refresh.materialize_source_refresh_request_bundle(
        _projection(),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["request_staged"] is True
    assert result["request_receipt_persisted"] is True
    assert result["verification_receipt_persisted"] is True
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    persisted_request = json.loads(request_path.read_text(encoding="utf-8"))
    persisted_verification = json.loads(
        verification_path.read_text(encoding="utf-8")
    )
    request_bytes = request_path.read_bytes()
    verification_bytes = verification_path.read_bytes()
    request_mtime = request_path.stat().st_mtime_ns
    verification_mtime = verification_path.stat().st_mtime_ns
    assert refresh._integrity_verified(persisted_request)
    assert refresh._integrity_verified(persisted_verification)
    assert result["request_receipt_sha256"] == refresh._sha256(
        refresh._canonical(persisted_request)
    )
    assert persisted_verification["request_receipt_sha256"] == result[
        "request_receipt_sha256"
    ]
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False

    inspected = refresh.inspect_source_refresh_request_bundle(
        _projection(),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW + timedelta(seconds=1),
    )
    assert inspected["status"] == "verified"
    assert inspected["request_id"] == result["request_id"]
    assert request_path.read_bytes() == request_bytes
    assert verification_path.read_bytes() == verification_bytes
    assert request_path.stat().st_mtime_ns == request_mtime
    assert verification_path.stat().st_mtime_ns == verification_mtime


def test_inspect_cli_uses_read_only_path_without_materializing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}

    def inspect(**kwargs):
        observed.update(kwargs)
        return {
            "schema": refresh.VERIFY_SCHEMA,
            "status": "verified",
            "request_state": "producer_refresh_staged",
        }

    monkeypatch.setattr(
        refresh,
        "inspect_current_source_refresh_request_bundle",
        inspect,
    )
    monkeypatch.setattr(
        refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("inspect mode must not materialize")
        ),
    )
    cycle_path = tmp_path / "cycle.json"
    signal_dir = tmp_path / "signals"
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"

    result = refresh.main(
        [
            "--inspect",
            "--cycle-receipt",
            str(cycle_path),
            "--signal-dir",
            str(signal_dir),
            "--request",
            str(request_path),
            "--verification",
            str(verification_path),
        ]
    )

    assert result == 0
    assert observed["cycle_receipt_path"] == cycle_path
    assert observed["signal_dir"] == signal_dir
    assert observed["request_path"] == request_path
    assert observed["verification_path"] == verification_path
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
