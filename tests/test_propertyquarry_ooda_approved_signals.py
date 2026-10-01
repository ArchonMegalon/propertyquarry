from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_stage_ooda_signals as stage


NOW = datetime(2026, 8, 26, 4, 15, tzinfo=timezone.utc)
SECRET_MARKER = "secret-runtime-detail-must-not-enter-ingress"


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _gold_receipt() -> dict[str, object]:
    return {
        "schema": "propertyquarry.gold_status.v1",
        "status": "blocked",
        "generated_at": "2026-08-26T04:10:00+00:00",
        "internal_runtime_detail": SECRET_MARKER,
        "live_mobile_surfaces": {
            "route_probe_blocked": True,
            "runtime_blocker": SECRET_MARKER,
            "operator_action": {
                "required": True,
                "interrupt_operator": True,
                "source_fresh": True,
                "source_generated_at": "2026-08-26T04:05:00+00:00",
                "reason": "live_runtime_host_admission_rejected",
                "runtime_blocker": SECRET_MARKER,
                "reversible_next_action": SECRET_MARKER,
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


def _scene_packet() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
        "generated_at": "2026-08-26T04:01:00+00:00",
        "source_receipt": "/private/operator/path/readiness.json",
        "source_receipt_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_receipt_generated_at": "2026-08-26T04:00:00+00:00",
        "providers": [
            {
                "provider": "magicfit",
                "expected_account_count": 2,
                "runtime_account_count": 1,
                "visible_account_gap": 1,
                "tracked_account_count": 1,
                "unavailable_account_count": 0,
                "credit_state": "funded",
                "credit_refresh_required": False,
                "runtime_blockers": [SECRET_MARKER],
                "post_refresh_checks": [SECRET_MARKER],
            },
            {
                "provider": "omagic",
                "expected_account_count": 8,
                "runtime_account_count": 8,
                "visible_account_gap": 0,
                "tracked_account_count": 8,
                "unavailable_account_count": 0,
                "credit_state": "funded",
                "credit_refresh_required": False,
            },
        ],
    }


def _scene_verifier() -> dict[str, object]:
    return {
        "status": "pass",
        "generated_at": "2026-08-26T04:01:00+00:00",
        "provider_count": 2,
        "checked_providers": ["magicfit", "omagic"],
        "blockers": [],
        "private_detail": SECRET_MARKER,
    }


def _scene_runtime() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_runtime_status.v1",
        "generated_at": "2026-08-26T04:01:05+00:00",
        "source_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_kind": "receipt_file",
        "source_ref": "/private/operator/path/readiness.json",
        "summary": {
            "action_required_count": 1,
            "action_required_providers": ["magicfit"],
        },
        "providers": [
            {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "attention_required": True,
                "blocking_reason": SECRET_MARKER,
            },
            {
                "provider": "omagic",
                "provider_key": "omagic",
                "attention_required": False,
            },
        ],
    }


def _public_origin_observation() -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_public_origin_observation.v1",
        "generated_at": "2026-08-26T04:14:00+00:00",
        "status": "reachable",
        "origin": "https://propertyquarry.com",
        "request": {
            "method": "GET",
            "path": "/",
            "credentials_sent": False,
            "redirects_followed": False,
            "tls_validation": "system_trust_store",
        },
        "observation": {
            "http_status": 200,
            "edge_provider": "",
            "error_code": 0,
            "reason": "public_origin_reachable",
        },
        "response_content_recorded": False,
        "response_headers_recorded": False,
        "action_required": False,
        "interrupt_operator": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _source_bundle(tmp_path: Path) -> dict[str, Path]:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    return {
        "gold_receipt_path": _write_json(source_dir / "gold.json", _gold_receipt()),
        "public_origin_observation_path": _write_json(
            source_dir / "public-origin.json", _public_origin_observation()
        ),
        "scene_packet_path": _write_json(source_dir / "packet.json", _scene_packet()),
        "scene_verifier_path": _write_json(source_dir / "verifier.json", _scene_verifier()),
        "scene_runtime_status_path": _write_json(source_dir / "runtime.json", _scene_runtime()),
    }


def _stale_scene_bundle(sources: dict[str, Path]) -> None:
    packet = json.loads(sources["scene_packet_path"].read_text(encoding="utf-8"))
    packet["generated_at"] = "2026-08-26T03:45:00+00:00"
    packet["source_receipt_generated_at"] = "2026-08-26T03:44:59+00:00"
    sources["scene_packet_path"].write_text(json.dumps(packet), encoding="utf-8")
    verifier = json.loads(
        sources["scene_verifier_path"].read_text(encoding="utf-8")
    )
    verifier["generated_at"] = "2026-08-26T03:45:00+00:00"
    sources["scene_verifier_path"].write_text(json.dumps(verifier), encoding="utf-8")
    runtime = json.loads(
        sources["scene_runtime_status_path"].read_text(encoding="utf-8")
    )
    runtime["generated_at"] = "2026-08-26T03:45:05+00:00"
    sources["scene_runtime_status_path"].write_text(json.dumps(runtime), encoding="utf-8")


def _stage_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    sources = _source_bundle(tmp_path)
    target_dir = tmp_path / "approved"
    report = stage.stage_approved_signals(
        **sources,
        target_dir=target_dir,
        receipt_path=tmp_path / "stage-receipt.json",
        now=NOW,
    )
    return target_dir, report


def _evaluate_bundle(
    tmp_path: Path,
    target_dir: Path,
    *,
    now: datetime = NOW,
    require_manifest: bool = True,
    send: bool = False,
    deliver=None,
) -> dict[str, object]:
    return cycle.run_cycle_once(
        gold_receipt=str(target_dir / "property-gold-status.json"),
        public_origin_observation=str(
            target_dir / "public-origin-observation.json"
        ),
        scene_packet=str(target_dir / "scene-video-provider-refresh-packet.json"),
        scene_verifier=str(target_dir / "scene-video-provider-refresh-verifier.json"),
        scene_runtime_status=str(target_dir / "scene-video-runtime-status.json"),
        approval_manifest=str(target_dir / "manifest.json"),
        require_approval_manifest=require_manifest,
        state_file=str(tmp_path / "state.json"),
        lock_file=str(tmp_path / "send.lock"),
        write=str(tmp_path / "cycle.json"),
        send=send,
        now=now,
        deliver=deliver,
    )


def test_stager_publishes_sanitized_readable_batch_and_private_receipt(tmp_path: Path) -> None:
    target_dir, report = _stage_bundle(tmp_path)

    assert report["status"] == "staged"
    assert report["sanitized_signal_count"] == 5
    assert report["automatic_execution_allowed"] is False
    assert report["provider_quota_consumption_allowed"] is False
    assert report["publication_verified"] is True
    assert report["blocking_reason"] == ""
    assert report["next_action"] == "republish_after_configured_interval"
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o755
    published_text = ""
    for path in target_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        published_text += path.read_text(encoding="utf-8")
    assert SECRET_MARKER not in published_text
    assert "/private/operator/path" not in published_text
    assert stat.S_IMODE((tmp_path / "stage-receipt.json").stat().st_mode) == 0o600

    cycle_report = _evaluate_bundle(tmp_path, target_dir)
    assert cycle_report["signal_approval"] == {
        "approved": True,
        "reason": "approved_projection_manifest_verified",
        "manifest_generated_at": NOW.isoformat(),
        "policy": "propertyquarry.ooda_action_required_only.v1",
    }
    assert cycle_report["action_required_count"] == 2
    assert cycle_report["novel_action_count"] == 2
    assert cycle_report["delivery_authorized"] is False
    assert cycle_report["delivery_attempted"] is False
    assert cycle_report["sent"] is False

    operator_projection = operator_status.load_operator_status(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=target_dir,
        now=NOW,
    )
    assert operator_projection["status"] == "action_required"
    assert operator_projection["action_required"] is True
    assert operator_projection["interrupt_operator"] is True
    assert {action["lane"] for action in operator_projection["actions"]} == {
        "gold_live_runtime",
        "scene_video_provider_refresh",
    }


def test_republishing_stale_gold_source_cannot_freshen_or_reauthorize_it(
    tmp_path: Path,
) -> None:
    sources = _source_bundle(tmp_path)
    stale_time = (NOW - timedelta(seconds=1801)).isoformat()
    gold_path = sources["gold_receipt_path"]
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["generated_at"] = stale_time
    gold["live_mobile_surfaces"]["operator_action"][
        "source_generated_at"
    ] = stale_time
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    target_dir = tmp_path / "approved"

    staged = stage.stage_approved_signals(
        **sources,
        target_dir=target_dir,
        receipt_path=tmp_path / "stage-receipt.json",
        now=NOW,
    )
    evaluated = _evaluate_bundle(tmp_path, target_dir, now=NOW)
    gold_lane = next(
        row for row in evaluated["lanes"] if row["lane"] == "gold_live_runtime"
    )

    assert staged["generated_at"] == NOW.isoformat()
    assert staged["lane_posture"]["gold_live_runtime"] == {
        "action_required": False,
        "reason": "source_receipt_not_fresh",
        "source_generated_at": stale_time,
    }
    assert evaluated["signal_approval"]["approved"] is True
    assert gold_lane["action_required"] is False
    assert gold_lane["clear_verified"] is False
    assert gold_lane["reason"] == "gold_receipt_not_fresh"
    assert all(
        action["lane"] != "gold_live_runtime"
        for action in evaluated["actions"]
    )
    assert evaluated["delivery_authorized"] is False
    assert evaluated["delivery_attempted"] is False
    assert evaluated["sent"] is False


def test_fresh_1033_observation_sustains_action_when_gold_is_stale(
    tmp_path: Path,
) -> None:
    sources = _source_bundle(tmp_path)
    stale_time = (NOW - timedelta(seconds=1801)).isoformat()
    gold = json.loads(sources["gold_receipt_path"].read_text(encoding="utf-8"))
    gold["generated_at"] = stale_time
    gold["live_mobile_surfaces"]["operator_action"][
        "source_generated_at"
    ] = stale_time
    sources["gold_receipt_path"].write_text(json.dumps(gold), encoding="utf-8")
    public_origin = _public_origin_observation()
    public_origin["status"] = "blocked"
    public_origin["observation"] = {
        "http_status": 530,
        "edge_provider": "cloudflare",
        "error_code": 1033,
        "reason": "cloudflare_tunnel_unavailable",
    }
    sources["public_origin_observation_path"].write_text(
        json.dumps(public_origin),
        encoding="utf-8",
    )
    target_dir = tmp_path / "approved"

    staged = stage.stage_approved_signals(
        **sources,
        target_dir=target_dir,
        receipt_path=tmp_path / "stage-receipt.json",
        now=NOW,
    )
    evaluated = _evaluate_bundle(tmp_path, target_dir, now=NOW)
    gold_lane = next(
        row for row in evaluated["lanes"] if row["lane"] == "gold_live_runtime"
    )
    gold_action = next(
        row for row in evaluated["actions"] if row["lane"] == "gold_live_runtime"
    )

    assert staged["lane_posture"]["gold_live_runtime"] == {
        "action_required": True,
        "reason": "live_runtime_tunnel_unavailable",
        "source_generated_at": public_origin["generated_at"],
    }
    assert gold_lane["action_required"] is True
    assert gold_lane["clear_verified"] is False
    assert gold_lane["reason"] == "live_runtime_tunnel_unavailable"
    assert gold_lane["observed_source_generated_at"] == public_origin[
        "generated_at"
    ]
    assert gold_action["consent_required"] is True
    assert gold_action["automatic_execution_allowed"] is False
    assert gold_action["protected_operations"] == [
        "runtime_configuration_change",
        "deployment_or_restart",
    ]
    assert gold_action["provider_quota_consumption_allowed"] is False
    assert evaluated["delivery_authorized"] is False
    assert evaluated["delivery_attempted"] is False
    assert evaluated["sent"] is False


def test_regenerated_gold_preserves_stale_source_provenance(
    tmp_path: Path,
) -> None:
    sources = _source_bundle(tmp_path)
    stale_time = (NOW - timedelta(hours=25)).isoformat()
    gold_path = sources["gold_receipt_path"]
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["generated_at"] = NOW.isoformat()
    gold["live_mobile_surfaces"]["operator_action"] = {
        "required": False,
        "interrupt_operator": False,
        "reason": "source_receipt_not_fresh",
        "source_fresh": False,
        "source_generated_at": stale_time,
        "notification_policy": "suppress_stale_signal",
        "provider_quota_consumption_allowed": False,
    }
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    target_dir = tmp_path / "approved"

    staged = stage.stage_approved_signals(
        **sources,
        target_dir=target_dir,
        receipt_path=tmp_path / "stage-receipt.json",
        now=NOW,
    )
    evaluated = _evaluate_bundle(tmp_path, target_dir, now=NOW)
    gold_lane = next(
        row for row in evaluated["lanes"] if row["lane"] == "gold_live_runtime"
    )

    assert staged["source_evidence_posture"]["progress"]["stale_lane_count"] == 1
    assert gold_lane["reason"] == "source_receipt_not_fresh"
    assert gold_lane["observed_source_generated_at"] == stale_time
    assert gold_lane["action_required"] is False
    assert evaluated["interrupt_operator"] is True
    assert all(
        action["lane"] != "gold_live_runtime"
        for action in evaluated["actions"]
    )


def test_stager_uses_the_30_minute_scene_window_and_names_producer_boundary(
    tmp_path: Path,
) -> None:
    sources = _source_bundle(tmp_path)
    _stale_scene_bundle(sources)

    staged = stage.stage_approved_signals(
        **sources,
        target_dir=tmp_path / "approved",
        receipt_path=tmp_path / "stage-receipt.json",
        now=NOW,
    )

    posture = staged["source_evidence_posture"]
    assert posture["status"] == "waiting_for_fresh_sources"
    assert staged["next_action"] == posture["next_action"]
    assert posture["automatic_source_refresh_allowed"] is False
    assert posture["progress"] == {
        "expected_lane_count": 2,
        "current_lane_count": 1,
        "stale_lane_count": 1,
        "unavailable_lane_count": 0,
    }
    scene_lane = next(
        row
        for row in posture["lanes"]
        if row["lane"] == "scene_video_provider_refresh"
    )
    assert scene_lane == {
        "lane": "scene_video_provider_refresh",
        "status": "stale",
        "reason": "source_receipt_not_fresh",
        "source_generated_at": "2026-08-26T03:44:59+00:00",
        "source_artifacts": [
            "scene_packet",
            "scene_verifier",
            "scene_runtime_status",
        ],
        "producer_authority": "external_receipt_producer",
        "producer_refresh_required": True,
        "automatic_source_refresh_allowed": False,
    }
    assert "provider" not in posture["next_action"].split("without", 1)[0]
    assert staged["provider_quota_consumption_allowed"] is False


def test_scheduler_cycle_rejects_hash_tampering_without_clearing_or_interrupting(tmp_path: Path) -> None:
    target_dir, _report = _stage_bundle(tmp_path)
    packet_path = target_dir / "scene-video-provider-refresh-packet.json"
    packet_path.write_text(packet_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    packet_path.chmod(0o644)

    cycle_report = _evaluate_bundle(tmp_path, target_dir)

    assert cycle_report["signal_approval"]["approved"] is False
    assert cycle_report["signal_approval"]["reason"] == "scene_packet_hash_mismatch"
    assert cycle_report["action_required_count"] == 0
    assert cycle_report["novel_action_count"] == 0
    assert cycle_report["interrupt_operator"] is False
    assert cycle_report["sent"] is False

    optional_flag_report = _evaluate_bundle(
        tmp_path,
        target_dir,
        require_manifest=False,
    )
    assert optional_flag_report["signal_approval"]["approved"] is False
    assert optional_flag_report["action_required_count"] == 0


def test_operator_status_verifies_real_completed_cycle_delivery_receipt(
    tmp_path: Path,
) -> None:
    target_dir, _report = _stage_bundle(tmp_path)
    cycle_report = _evaluate_bundle(
        tmp_path,
        target_dir,
        send=True,
        deliver=lambda **_kwargs: {
            "delivery_mode": "principal_binding",
            "message_ids": ["receipt-6101"],
        },
    )
    assert cycle_report["status"] == "completed"
    assert cycle_report["state_updated"] is True

    operator_projection = operator_status.load_operator_status(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=target_dir,
        now=NOW,
    )
    assert operator_projection["status"] == "ready"
    assert operator_projection["action_required"] is False
    assert operator_projection["interrupt_operator"] is False
    assert operator_projection["progress"]["delivery_receipt_verified"] is True
    assert operator_projection["progress"]["delivered_action_count"] == 2


@pytest.mark.parametrize(
    ("forgery", "expected_reason"),
    [
        ("extra_manifest_authority", "approval_manifest_contract_not_admissible"),
        ("extra_signal_authority", "approval_manifest_contract_not_admissible"),
        ("source_contract", "gold_receipt_approval_not_admissible"),
        ("source_timestamp", "approval_manifest_contract_not_admissible"),
        ("projected_payload_authority", "approved_signal_payload_contract_not_admissible"),
    ],
)
def test_scheduler_rejects_forged_approved_snapshot_semantics(
    tmp_path: Path,
    forgery: str,
    expected_reason: str,
) -> None:
    target_dir, _report = _stage_bundle(tmp_path)
    manifest_path = target_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if forgery == "extra_manifest_authority":
        manifest["delivery_authorized"] = True
    elif forgery == "extra_signal_authority":
        manifest["signals"]["gold_receipt"]["automatic_execution_allowed"] = True
    elif forgery == "source_contract":
        manifest["signals"]["gold_receipt"]["source_contract"] = "forged.contract.v1"
    elif forgery == "source_timestamp":
        manifest["signals"]["gold_receipt"]["source_generated_at"] = (
            NOW - timedelta(seconds=1)
        ).isoformat()
    elif forgery == "projected_payload_authority":
        gold_path = target_dir / "property-gold-status.json"
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        gold["automatic_execution_allowed"] = True
        gold_bytes = stage.approved.canonical_json_bytes(gold)
        gold_path.write_bytes(gold_bytes)
        gold_path.chmod(0o644)
        manifest["signals"]["gold_receipt"]["sha256"] = (
            stage.approved.sha256_bytes(gold_bytes)
        )
        manifest["signals"]["gold_receipt"]["bytes"] = len(gold_bytes)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(f"unknown forgery: {forgery}")

    manifest_path.write_bytes(stage.approved.canonical_json_bytes(manifest))
    manifest_path.chmod(0o644)
    cycle_report = _evaluate_bundle(tmp_path, target_dir)

    assert cycle_report["signal_approval"]["approved"] is False
    assert cycle_report["signal_approval"]["reason"] == expected_reason
    assert cycle_report["action_required_count"] == 0
    assert cycle_report["novel_action_count"] == 0
    assert cycle_report["interrupt_operator"] is False
    assert cycle_report["delivery_attempted"] is False
    assert cycle_report["sent"] is False


def test_scheduler_cycle_rejects_stale_manifest_and_peer_writable_signal(tmp_path: Path) -> None:
    target_dir, _report = _stage_bundle(tmp_path)
    stale_report = _evaluate_bundle(tmp_path, target_dir, now=NOW + timedelta(seconds=1801))
    assert stale_report["signal_approval"]["reason"] == "approval_manifest_not_fresh"
    assert stale_report["action_required_count"] == 0

    packet_path = target_dir / "scene-video-provider-refresh-packet.json"
    packet_path.chmod(0o664)
    mode_report = _evaluate_bundle(tmp_path, target_dir)
    assert mode_report["signal_approval"]["reason"] == "scene_packet_mode_not_admissible"
    assert mode_report["action_required_count"] == 0


def test_scheduler_cycle_rejects_partial_publication_without_manifest(tmp_path: Path) -> None:
    target_dir, _report = _stage_bundle(tmp_path)
    (target_dir / "manifest.json").unlink()

    cycle_report = _evaluate_bundle(tmp_path, target_dir)

    assert cycle_report["signal_approval"]["approved"] is False
    assert cycle_report["signal_approval"]["reason"] == "approval_manifest_contract_not_admissible"
    assert cycle_report["input_evidence"]["approval_manifest"]["status"] == "missing"
    assert cycle_report["action_required_count"] == 0
    assert cycle_report["interrupt_operator"] is False


def test_stager_rejects_peer_writable_operator_source(tmp_path: Path) -> None:
    sources = _source_bundle(tmp_path)
    sources["gold_receipt_path"].chmod(0o666)

    with pytest.raises(ValueError, match="gold_receipt_source_not_admissible"):
        stage.stage_approved_signals(
            **sources,
            target_dir=tmp_path / "approved",
            receipt_path=tmp_path / "stage-receipt.json",
            now=NOW,
        )

    assert not (tmp_path / "approved" / "manifest.json").exists()


class _StopAfterFirstCycle:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        assert timeout == 60.0
        self.stopped = True
        return True


def test_stage_daemon_persists_normalized_success_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _source_bundle(tmp_path)
    receipt_path = tmp_path / "daemon-status.json"
    monkeypatch.setattr(stage, "_observed_now", lambda _now=None: NOW)

    result = stage.run_stage_daemon(
        **sources,
        target_dir=tmp_path / "approved",
        receipt_path=receipt_path,
        interval_seconds=60,
        max_age_seconds=1800,
        stop_event=_StopAfterFirstCycle(),
    )

    status = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result == 0
    assert status["status"] == "ready"
    assert status["blocking_reason"] == ""
    assert status["next_action"] == "republish_after_configured_interval"
    assert status["progress"]["cycles"] == 1
    assert status["progress"]["successful_cycles"] == 1
    assert status["progress"]["failed_cycles"] == 0
    assert status["progress"]["last_successful_at"]
    assert status["automatic_execution_allowed"] is False
    assert status["provider_quota_consumption_allowed"] is False
    assert status["source_status"] == "verified_current"
    assert status["source_evidence_posture"]["status"] == "verified_current"
    assert status["source_evidence_posture"][
        "automatic_source_refresh_allowed"
    ] is False
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    health = stage.inspect_stage_status_receipt(
        receipt_path,
        max_age_seconds=1800,
    )
    assert health["status"] == "ready"
    assert health["blocking_reason"] == ""
    assert health["source_status"] == "verified_current"
    assert SECRET_MARKER not in capsys.readouterr().out


def test_stage_daemon_stays_operational_but_reports_waiting_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _source_bundle(tmp_path)
    _stale_scene_bundle(sources)
    receipt_path = tmp_path / "daemon-status.json"
    monkeypatch.setattr(stage, "_observed_now", lambda _now=None: NOW)

    assert stage.run_stage_daemon(
        **sources,
        target_dir=tmp_path / "approved",
        receipt_path=receipt_path,
        interval_seconds=60,
        max_age_seconds=1800,
        stop_event=_StopAfterFirstCycle(),
    ) == 0

    status = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert status["status"] == "ready"
    assert status["blocking_reason"] == ""
    assert status["source_status"] == "waiting_for_fresh_sources"
    assert status["source_evidence_posture"]["progress"]["stale_lane_count"] == 1
    assert status["next_action"] == status["source_evidence_posture"]["next_action"]
    assert status["automatic_execution_allowed"] is False
    assert status["provider_quota_consumption_allowed"] is False
    health = stage.inspect_stage_status_receipt(
        receipt_path,
        now=NOW,
        max_age_seconds=1800,
    )
    assert health["status"] == "ready"
    assert health["source_status"] == "waiting_for_fresh_sources"

    status["source_evidence_posture"]["lanes"][1]["status"] = "current"
    receipt_path.write_text(json.dumps(status), encoding="utf-8")
    receipt_path.chmod(0o600)
    with pytest.raises(
        ValueError,
        match="stage_status_source_evidence_not_admissible",
    ):
        stage.inspect_stage_status_receipt(
            receipt_path,
            now=NOW,
            max_age_seconds=1800,
        )


def test_stage_daemon_revokes_approval_but_stays_operational_for_missing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sources = _source_bundle(tmp_path)
    receipt_path = tmp_path / "daemon-status.json"
    receipt_path.write_text(
        json.dumps({"status": "ready", "updated_at": NOW.isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(stage, "_observed_now", lambda _now=None: NOW)
    target_dir = tmp_path / "approved"
    stage.stage_approved_signals(
        **sources,
        target_dir=target_dir,
        receipt_path=tmp_path / "initial-stage.json",
        now=NOW,
    )
    initially_approved = _evaluate_bundle(tmp_path, target_dir)
    assert initially_approved["signal_approval"]["approved"] is True
    assert initially_approved["action_required_count"] == 2
    sources["gold_receipt_path"].unlink()

    result = stage.run_stage_daemon(
        **sources,
        target_dir=target_dir,
        receipt_path=receipt_path,
        interval_seconds=60,
        max_age_seconds=1800,
        stop_event=_StopAfterFirstCycle(),
    )

    status = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result == 0
    assert status["status"] == "ready"
    assert status["blocking_reason"] == ""
    assert status["publication_status"] == "revoked"
    assert status["source_status"] == "waiting_for_fresh_sources"
    assert status["source_evidence_posture"]["progress"] == {
        "expected_lane_count": 2,
        "current_lane_count": 1,
        "stale_lane_count": 0,
        "unavailable_lane_count": 1,
    }
    gold_lane = next(
        row
        for row in status["source_evidence_posture"]["lanes"]
        if row["lane"] == "gold_live_runtime"
    )
    assert gold_lane["status"] == "unavailable"
    assert gold_lane["reason"] == "gold_live_runtime_source_receipts_unavailable"
    assert gold_lane["producer_authority"] == "external_receipt_producer"
    assert gold_lane["automatic_source_refresh_allowed"] is False
    assert status["progress"] == {
        "cycles": 1,
        "successful_cycles": 0,
        "failed_cycles": 1,
        "last_successful_at": "",
    }
    assert status["automatic_execution_allowed"] is False
    assert status["provider_quota_consumption_allowed"] is False
    health = stage.inspect_stage_status_receipt(
        receipt_path,
        now=NOW,
        max_age_seconds=1800,
    )
    assert health["status"] == "ready"
    assert health["publication_status"] == "revoked"
    revocation = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
    assert revocation["schema"] == stage.REVOCATION_SCHEMA
    assert revocation["status"] == "revoked"
    assert revocation["automatic_execution_allowed"] is False
    assert revocation["source_lanes"] == status["source_lanes"]
    assert revocation["source_evidence_posture"] == status["source_evidence_posture"]

    cycle_report = _evaluate_bundle(tmp_path, target_dir)
    assert cycle_report["status"] == "silent"
    assert cycle_report["signal_approval"]["approved"] is False
    assert cycle_report["signal_approval"]["revocation_verified"] is True
    assert cycle_report["signal_approval"]["reason"] == (
        "approved_projection_manifest_revoked"
    )
    assert cycle_report["publication_status"] == "revoked"
    assert cycle_report["action_required_count"] == 0
    assert cycle_report["revocation_suppressed_action_count"] == 1
    assert cycle_report["interrupt_operator"] is False
    assert cycle_report["provider_quota_consumption_allowed"] is False
    assert cycle_report["source_evidence_posture"]["progress"] == {
        "expected_lane_count": 2,
        "current_lane_count": 1,
        "stale_lane_count": 0,
        "unavailable_lane_count": 1,
    }

    operator_projection = operator_status.load_operator_status(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=target_dir,
        now=NOW,
    )
    assert operator_projection["status"] == "waiting_for_evidence"
    assert operator_projection["action_required"] is False
    assert operator_projection["interrupt_operator"] is False
    assert operator_projection["blocking_reason"] == (
        "approved_source_snapshot_revoked"
    )
    assert operator_projection["progress"]["approved_snapshot_verified"] is False
    assert operator_projection["progress"]["revocation_verified"] is True
    assert operator_projection["progress"][
        "current_revocation_manifest_verified"
    ] is True

    capsys.readouterr()
    exit_code = stage.main(
        [
            "--health",
            "--require-status-receipt",
            "--target-dir",
            str(target_dir),
            "--write",
            str(receipt_path),
            "--approval-max-age-seconds",
            "1800",
        ]
    )
    health_output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert health_output["status"] == "ready"
    assert health_output["approved"] is False
    assert health_output["revocation_verified"] is True
    assert health_output["publication_status"] == "revoked"

    revocation["reason"] = "forged_ready"
    (target_dir / "manifest.json").write_text(
        json.dumps(revocation),
        encoding="utf-8",
    )
    (target_dir / "manifest.json").chmod(0o644)
    tampered_exit = stage.main(
        [
            "--health",
            "--require-status-receipt",
            "--target-dir",
            str(target_dir),
            "--write",
            str(receipt_path),
            "--approval-max-age-seconds",
            "1800",
        ]
    )
    tampered_health = json.loads(capsys.readouterr().out)
    assert tampered_exit == 1
    assert tampered_health["status"] == "blocked"
    assert tampered_health["approved"] is False
    tampered_operator_projection = operator_status.load_operator_status(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=target_dir,
        now=NOW,
    )
    assert tampered_operator_projection["status"] == "blocked"
    assert tampered_operator_projection["interrupt_operator"] is False


def test_initial_missing_source_publishes_manifest_only_waiting_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _source_bundle(tmp_path)
    sources["gold_receipt_path"].unlink()
    target_dir = tmp_path / "approved"
    receipt_path = tmp_path / "daemon-status.json"
    monkeypatch.setattr(stage, "_observed_now", lambda _now=None: NOW)

    assert stage.run_stage_daemon(
        **sources,
        target_dir=target_dir,
        receipt_path=receipt_path,
        interval_seconds=60,
        max_age_seconds=1800,
        stop_event=_StopAfterFirstCycle(),
    ) == 0

    assert (target_dir / "manifest.json").is_file()
    assert not any(
        (target_dir / filename).exists()
        for filename in stage.approved.SIGNAL_FILENAMES.values()
    )
    cycle_report = _evaluate_bundle(tmp_path, target_dir)
    assert cycle_report["status"] == "silent"
    assert cycle_report["signal_approval"]["revocation_verified"] is True
    assert cycle_report["input_evidence"]["gold_receipt"]["status"] == "missing"
    assert cycle_report["action_required_count"] == 0
    assert cycle_report["interrupt_operator"] is False
    assert cycle_report["delivery_attempted"] is False
    assert cycle_report["provider_quota_consumption_allowed"] is False

    def unexpected_delivery(**_kwargs):
        raise AssertionError("verified revocation must suppress delivery")

    send_requested_report = _evaluate_bundle(
        tmp_path,
        target_dir,
        send=True,
        deliver=unexpected_delivery,
    )
    assert send_requested_report["execution_mode"] == "revocation_fail_closed"
    assert send_requested_report["send_requested"] is True
    assert send_requested_report["delivery_authorized"] is False
    assert send_requested_report["delivery_attempted"] is False
    assert send_requested_report["sent"] is False

    projection = operator_status.load_operator_status(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=target_dir,
        now=NOW,
    )
    assert projection["status"] == "waiting_for_evidence"
    assert projection["action_required"] is False
    assert projection["interrupt_operator"] is False
    assert projection["progress"]["current_revocation_manifest_verified"] is True


def test_stage_daemon_keeps_internal_publication_failure_unhealthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _source_bundle(tmp_path)
    receipt_path = tmp_path / "daemon-status.json"
    target_dir = tmp_path / "approved"
    target_dir.mkdir(mode=0o777)
    target_dir.chmod(0o777)
    monkeypatch.setattr(stage, "_observed_now", lambda _now=None: NOW)

    assert stage.run_stage_daemon(
        **sources,
        target_dir=target_dir,
        receipt_path=receipt_path,
        interval_seconds=60,
        max_age_seconds=1800,
        stop_event=_StopAfterFirstCycle(),
    ) == 0

    status = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert status["status"] == "stage_failed"
    assert status["blocking_reason"] == "stage_cycle_failed"
    assert status["next_action"] == (
        "inspect_source_contract_freshness_and_permissions_then_retry"
    )
    assert status["progress"]["failed_cycles"] == 1
    assert status["automatic_execution_allowed"] is False
    assert status["provider_quota_consumption_allowed"] is False
    with pytest.raises(ValueError, match="stage_status_contract_not_admissible"):
        stage.inspect_stage_status_receipt(
            receipt_path,
            now=NOW,
            max_age_seconds=1800,
        )
