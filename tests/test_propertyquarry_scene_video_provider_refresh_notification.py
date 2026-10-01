from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import propertyquarry_notify_scene_video_provider_refresh as notify_refresh


NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
PACKET_GENERATED_AT = "2026-08-26T03:45:00+00:00"
SOURCE_GENERATED_AT = "2026-08-26T03:44:59+00:00"
RUNTIME_GENERATED_AT = "2026-08-26T03:45:05+00:00"
SOURCE_REF = "/data/artifacts/property-scene-video-readiness.json"
RAW_BLOCKER = "secret-provider-blocker-must-not-be-notified"
RAW_COMMAND = "merge_scene_video_provider_accounts_env.py --secret-value must-not-be-notified"


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _packet(
    *,
    generated_at: str = PACKET_GENERATED_AT,
    source_generated_at: str = SOURCE_GENERATED_AT,
    actionable: bool = True,
    credit_review: bool = False,
) -> dict[str, object]:
    gap = 1 if actionable and not credit_review else 0
    return {
        "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
        "generated_at": generated_at,
        "source_receipt": SOURCE_REF,
        "source_receipt_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_receipt_generated_at": source_generated_at,
        "providers": [
            {
                "provider": "magicfit",
                "expected_account_count": 2,
                "tracked_account_count": 3,
                "unavailable_account_count": 1 if credit_review else 0,
                "runtime_account_count": 2 - gap,
                "visible_account_gap": gap,
                "credit_state": "constrained" if credit_review else "funded",
                "credit_refresh_required": credit_review,
                "runtime_blockers": [RAW_BLOCKER],
                "post_refresh_checks": [RAW_COMMAND],
            },
            {
                "provider": "omagic",
                "expected_account_count": 8,
                "runtime_account_count": 8,
                "visible_account_gap": 0,
                "credit_state": "funded",
                "credit_refresh_required": False,
                "runtime_blockers": [],
            },
        ],
    }


def _verifier(
    *,
    status: str = "pass",
    generated_at: str = PACKET_GENERATED_AT,
) -> dict[str, object]:
    return {
        "status": status,
        "generated_at": generated_at,
        "provider_count": 2,
        "checked_providers": ["magicfit", "omagic"],
        "blockers": [],
    }


def _runtime_status(
    *,
    generated_at: str = RUNTIME_GENERATED_AT,
    confirms_action: bool = True,
) -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_runtime_status.v1",
        "generated_at": generated_at,
        "source_contract_name": "propertyquarry.scene_video_readiness.v1",
        "source_kind": "receipt_file",
        "source_ref": SOURCE_REF,
        "summary": {
            "provider_count": 2,
            "ready_count": 1 if confirms_action else 2,
            "blocked_count": 1 if confirms_action else 0,
            "action_required_count": 1 if confirms_action else 0,
            "action_required_providers": ["magicfit"] if confirms_action else [],
        },
        "providers": [
            {
                "provider": "magicfit",
                "status": "blocked" if confirms_action else "ready",
                "attention_required": confirms_action,
            },
            {
                "provider": "omagic",
                "status": "ready",
                "attention_required": False,
            },
        ],
    }


def _build_report(
    *,
    tmp_path: Path,
    packet: dict[str, object] | None = None,
    verifier: dict[str, object] | None = None,
    runtime_status: dict[str, object] | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    packet = packet or _packet()
    verifier = verifier or _verifier()
    runtime_status = runtime_status or _runtime_status()
    packet_path = _write_json(tmp_path / "packet.json", packet)
    verifier_path = _write_json(tmp_path / "verifier.json", verifier)
    runtime_status_path = _write_json(tmp_path / "runtime.json", runtime_status)
    return notify_refresh.build_notification_report(
        packet=packet,
        packet_path=packet_path,
        verifier=verifier,
        verifier_path=verifier_path,
        runtime_status=runtime_status,
        runtime_status_path=runtime_status_path,
        state_path=state_path or tmp_path / "state.json",
        principal_id="cf-email:tibor.girschele@gmail.com",
        base_url="https://propertyquarry.com",
        force=False,
        now=NOW,
        dry_run=dry_run,
    )


def test_scene_video_notification_suppresses_failed_verifier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("failed verifier must stay silent")),
    )

    report = _build_report(tmp_path=tmp_path, verifier=_verifier(status="fail"))

    assert report["sent"] is False
    assert report["action_required"] is False
    assert report["action_reason"] == "packet_verifier_not_pass"
    assert report["skipped_reason"] == "no_fresh_operator_action"
    assert not (tmp_path / "state.json").exists()


def test_scene_video_notification_suppresses_stale_sources(tmp_path: Path, monkeypatch) -> None:
    old_packet_time = "2026-08-26T03:29:00+00:00"
    old_runtime_time = "2026-08-26T03:29:05+00:00"
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("stale source must stay silent")),
    )

    report = _build_report(
        tmp_path=tmp_path,
        packet=_packet(generated_at=old_packet_time),
        verifier=_verifier(generated_at=old_packet_time),
        runtime_status=_runtime_status(generated_at=old_runtime_time),
    )

    assert report["action_reason"] == "source_receipt_not_fresh"
    assert report["source_verified"] is False
    assert report["sent"] is False


def test_scene_video_notification_requires_coherent_packet_verifier_pair(tmp_path: Path) -> None:
    report = _build_report(
        tmp_path=tmp_path,
        verifier=_verifier(generated_at="2026-08-26T03:45:01+00:00"),
    )

    assert report["action_reason"] == "packet_verifier_not_coherent"
    assert report["action_required"] is False


def test_scene_video_notification_requires_runtime_confirmation(tmp_path: Path) -> None:
    report = _build_report(
        tmp_path=tmp_path,
        runtime_status=_runtime_status(confirms_action=False),
    )

    assert report["action_reason"] == "runtime_does_not_confirm_operator_action"
    assert report["action_required"] is False


def test_scene_video_notification_rejects_unbound_or_malformed_source_projection(tmp_path: Path) -> None:
    unbound_runtime = _runtime_status()
    unbound_runtime["source_ref"] = "/data/artifacts/different-readiness.json"
    malformed_packet = _packet()
    malformed_packet["providers"][0]["visible_account_gap"] = 2

    unbound = _build_report(tmp_path=tmp_path, runtime_status=unbound_runtime)
    malformed = _build_report(tmp_path=tmp_path, packet=malformed_packet)

    assert unbound["action_reason"] == "source_receipt_provenance_not_admissible"
    assert malformed["action_reason"] == "packet_provider_counts_not_admissible"
    assert unbound["action_required"] is False
    assert malformed["action_required"] is False


def test_scene_video_notification_sends_only_sanitized_action(tmp_path: Path, monkeypatch) -> None:
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: sent.update(kwargs)
        or {"delivery_mode": "principal_binding", "message_ids": ["5091"]},
    )
    state_path = tmp_path / "state.json"

    report = _build_report(tmp_path=tmp_path, state_path=state_path)

    assert report["sent"] is True
    assert report["would_send"] is True
    assert report["delivery_mode"] == "principal_binding"
    assert report["action_reason"] == "provider_account_material_required"
    assert report["consent_required"] is True
    assert report["automatic_execution_allowed"] is False
    assert report["provider_quota_consumption_allowed"] is False
    text = str(sent["text"])
    assert "PropertyQuarry scene-video operator action required." in text
    assert "provider_account_material_required" in text
    assert "automatic execution disabled" in text
    assert "Provider quota: disabled" in text
    assert RAW_BLOCKER not in text
    assert RAW_COMMAND not in text
    assert RAW_BLOCKER not in json.dumps(report)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_observed_status"] == "action_required"
    assert state["active_action_digest"] == report["action_digest"]
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_scene_video_notification_keeps_credit_change_consent_gated(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: observed.update(kwargs)
        or {"delivery_mode": "principal_binding", "message_ids": ["5092"]},
    )

    report = _build_report(tmp_path=tmp_path, packet=_packet(credit_review=True))

    assert report["sent"] is True
    assert report["action_reason"] == "provider_credit_review_required"
    assert report["automatic_execution_allowed"] is False
    assert report["provider_quota_consumption_allowed"] is False
    assert "provider_credit_or_plan_change" in str(observed["text"])


def test_scene_video_notification_dedupes_same_active_action_across_receipt_refreshes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prior_packet = _packet(
        generated_at="2026-08-26T03:05:00+00:00",
        source_generated_at="2026-08-26T03:04:59+00:00",
    )
    prior_verifier = _verifier(generated_at="2026-08-26T03:05:00+00:00")
    prior_runtime = _runtime_status(generated_at="2026-08-26T03:05:05+00:00")
    prior_action = notify_refresh.scene_video_operator_action_summary(
        packet=prior_packet,
        verifier=prior_verifier,
        runtime_status=prior_runtime,
        now=NOW,
    )
    state_path = tmp_path / "state.json"
    notify_refresh.gold_notify._write_notification_state(
        state_path,
        {"active_action_digest": notify_refresh._action_digest(prior_action)},
    )
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("same active action must stay silent")),
    )

    report = _build_report(tmp_path=tmp_path, state_path=state_path)

    assert report["sent"] is False
    assert report["skipped_reason"] == "already_notified_same_action"


def test_scene_video_notification_fresh_clear_allows_later_recurrence(tmp_path: Path, monkeypatch) -> None:
    action = notify_refresh.scene_video_operator_action_summary(
        packet=_packet(),
        verifier=_verifier(),
        runtime_status=_runtime_status(),
        now=NOW,
    )
    state_path = tmp_path / "state.json"
    notify_refresh.gold_notify._write_notification_state(
        state_path,
        {"active_action_digest": notify_refresh._action_digest(action)},
    )

    clear_report = _build_report(
        tmp_path=tmp_path,
        packet=_packet(actionable=False),
        runtime_status=_runtime_status(confirms_action=False),
        state_path=state_path,
    )

    assert clear_report["sent"] is False
    assert clear_report["action_reason"] == "no_actionable_provider_refresh"
    assert clear_report["state_updated"] is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_observed_status"] == "clear"
    assert state["active_action_digest"] == ""

    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: {"delivery_mode": "principal_binding", "message_ids": ["5093"]},
    )
    recurrence = _build_report(tmp_path=tmp_path, state_path=state_path)
    assert recurrence["sent"] is True


def test_scene_video_notification_dry_run_never_sends_or_changes_state(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("dry run must not deliver")),
    )

    report = _build_report(
        tmp_path=tmp_path,
        state_path=state_path,
        dry_run=True,
    )

    assert report["sent"] is False
    assert report["would_send"] is True
    assert report["skipped_reason"] == "dry_run"
    assert RAW_BLOCKER not in str(report["message_preview"])
    assert RAW_COMMAND not in str(report["message_preview"])
    assert not state_path.exists()


def test_scene_video_notification_cli_dry_run_writes_private_sanitized_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    packet_time = datetime.now(timezone.utc).replace(microsecond=0)
    runtime_time = packet_time + timedelta(seconds=5)
    packet = _packet(
        generated_at=packet_time.isoformat(),
        source_generated_at=(packet_time - timedelta(seconds=1)).isoformat(),
    )
    verifier = _verifier(generated_at=packet_time.isoformat())
    runtime_status = _runtime_status(generated_at=runtime_time.isoformat())
    packet_path = _write_json(tmp_path / "packet.json", packet)
    verifier_path = _write_json(tmp_path / "verifier.json", verifier)
    runtime_path = _write_json(tmp_path / "runtime.json", runtime_status)
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(
        notify_refresh.gold_notify,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("CLI dry run must not deliver")),
    )

    exit_code = notify_refresh.main(
        [
            "--packet",
            str(packet_path),
            "--verifier",
            str(verifier_path),
            "--runtime-status",
            str(runtime_path),
            "--state-file",
            str(state_path),
            "--write",
            str(report_path),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["skipped_reason"] == "dry_run"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sent"] is False
    assert report["would_send"] is True
    assert RAW_BLOCKER not in json.dumps(report)
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert not state_path.exists()
