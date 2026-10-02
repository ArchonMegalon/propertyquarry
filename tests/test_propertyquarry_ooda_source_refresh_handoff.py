from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
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


def _source_bundle(tmp_path: Path, *, current: bool = False) -> tuple[Path, Path, dict[str, object]]:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "request-verification.json"
    result = refresh.materialize_source_refresh_request_bundle(
        _projection(current=current),
        request_path=request_path,
        verification_path=verification_path,
        now=NOW,
    )
    assert result["status"] == "verified"
    return request_path, verification_path, result


def test_staged_request_becomes_stable_lane_specific_pickup_work(
    tmp_path: Path,
) -> None:
    request_path, request_verification_path, source = _source_bundle(tmp_path)
    result = handoff.materialize_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=tmp_path / "handoff.json",
        verification_path=tmp_path / "handoff-verification.json",
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["handoff_state"] == "producer_pickup_available"
    assert result["handoff_available"] is True
    assert result["request_id"] == source["request_id"]
    assert [row["lane"] for row in result["work_items"]] == [
        "gold_live_runtime",
        "scene_video_provider_refresh",
    ]
    assert all(
        row["work_item_state"] == "available_for_independent_pickup"
        and row["pickup_contract"]["mode"] == "read_only_artifact"
        and row["pickup_contract"]["handoff_confers_authority"] is False
        and row["settlement_contract"]["required_lane_status"] == "current"
        for row in result["work_items"]
    )
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["producer_claim_recorded"] is False
    assert result["producer_dispatch_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False


def test_work_item_ids_survive_exact_request_receipt_churn(tmp_path: Path) -> None:
    request_path, request_verification_path, _source = _source_bundle(tmp_path)
    first = handoff.materialize_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=tmp_path / "handoff.json",
        verification_path=tmp_path / "handoff-verification.json",
        now=NOW,
    )
    later = NOW + timedelta(minutes=1)
    projection = _projection()
    projection["updated_at"] = later.isoformat()
    projection["signal_approval"]["manifest_generated_at"] = later.isoformat()
    projection["source_cycle_receipt_sha256"] = "b" * 64
    refresh.materialize_source_refresh_request_bundle(
        projection,
        request_path=request_path,
        verification_path=request_verification_path,
        now=later,
    )
    second = handoff.materialize_source_refresh_handoff_bundle(
        projection,
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=tmp_path / "handoff.json",
        verification_path=tmp_path / "handoff-verification.json",
        now=later,
    )

    assert first["handoff_id"] == second["handoff_id"]
    assert [row["work_item_id"] for row in first["work_items"]] == [
        row["work_item_id"] for row in second["work_items"]
    ]
    assert first["request_receipt_sha256"] != second["request_receipt_sha256"]


def test_current_sources_publish_not_required_handoff_tombstone(
    tmp_path: Path,
) -> None:
    request_path, request_verification_path, _source = _source_bundle(
        tmp_path,
        current=True,
    )
    result = handoff.materialize_source_refresh_handoff_bundle(
        _projection(current=True),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=tmp_path / "handoff.json",
        verification_path=tmp_path / "handoff-verification.json",
        now=NOW,
    )

    assert result["status"] == "verified"
    assert result["handoff_state"] == "current_sources_verified"
    assert result["handoff_available"] is False
    assert result["handoff_id"] == ""
    assert result["work_items"] == []
    assert result["interrupt_operator"] is False


def test_handoff_bundle_is_private_integrity_bound_and_read_only_inspectable(
    tmp_path: Path,
) -> None:
    request_path, request_verification_path, _source = _source_bundle(tmp_path)
    handoff_path = tmp_path / "handoff.json"
    verification_path = tmp_path / "handoff-verification.json"
    result = handoff.materialize_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=verification_path,
        now=NOW,
    )
    handoff_bytes = handoff_path.read_bytes()
    verification_bytes = verification_path.read_bytes()
    handoff_mtime = handoff_path.stat().st_mtime_ns
    verification_mtime = verification_path.stat().st_mtime_ns

    assert result["status"] == "verified"
    assert stat.S_IMODE(handoff_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    assert handoff._integrity_verified(json.loads(handoff_path.read_text()))
    assert handoff._integrity_verified(
        json.loads(verification_path.read_text())
    )

    inspected = handoff.inspect_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=verification_path,
        now=NOW + timedelta(seconds=1),
    )
    assert inspected["status"] == "verified"
    assert inspected["handoff_receipt_sha256"] == result[
        "handoff_receipt_sha256"
    ]
    assert handoff_path.read_bytes() == handoff_bytes
    assert verification_path.read_bytes() == verification_bytes
    assert handoff_path.stat().st_mtime_ns == handoff_mtime
    assert verification_path.stat().st_mtime_ns == verification_mtime


def test_tamper_or_detached_request_fails_closed_without_interrupt(
    tmp_path: Path,
) -> None:
    request_path, request_verification_path, _source = _source_bundle(tmp_path)
    handoff_path = tmp_path / "handoff.json"
    handoff_verification_path = tmp_path / "handoff-verification.json"
    handoff.materialize_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=handoff_verification_path,
        now=NOW,
    )
    payload = json.loads(handoff_path.read_text())
    payload["work_items"][0]["lane"] = "forged"
    handoff_path.write_text(json.dumps(payload), encoding="utf-8")
    handoff_path.chmod(0o600)
    blocked = handoff.inspect_source_refresh_handoff_bundle(
        _projection(),
        request_path=request_path,
        request_verification_path=request_verification_path,
        handoff_path=handoff_path,
        verification_path=handoff_verification_path,
        now=NOW,
    )

    assert blocked["status"] == "blocked"
    assert blocked["handoff_state"] == "blocked"
    assert blocked["action_required"] is False
    assert blocked["interrupt_operator"] is False
    assert blocked["handoff_confers_authority"] is False
    assert blocked["producer_dispatch_authorized"] is False
    assert blocked["provider_quota_consumption_allowed"] is False
    assert blocked["delivery_authorized"] is False


def test_inspect_cli_never_materializes(monkeypatch, tmp_path: Path, capsys) -> None:
    observed: dict[str, object] = {}

    def inspect(**kwargs):
        observed.update(kwargs)
        return {
            "schema": handoff.VERIFY_SCHEMA,
            "status": "verified",
            "handoff_state": "producer_pickup_available",
        }

    monkeypatch.setattr(
        handoff,
        "inspect_current_source_refresh_handoff_bundle",
        inspect,
    )
    monkeypatch.setattr(
        handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("inspect must not materialize")
        ),
    )
    paths = [tmp_path / name for name in ("cycle", "request", "request-v", "handoff", "handoff-v")]
    result = handoff.main(
        [
            "--inspect",
            "--cycle-receipt",
            str(paths[0]),
            "--signal-dir",
            str(tmp_path / "signals"),
            "--request",
            str(paths[1]),
            "--request-verification",
            str(paths[2]),
            "--handoff",
            str(paths[3]),
            "--verification",
            str(paths[4]),
        ]
    )

    assert result == 0
    assert observed["handoff_path"] == paths[3]
    assert observed["verification_path"] == paths[4]
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
