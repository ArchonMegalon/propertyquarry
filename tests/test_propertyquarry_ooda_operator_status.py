from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_notification_cycle as cycle
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_stage_ooda_signals as stage


NOW = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)


def _approved_payloads(generated_at: str) -> dict[str, dict[str, object]]:
    source_ref = "approved-signal://propertyquarry/scene-video-readiness"
    return {
        "gold_receipt": {
            "schema": approved.GOLD_STATUS_SCHEMA,
            "status": "pass",
            "generated_at": generated_at,
        },
        "public_origin_observation": {
            "schema": approved.SOURCE_CONTRACTS[
                "public_origin_observation"
            ],
            "generated_at": generated_at,
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
        },
        "scene_packet": {
            "contract_name": approved.SOURCE_CONTRACTS["scene_packet"],
            "generated_at": generated_at,
            "source_receipt": source_ref,
            "source_receipt_contract_name": "propertyquarry.scene_video_readiness.v1",
            "source_receipt_generated_at": generated_at,
            "providers": [
                {
                    "provider": "magicfit",
                    "expected_account_count": 1,
                    "runtime_account_count": 1,
                    "visible_account_gap": 0,
                    "tracked_account_count": 1,
                    "unavailable_account_count": 0,
                    "credit_state": "funded",
                    "credit_refresh_required": False,
                }
            ],
        },
        "scene_verifier": {
            "status": "pass",
            "generated_at": generated_at,
            "provider_count": 1,
            "checked_providers": ["magicfit"],
            "blockers": [],
        },
        "scene_runtime_status": {
            "contract_name": approved.SOURCE_CONTRACTS["scene_runtime_status"],
            "generated_at": generated_at,
            "source_contract_name": "propertyquarry.scene_video_readiness.v1",
            "source_kind": "receipt_file",
            "source_ref": source_ref,
            "summary": {
                "action_required_count": 0,
                "action_required_providers": [],
            },
            "providers": [
                {
                    "provider": "magicfit",
                    "provider_key": "magicfit",
                    "attention_required": False,
                }
            ],
        },
    }


def _approved_snapshot(tmp_path: Path) -> tuple[Path, dict[str, dict[str, object]], dict[str, object]]:
    signal_dir = tmp_path / "approved"
    signal_dir.mkdir(mode=0o755)
    generated_at = (NOW - timedelta(minutes=5)).isoformat()
    signal_bytes = {
        name: approved.canonical_json_bytes(payload)
        for name, payload in _approved_payloads(generated_at).items()
    }
    for name, filename in approved.SIGNAL_FILENAMES.items():
        path = signal_dir / filename
        path.write_bytes(signal_bytes[name])
        path.chmod(0o644)
    manifest = approved.build_approval_manifest(
        signal_bytes=signal_bytes,
        source_evidence={name: {"sha256": "a" * 64} for name in signal_bytes},
        source_generated_at={name: generated_at for name in signal_bytes},
        source_contracts=approved.SOURCE_CONTRACTS,
        now=NOW,
    )
    manifest_path = signal_dir / "manifest.json"
    manifest_path.write_bytes(approved.canonical_json_bytes(manifest))
    manifest_path.chmod(0o644)
    evidence: dict[str, dict[str, object]] = {}
    for name, filename in {
        **approved.SIGNAL_FILENAMES,
        "approval_manifest": "manifest.json",
    }.items():
        path = signal_dir / filename
        raw = path.read_bytes()
        evidence[name] = {
            "path": str(path),
            "status": "ready",
            "sha256": approved.sha256_bytes(raw),
            "bytes": len(raw),
            "mode": 0o644,
        }
    approval_status = stage.inspect_approved_signal_dir(signal_dir, now=NOW)
    return signal_dir, evidence, approval_status


def _cycle_receipt(
    evidence: dict[str, dict[str, object]],
    approval_status: dict[str, object],
) -> dict[str, object]:
    source_generated_at = (NOW - timedelta(minutes=10)).isoformat()
    lanes = [
        {
            "lane": "gold_live_runtime",
            "source_status": "ready",
            "action_required": True,
            "clear_verified": False,
            "reason": "live_runtime_host_admission_rejected",
            "source_generated_at": source_generated_at,
            "observed_source_generated_at": source_generated_at,
        },
        {
            "lane": "scene_video_provider_refresh",
            "source_status": "ready",
            "action_required": False,
            "clear_verified": True,
            "reason": "no_actionable_provider_refresh",
            "source_generated_at": "",
            "observed_source_generated_at": source_generated_at,
        },
    ]
    receipt: dict[str, object] = {
        "schema": "propertyquarry.ooda_notification_cycle.v1",
        "generated_at": NOW.isoformat(),
        "status": "action_required",
        "execution_mode": "evaluate_only",
        "notification_policy": "action_required_only",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": True,
        "notification_count": 0,
        "message_ids": [],
        "delivery_mode": "",
        "state_path": "/private/ooda-notification-state.json",
        "state_updated": False,
        "notification_state_status": "missing",
        "notification_state_sha256": "",
        "notification_state_admissible": True,
        "operator_action_required": True,
        "interrupt_operator": True,
        "action_required_count": 1,
        "novel_action_count": 1,
        "active_action_count_before": 0,
        "active_action_count_projected": 1,
        "protected_operation_executed": False,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "signal_approval": approval_status,
        "input_evidence": evidence,
        "lanes": lanes,
        "source_evidence_posture": cycle._build_source_evidence_posture(lanes),
        "actions": [
            {
                "lane": "gold_live_runtime",
                "reason": "live_runtime_host_admission_rejected",
                "source_generated_at": source_generated_at,
                "safe_next_action": operator_status._GOLD_ACTIONS[
                    "live_runtime_host_admission_rejected"
                ],
                "consent_required": True,
                "automatic_execution_allowed": False,
                "protected_operations": [
                    "runtime_configuration_change",
                    "deployment_or_restart",
                ],
                "provider_quota_consumption_allowed": False,
            }
        ],
    }
    receipt["message_preview"] = cycle._build_consolidated_message(
        list(receipt["actions"]),
        generated_at=NOW.isoformat(),
    )
    receipt["next_action"] = (
        "rerun with --send only when factual operator delivery is authorized"
    )
    return receipt


def _silent_cycle_receipt(
    evidence: dict[str, dict[str, object]],
    approval_status: dict[str, object],
    *,
    stale_gold: bool = False,
) -> dict[str, object]:
    receipt = _cycle_receipt(evidence, approval_status)
    source_generated_at = (
        NOW - timedelta(minutes=31 if stale_gold else 10)
    ).isoformat()
    receipt.update(
        {
            "status": "silent",
            "would_send": False,
            "operator_action_required": False,
            "interrupt_operator": False,
            "action_required_count": 0,
            "novel_action_count": 0,
            "active_action_count_projected": 0,
            "active_action_count_after": 0,
            "actions": [],
        }
    )
    receipt.pop("message_preview")
    lanes = list(receipt["lanes"])
    lanes[0] = {
        "lane": "gold_live_runtime",
        "source_status": "ready",
        "action_required": False,
        "clear_verified": not stale_gold,
        "reason": "gold_receipt_not_fresh" if stale_gold else "gold_not_blocked",
        "source_generated_at": "",
        "observed_source_generated_at": source_generated_at,
    }
    receipt["lanes"] = lanes
    receipt["source_evidence_posture"] = cycle._build_source_evidence_posture(lanes)
    receipt["next_action"] = (
        str(receipt["source_evidence_posture"]["next_action"])
        if stale_gold
        else "await fresh approved signals"
    )
    return receipt


def _write_private(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_operator_status_binds_fresh_cycle_to_current_approved_snapshot(tmp_path: Path) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))

    result = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )

    assert result["status"] == "action_required"
    assert result["interrupt_operator"] is True
    assert result["blocking_reason"] == "live_runtime_host_admission_rejected"
    assert result["progress"] == {
        "approved_snapshot_verified": True,
        "action_required_count": 1,
        "novel_action_count": 1,
        "source_current_lane_count": 2,
        "source_stale_lane_count": 0,
        "source_unavailable_lane_count": 0,
        "current_snapshot_hashes_verified": True,
    }
    assert result["automatic_execution_allowed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["protected_operation_executed"] is False


def test_operator_status_cli_identifies_runtime_source(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    monkeypatch.setattr(operator_status, "_observed_now", lambda _now=None: NOW)

    result = operator_status.main(
        [
            "--receipt",
            str(receipt_path),
            "--signal-dir",
            str(signal_dir),
            "--presentation-state",
            str(tmp_path / "presentation.json"),
            "--source-type",
            "runtime_container",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["source"]["type"] == "runtime_container"
    assert payload["status"] == "action_required"


def test_operator_status_cli_records_then_suppresses_repeated_presentation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "presentation.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    monkeypatch.setattr(operator_status, "_observed_now", lambda _now=None: NOW)
    args = [
        "--receipt",
        str(receipt_path),
        "--signal-dir",
        str(signal_dir),
        "--presentation-state",
        str(presentation_path),
        "--record-presentation",
    ]

    assert operator_status.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "action_required"
    assert first["interrupt_operator"] is True
    assert first["presentation_receipt"]["status"] == "recorded"
    first_state = presentation_path.read_bytes()

    assert operator_status.main(args) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["status"] == "pending_action"
    assert repeated["action_required"] is True
    assert repeated["interrupt_operator"] is False
    assert repeated["presentation_receipt"]["status"] == "unchanged"
    assert repeated["presentation_receipt"]["presentation_recorded"] is False
    assert repeated["presentation_receipt"]["state_updated"] is False
    assert presentation_path.read_bytes() == first_state


def test_runtime_presentation_record_requires_the_rendered_cycle_hash(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "presentation.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    monkeypatch.setattr(operator_status, "_observed_now", lambda _now=None: NOW)
    base_args = [
        "--receipt",
        str(receipt_path),
        "--signal-dir",
        str(signal_dir),
        "--presentation-state",
        str(presentation_path),
        "--record-presentation",
        "--source-type",
        "runtime_container",
    ]

    assert operator_status.main(base_args) == 0
    unbound = json.loads(capsys.readouterr().out)
    assert unbound["presentation_receipt"]["status"] == "blocked"
    assert (
        unbound["presentation_receipt"]["blocking_reason"]
        == "presentation_source_binding_required"
    )
    assert not presentation_path.exists()

    assert operator_status.main(
        [
            *base_args,
            "--expected-cycle-receipt-sha256",
            "f" * 64,
        ]
    ) == 0
    mismatched = json.loads(capsys.readouterr().out)
    assert mismatched["presentation_receipt"]["status"] == "blocked"
    assert (
        mismatched["presentation_receipt"]["blocking_reason"]
        == "presentation_source_binding_mismatch"
    )
    assert not presentation_path.exists()

    expected_digest = str(mismatched["source_cycle_receipt_sha256"])
    assert operator_status.main(
        [
            *base_args,
            "--expected-cycle-receipt-sha256",
            expected_digest,
        ]
    ) == 0
    bound = json.loads(capsys.readouterr().out)
    assert bound["presentation_receipt"]["status"] == "recorded"
    assert (
        bound["presentation_receipt"]["source_cycle_receipt_sha256"]
        == expected_digest
    )
    assert stat.S_IMODE(presentation_path.stat().st_mode) == 0o600


def test_operator_status_rejects_stale_cycle_and_peer_writable_receipt(tmp_path: Path) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    receipt = _cycle_receipt(evidence, approval_status)
    receipt["generated_at"] = (NOW - timedelta(seconds=1801)).isoformat()
    _write_private(receipt_path, receipt)

    stale = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert stale["status"] == "blocked"
    assert stale["blocking_reason"] == "cycle_or_manifest_receipt_not_fresh"
    assert stale["interrupt_operator"] is False

    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    receipt_path.chmod(0o660)
    inadmissible = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert inadmissible["blocking_reason"] == "cycle_receipt_file_not_admissible"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o660


def test_operator_status_rejects_tampered_action_or_snapshot(tmp_path: Path) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    receipt = _cycle_receipt(evidence, approval_status)
    receipt["actions"][0]["safe_next_action"] = "run the deployment automatically"
    _write_private(receipt_path, receipt)

    action_tamper = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert action_tamper["blocking_reason"] == "cycle_action_contract_not_admissible"
    assert action_tamper["action_required"] is False

    posture_tamper_receipt = _cycle_receipt(evidence, approval_status)
    posture_tamper_receipt["source_evidence_posture"]["status"] = (
        "waiting_for_fresh_sources"
    )
    _write_private(receipt_path, posture_tamper_receipt)
    posture_tamper = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert posture_tamper["blocking_reason"] == "cycle_source_evidence_not_admissible"
    assert posture_tamper["interrupt_operator"] is False

    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    signal_path = signal_dir / approved.SIGNAL_FILENAMES["gold_receipt"]
    signal_path.write_bytes(signal_path.read_bytes() + b" ")
    signal_path.chmod(0o644)
    snapshot_tamper = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert snapshot_tamper["blocking_reason"] == "approved_signal_snapshot_not_admissible"
    assert snapshot_tamper["interrupt_operator"] is False


def test_operator_status_rejects_ambiguous_or_contradictory_cycle_envelopes(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    baseline = _silent_cycle_receipt(evidence, approval_status)
    forged_receipts: list[dict[str, object]] = []

    for mutation in (
        {"sent": True},
        {"delivery_attempted": True},
        {"notification_count": 1},
        {"message_ids": ["forged-message"]},
        {"delivery_mode": "forged_transport"},
        {"delivery_authorized": True},
        {"manual_apply_authorized": True},
        {"notification_state_status": "ready"},
        {"notification_state_admissible": False},
    ):
        forged = json.loads(json.dumps(baseline))
        forged.update(mutation)
        forged_receipts.append(forged)

    missing_envelope_field = json.loads(json.dumps(baseline))
    missing_envelope_field.pop("active_action_count_after")
    forged_receipts.append(missing_envelope_field)

    lane_authority = json.loads(json.dumps(baseline))
    lane_authority["lanes"][0]["manual_apply_authorized"] = True
    forged_receipts.append(lane_authority)

    evidence_authority = json.loads(json.dumps(baseline))
    evidence_authority["input_evidence"]["gold_receipt"][
        "provider_access_granted"
    ] = True
    forged_receipts.append(evidence_authority)

    for forged in forged_receipts:
        _write_private(receipt_path, forged)
        result = operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        )
        assert result["status"] == "blocked"
        assert result["blocking_reason"] == (
            "cycle_receipt_contract_not_admissible"
        )
        assert result["action_required"] is False
        assert result["interrupt_operator"] is False
        assert result["actions"] == []


def test_operator_status_preserves_genuine_action_when_incident_ledger_is_invalid(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    receipt = _cycle_receipt(evidence, approval_status)
    receipt.update(
        {
            "notification_state_status": "invalid",
            "notification_state_sha256": "c" * 64,
            "notification_state_admissible": False,
            "next_action": (
                "repair or explicitly replace the private notification incident ledger, then "
                "rerun with --send only when factual operator delivery remains authorized"
            ),
        }
    )
    _write_private(receipt_path, receipt)

    result = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )

    assert result["status"] == "action_required"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["blocking_reason"] == (
        "notification_state_not_admissible,live_runtime_host_admission_rejected"
    )
    assert result["notification_state"] == {
        "status": "invalid",
        "sha256": "c" * 64,
        "admissible": False,
        "state_updated": False,
    }
    assert result["automatic_execution_allowed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["protected_operation_executed"] is False


def test_operator_status_reports_quiet_invalid_incident_ledger_exactly(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    receipt = _silent_cycle_receipt(evidence, approval_status)
    receipt.update(
        {
            "status": "notification_state_invalid",
            "execution_mode": "evaluate_only",
            "notification_state_status": "invalid",
            "notification_state_sha256": "d" * 64,
            "notification_state_admissible": False,
            "next_action": (
                "repair or explicitly replace the private notification incident ledger before "
                "another send-enabled cycle"
            ),
        }
    )
    _write_private(receipt_path, receipt)

    result = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["blocking_reason"] == "notification_state_not_admissible"
    assert result["next_action"] == receipt["next_action"]
    assert result["notification_state"] == {
        "status": "invalid",
        "sha256": "d" * 64,
        "admissible": False,
        "state_updated": False,
    }


def test_operator_status_binds_approval_and_actions_to_exact_source_lane(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"

    forged_receipts: list[tuple[dict[str, object], str]] = []

    approval_extra = _cycle_receipt(evidence, approval_status)
    approval_extra["signal_approval"] = json.loads(
        json.dumps(approval_extra["signal_approval"])
    )
    approval_extra["signal_approval"]["delivery_authorized"] = True
    forged_receipts.append(
        (approval_extra, "cycle_approval_or_safety_contract_not_admissible")
    )

    action_extra = _cycle_receipt(evidence, approval_status)
    action_extra["actions"][0]["manual_apply_authorized"] = True
    forged_receipts.append((action_extra, "cycle_action_contract_not_admissible"))

    preview_tamper = _cycle_receipt(evidence, approval_status)
    preview_tamper["message_preview"] = "deploy now"
    forged_receipts.append(
        (preview_tamper, "cycle_action_contract_not_admissible")
    )

    wrong_lane = _cycle_receipt(evidence, approval_status)
    wrong_lane["actions"] = [
        {
            "lane": "scene_video_provider_refresh",
            "reason": "provider_account_material_required",
            "source_generated_at": (NOW - timedelta(minutes=10)).isoformat(),
            "safe_next_action": operator_status._SCENE_ACTION,
            "consent_required": True,
            "automatic_execution_allowed": False,
            "protected_operations": operator_status._PROTECTED_BY_LANE[
                "scene_video_provider_refresh"
            ],
            "provider_quota_consumption_allowed": False,
            "providers": [
                {
                    "provider": "magicfit",
                    "provider_label": "MagicFit",
                    "visible_account_gap": 1,
                    "action_reasons": ["provider_account_material_required"],
                }
            ],
        }
    ]
    forged_receipts.append((wrong_lane, "cycle_action_contract_not_admissible"))

    wrong_reason = _cycle_receipt(evidence, approval_status)
    wrong_reason["actions"][0]["reason"] = "live_runtime_prerequisite_blocked"
    wrong_reason["actions"][0]["safe_next_action"] = operator_status._GOLD_ACTIONS[
        "live_runtime_prerequisite_blocked"
    ]
    forged_receipts.append((wrong_reason, "cycle_action_contract_not_admissible"))

    wrong_timestamp = _cycle_receipt(evidence, approval_status)
    wrong_timestamp["actions"][0]["source_generated_at"] = (
        NOW - timedelta(minutes=9)
    ).isoformat()
    forged_receipts.append(
        (wrong_timestamp, "cycle_action_contract_not_admissible")
    )

    for forged, expected_reason in forged_receipts:
        _write_private(receipt_path, forged)
        result = operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        )
        assert result["status"] == "blocked"
        assert result["blocking_reason"] == expected_reason
        assert result["action_required"] is False
        assert result["interrupt_operator"] is False
        assert result["actions"] == []


def test_operator_status_requires_complete_bound_delivery_receipt(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"

    completed = _cycle_receipt(evidence, approval_status)
    completed.update(
        {
            "status": "completed",
            "execution_mode": "send",
            "delivery_authorized": True,
            "delivery_attempted": True,
            "sent": True,
            "would_send": False,
            "notification_count": 1,
            "delivery_mode": "principal_binding",
            "message_ids": ["6101"],
            "state_updated": True,
            "notification_state_status": "ready",
            "notification_state_sha256": "b" * 64,
            "notification_state_admissible": True,
            "active_action_count_after": 1,
            "next_action": (
                "await fresh approved signals; protected operations remain consent-gated"
            ),
        }
    )
    completed.pop("message_preview")
    _write_private(receipt_path, completed)
    verified = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert verified["status"] == "ready"
    assert verified["action_required"] is False
    assert verified["interrupt_operator"] is False
    assert verified["progress"]["delivery_receipt_verified"] is True
    assert verified["progress"]["delivered_action_count"] == 1

    malformed_variants = [
        {"notification_count": 0},
        {"delivery_mode": ""},
        {"message_ids": []},
        {"message_ids": ["6101", "6101"]},
        {"state_updated": False},
        {"active_action_count_after": 0},
        {"interrupt_operator": False},
        {"execution_mode": "evaluate_only"},
    ]
    for mutation in malformed_variants:
        malformed = json.loads(json.dumps(completed))
        malformed.update(mutation)
        _write_private(receipt_path, malformed)
        rejected = operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        )
        assert rejected["status"] == "blocked"
        assert rejected["blocking_reason"] == (
            "cycle_non_action_contract_not_admissible"
        )
        assert rejected["action_required"] is False
        assert rejected["interrupt_operator"] is False
        assert rejected["actions"] == []

    unknown_delivery_field = json.loads(json.dumps(completed))
    unknown_delivery_field["delivery_error_code"] = "forged_success"
    _write_private(receipt_path, unknown_delivery_field)
    rejected_unknown = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )
    assert rejected_unknown["status"] == "blocked"
    assert rejected_unknown["blocking_reason"] == (
        "cycle_receipt_contract_not_admissible"
    )


def test_operator_status_suppresses_fresh_verified_silent_cycle(tmp_path: Path) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    receipt = _silent_cycle_receipt(evidence, approval_status)
    _write_private(receipt_path, receipt)

    result = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        now=NOW,
    )

    assert result["status"] == "ready"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["blocking_reason"] == ""
    assert result["next_action"] == "await fresh approved signals"


def test_presentation_ledger_suppresses_only_a_repeated_operator_interrupt(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    delivery_state_path = tmp_path / "delivery-state.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))

    first = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert first["status"] == "action_required"
    assert first["action_required"] is True
    assert first["interrupt_operator"] is True
    assert first["presentation"]["state_status"] == "missing"
    assert first["presentation"]["novel_action_count"] == 1
    assert len(first["actions"][0]["presentation_digest"]) == 64

    presentation_receipt = operator_status.record_operator_presentation(
        first,
        state_path=presentation_path,
        now=NOW,
    )
    assert presentation_receipt["status"] == "recorded"
    assert presentation_receipt["delivery_state_updated"] is False
    assert presentation_receipt["provider_quota_consumed"] is False
    assert presentation_receipt["protected_operation_executed"] is False
    assert stat.S_IMODE(presentation_path.stat().st_mode) == 0o600
    assert not delivery_state_path.exists()
    first_state = presentation_path.read_bytes()

    repeated = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert repeated["status"] == "pending_action"
    assert repeated["action_required"] is True
    assert repeated["interrupt_operator"] is False
    assert repeated["actions"] == []
    assert len(repeated["pending_actions"]) == 1
    assert repeated["presentation"]["state_status"] == "ready"
    assert repeated["presentation"]["novel_action_count"] == 0
    assert repeated["presentation"]["delivery_state_independent"] is True
    assert not delivery_state_path.exists()
    repeated_receipt = operator_status.record_operator_presentation(
        repeated,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=1),
    )
    assert repeated_receipt["status"] == "unchanged"
    assert repeated_receipt["presentation_recorded"] is False
    assert repeated_receipt["state_updated"] is False
    assert presentation_path.read_bytes() == first_state


def test_invalid_presentation_ledger_fails_closed_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    _write_private(
        presentation_path,
        {
            "schema": operator_status.PRESENTATION_STATE_SCHEMA,
            "active_presentations": {
                "gold_live_runtime": {"action_digest": "not-a-digest"}
            },
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "protected_operation_executed": False,
        },
    )
    original = presentation_path.read_bytes()

    result = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert result["status"] == "blocked"
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert result["blocking_reason"] == (
        "operator_presentation_state_not_admissible"
    )
    assert result["next_action"] == (
        "repair or explicitly replace the private operator-presentation "
        "ledger before presenting another action"
    )
    assert result["actions"] == []
    assert result["presentation"]["state_status"] == "invalid"

    record = operator_status.record_operator_presentation(
        result,
        state_path=presentation_path,
        now=NOW,
    )
    assert record["status"] == "blocked"
    assert record["blocking_reason"] == "operator_projection_not_admissible"
    assert presentation_path.read_bytes() == original


def test_presentation_ledger_requires_exact_schema_and_ordered_timestamps(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    initial = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert operator_status.record_operator_presentation(
        initial,
        state_path=presentation_path,
        now=NOW,
    )["status"] == "recorded"
    valid_state = json.loads(presentation_path.read_text(encoding="utf-8"))

    forged_states: list[dict[str, object]] = []
    extra_authority = json.loads(json.dumps(valid_state))
    extra_authority["manual_apply_authorized"] = True
    forged_states.append(extra_authority)

    unknown_lane = json.loads(json.dumps(valid_state))
    unknown_lane["active_presentations"]["untrusted_lane"] = unknown_lane[
        "active_presentations"
    ].pop("gold_live_runtime")
    forged_states.append(unknown_lane)

    row_authority = json.loads(json.dumps(valid_state))
    row_authority["active_presentations"]["gold_live_runtime"][
        "delivery_authorized"
    ] = True
    forged_states.append(row_authority)

    reversed_timestamps = json.loads(json.dumps(valid_state))
    reversed_timestamps["active_presentations"]["gold_live_runtime"][
        "first_presented_at"
    ] = (NOW + timedelta(seconds=1)).isoformat()
    forged_states.append(reversed_timestamps)

    row_after_state = json.loads(json.dumps(valid_state))
    row_after_state["active_presentations"]["gold_live_runtime"][
        "last_presented_at"
    ] = (NOW + timedelta(seconds=1)).isoformat()
    forged_states.append(row_after_state)

    invalid_state_timestamp = json.loads(json.dumps(valid_state))
    invalid_state_timestamp["updated_at"] = "not-a-timestamp"
    forged_states.append(invalid_state_timestamp)

    future_state_timestamp = json.loads(json.dumps(valid_state))
    future_state_timestamp["updated_at"] = (
        NOW + timedelta(seconds=31)
    ).isoformat()
    forged_states.append(future_state_timestamp)

    invalid_source_binding = json.loads(json.dumps(valid_state))
    invalid_source_binding["source_cycle_receipt_sha256"] = "not-a-digest"
    forged_states.append(invalid_source_binding)

    for forged_state in forged_states:
        _write_private(presentation_path, forged_state)
        forged_bytes = presentation_path.read_bytes()
        result = operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            presentation_state_path=presentation_path,
            now=NOW,
        )
        assert result["status"] == "blocked"
        assert result["action_required"] is False
        assert result["interrupt_operator"] is False
        assert result["blocking_reason"] == (
            "operator_presentation_state_not_admissible"
        )
        assert result["actions"] == []
        assert result["presentation"]["state_status"] == "invalid"
        record = operator_status.record_operator_presentation(
            result,
            state_path=presentation_path,
            now=NOW,
        )
        assert record["status"] == "blocked"
        assert record["blocking_reason"] == (
            "operator_projection_not_admissible"
        )
        assert presentation_path.read_bytes() == forged_bytes


def test_presented_clear_snapshot_rearms_the_same_future_action(tmp_path: Path) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    action_receipt = _cycle_receipt(evidence, approval_status)
    _write_private(receipt_path, action_receipt)
    action = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert operator_status.record_operator_presentation(
        action,
        state_path=presentation_path,
        now=NOW,
    )["status"] == "recorded"

    clear_receipt = _silent_cycle_receipt(evidence, approval_status)
    _write_private(receipt_path, clear_receipt)
    clear = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert clear["status"] == "ready"
    clear_record = operator_status.record_operator_presentation(
        clear,
        state_path=presentation_path,
        now=NOW,
    )
    assert clear_record["status"] == "recorded"
    assert clear_record["presentation_recorded"] is False
    assert clear_record["cleared_presentation_count"] == 1
    assert clear_record["state_updated"] is True

    _write_private(receipt_path, action_receipt)
    rearmed = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert rearmed["status"] == "action_required"
    assert rearmed["interrupt_operator"] is True
    assert rearmed["presentation"]["novel_action_count"] == 1


def test_stale_evidence_retains_presentation_identity_without_reinterrupt(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    action_receipt = _cycle_receipt(evidence, approval_status)
    _write_private(receipt_path, action_receipt)
    action = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert operator_status.record_operator_presentation(
        action,
        state_path=presentation_path,
        now=NOW,
    )["status"] == "recorded"
    recorded_state = presentation_path.read_bytes()

    waiting_receipt = _silent_cycle_receipt(
        evidence,
        approval_status,
        stale_gold=True,
    )
    _write_private(receipt_path, waiting_receipt)
    waiting = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )

    assert waiting["status"] == "waiting_for_evidence"
    assert waiting["action_required"] is False
    assert waiting["interrupt_operator"] is False
    assert waiting["blocking_reason"] == "approved_source_evidence_not_current"
    assert waiting["source_evidence"]["status"] == "waiting_for_fresh_sources"
    assert waiting["presentation"]["retained_context_lanes"] == [
        "gold_live_runtime"
    ]
    waiting_record = operator_status.record_operator_presentation(
        waiting,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=1),
    )
    assert waiting_record["status"] == "unchanged"
    assert waiting_record["freshness_gap_retained"] is True
    assert waiting_record["active_presentation_count"] == 1
    assert waiting_record["state_updated"] is False
    assert presentation_path.read_bytes() == recorded_state

    _write_private(receipt_path, action_receipt)
    recovered = operator_status.load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
        presentation_state_path=presentation_path,
        now=NOW,
    )
    assert recovered["status"] == "pending_action"
    assert recovered["action_required"] is True
    assert recovered["interrupt_operator"] is False
    assert recovered["presentation"]["novel_action_count"] == 0


def test_presentation_novelty_binds_semantic_context_but_ignores_receipt_churn(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    receipt = _cycle_receipt(evidence, approval_status)
    _write_private(receipt_path, receipt)
    context = {
        "schema": "test.operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "current_probe_origin": "http://127.0.0.1:8090",
            "proposed_probe_origin": "http://127.0.0.1:8097",
            "compose_project": "property",
        },
    }

    first = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={"gold_live_runtime": context},
    )
    assert first["interrupt_operator"] is True
    assert len(first["actions"][0]["presentation_context_digest"]) == 64
    first_record = operator_status.record_operator_presentation(
        first,
        state_path=presentation_path,
        now=NOW,
    )
    assert first_record["status"] == "recorded"
    first_state = presentation_path.read_bytes()
    first_source_digest = str(first["source_cycle_receipt_sha256"])

    refreshed_receipt = _cycle_receipt(evidence, approval_status)
    refreshed_source_generated_at = (NOW - timedelta(minutes=9)).isoformat()
    refreshed_receipt["actions"][0][
        "source_generated_at"
    ] = refreshed_source_generated_at
    refreshed_receipt["lanes"][0][
        "source_generated_at"
    ] = refreshed_source_generated_at
    refreshed_receipt["lanes"][0][
        "observed_source_generated_at"
    ] = refreshed_source_generated_at
    refreshed_receipt["source_evidence_posture"] = (
        cycle._build_source_evidence_posture(refreshed_receipt["lanes"])
    )
    refreshed_receipt["message_preview"] = cycle._build_consolidated_message(
        list(refreshed_receipt["actions"]),
        generated_at=str(refreshed_receipt["generated_at"]),
    )
    _write_private(receipt_path, refreshed_receipt)
    refreshed = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={"gold_live_runtime": dict(context)},
    )
    assert refreshed["status"] == "pending_action"
    assert refreshed["interrupt_operator"] is False
    refreshed_record = operator_status.record_operator_presentation(
        refreshed,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=1),
    )
    assert refreshed_record["status"] == "unchanged"
    assert refreshed_record["source_cycle_receipt_sha256"] == refreshed[
        "source_cycle_receipt_sha256"
    ]
    assert refreshed_record["state_source_cycle_receipt_sha256"] == (
        first_source_digest
    )
    assert refreshed_record["state_updated"] is False
    assert presentation_path.read_bytes() == first_state

    unavailable_context = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={},
    )
    assert unavailable_context["status"] == "pending_action"
    assert unavailable_context["interrupt_operator"] is False
    assert unavailable_context["presentation"]["novel_action_count"] == 0
    assert unavailable_context["presentation"]["retained_context_lanes"] == [
        "gold_live_runtime"
    ]
    assert unavailable_context["pending_actions"][0][
        "presentation_context_digest"
    ] == first["actions"][0]["presentation_context_digest"]
    unavailable_record = operator_status.record_operator_presentation(
        unavailable_context,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=1),
    )
    assert unavailable_record["status"] == "unchanged"
    assert unavailable_record["state_updated"] is False
    assert presentation_path.read_bytes() == first_state

    changed_context = json.loads(json.dumps(context))
    changed_context["scope"]["proposed_probe_origin"] = "http://127.0.0.1:8098"
    changed = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={"gold_live_runtime": changed_context},
    )
    assert changed["status"] == "action_required"
    assert changed["interrupt_operator"] is True
    assert changed["presentation"]["novel_action_count"] == 1

    unrendered = json.loads(json.dumps(changed))
    unrendered["actions"] = []
    unrendered["interrupt_operator"] = False
    unrendered_record = operator_status.record_operator_presentation(
        unrendered,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=2),
    )
    assert unrendered_record["status"] == "blocked"
    assert (
        unrendered_record["blocking_reason"]
        == "presentation_render_binding_mismatch"
    )
    assert presentation_path.read_bytes() == first_state

    tampered = json.loads(json.dumps(changed))
    tampered["pending_actions"][0]["presentation_context_digest"] = "f" * 64
    rejected = operator_status.record_operator_presentation(
        tampered,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=2),
    )
    assert rejected["status"] == "blocked"
    assert rejected["blocking_reason"] == "presentation_action_not_admissible"
    assert presentation_path.read_bytes() == first_state

    changed_record = operator_status.record_operator_presentation(
        changed,
        state_path=presentation_path,
        now=NOW + timedelta(minutes=2),
    )
    assert changed_record["status"] == "recorded"
    assert changed_record["presentation_recorded"] is True
    assert changed_record["state_updated"] is True
    assert presentation_path.read_bytes() != first_state


def test_presentation_context_hydrates_without_rearming_action_only_baseline(
    tmp_path: Path,
) -> None:
    signal_dir, evidence, approval_status = _approved_snapshot(tmp_path)
    receipt_path = tmp_path / "cycle.json"
    presentation_path = tmp_path / "operator-presentation.json"
    _write_private(receipt_path, _cycle_receipt(evidence, approval_status))
    first = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={},
    )
    assert operator_status.record_operator_presentation(
        first,
        state_path=presentation_path,
        now=NOW,
    )["status"] == "recorded"
    first_state = json.loads(presentation_path.read_text())
    context = {
        "schema": "test.operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "current_probe_origin": "http://127.0.0.1:8090",
            "proposed_probe_origin": "http://127.0.0.1:8097",
            "compose_project": "property",
        },
    }

    hydrated = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={"gold_live_runtime": context},
    )

    assert hydrated["status"] == "pending_action"
    assert hydrated["interrupt_operator"] is False
    assert hydrated["actions"] == []
    assert hydrated["presentation"]["context_hydration_lanes"] == [
        "gold_live_runtime"
    ]
    tampered = json.loads(json.dumps(hydrated))
    tampered["presentation"]["context_hydration_lanes"] = []
    rejected = operator_status.record_operator_presentation(
        tampered,
        state_path=presentation_path,
        now=NOW + timedelta(seconds=1),
    )
    assert rejected["status"] == "blocked"
    assert (
        rejected["blocking_reason"]
        == "presentation_context_hydration_not_admissible"
    )
    hydration_record = operator_status.record_operator_presentation(
        hydrated,
        state_path=presentation_path,
        now=NOW + timedelta(seconds=1),
    )
    hydrated_state = json.loads(presentation_path.read_text())
    assert hydration_record["status"] == "recorded"
    assert hydration_record["presentation_recorded"] is False
    assert hydration_record["context_hydrated_count"] == 1
    assert hydration_record["state_updated"] is True
    assert hydrated_state["active_presentations"]["gold_live_runtime"][
        "first_presented_at"
    ] == first_state["active_presentations"]["gold_live_runtime"][
        "first_presented_at"
    ]
    assert len(
        hydrated_state["active_presentations"]["gold_live_runtime"][
            "presentation_context_digest"
        ]
    ) == 64

    changed_context = json.loads(json.dumps(context))
    changed_context["scope"]["proposed_probe_origin"] = "http://127.0.0.1:8098"
    changed = operator_status.apply_operator_presentation_state(
        operator_status.load_operator_status(
            cycle_receipt_path=receipt_path,
            signal_dir=signal_dir,
            now=NOW,
        ),
        state_path=presentation_path,
        presentation_context_by_lane={"gold_live_runtime": changed_context},
    )
    assert changed["status"] == "action_required"
    assert changed["interrupt_operator"] is True
    assert changed["presentation"]["novel_action_count"] == 1
