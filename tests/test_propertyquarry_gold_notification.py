from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

from scripts import propertyquarry_notify_gold_status as notify_gold_status


NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
SOURCE_GENERATED_AT = "2026-08-26T03:35:17.410758+00:00"
RAW_RUNTIME_BLOCKER = "secret-runtime-detail-must-not-be-notified"


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _actionable_payload(*, generated_at: str = "2026-08-26T03:37:58+00:00") -> dict[str, object]:
    return {
        "status": "blocked",
        "generated_at": generated_at,
        "live_mobile_surfaces": {
            "status": "blocked",
            "route_probe_blocked": True,
            "runtime_blocker": RAW_RUNTIME_BLOCKER,
            "operator_action": {
                "required": True,
                "interrupt_operator": True,
                "source_fresh": True,
                "notification_policy": "action_required_only",
                "provider_quota_consumption_allowed": False,
                "reason": "live_runtime_host_admission_rejected",
                "source_generated_at": SOURCE_GENERATED_AT,
                "reversible_next_action": RAW_RUNTIME_BLOCKER,
                "runtime_blocker": RAW_RUNTIME_BLOCKER,
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


def _build_report(
    *,
    payload: dict[str, object],
    receipt_path: Path,
    state_path: Path,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    return notify_gold_status.build_notification_report(
        payload=payload,
        receipt_path=receipt_path,
        state_path=state_path,
        principal_id="cf-email:tibor.girschele@gmail.com",
        base_url="https://propertyquarry.com",
        force=force,
        now=NOW,
        dry_run=dry_run,
    )


def test_gold_notification_receipt_path_prefers_canonical_latest_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    canonical = _write_json(
        tmp_path / "_completion" / "property_gold_status" / "latest.json",
        {"status": "pass"},
    )
    legacy = _write_json(
        tmp_path / "_completion" / "propertyquarry-gold-status-latest.json",
        {"status": "blocked"},
    )

    resolved = notify_gold_status._resolve_receipt_path("_completion/property_gold_status/latest.json")

    assert resolved == canonical.resolve()
    assert resolved != legacy.resolve()


def test_gold_notification_receipt_path_falls_back_to_legacy_latest_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = _write_json(
        tmp_path / "_completion" / "propertyquarry-gold-status-latest.json",
        {"status": "pass"},
    )

    resolved = notify_gold_status._resolve_receipt_path("_completion/property_gold_status/latest.json")

    assert resolved == legacy.resolve()


def test_gold_notification_keeps_green_receipt_silent(tmp_path: Path, monkeypatch) -> None:
    payload = {
        "status": "pass",
        "ready_for_notification": True,
        "generated_at": "2026-08-26T03:37:58+00:00",
        "pass_areas": [{"area": "live_mobile_surfaces"}],
    }
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("green status must stay silent")),
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is False
    assert report["action_required"] is False
    assert report["skipped_reason"] == "no_fresh_operator_action"
    assert not state_path.exists()


def test_gold_notification_sends_sanitized_fresh_operator_action(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: sent.update(kwargs) or {"delivery_mode": "principal_binding", "message_ids": ["3097"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert report["would_send"] is True
    assert report["notification_kind"] == "operator_action_required"
    assert report["action_reason"] == "live_runtime_host_admission_rejected"
    assert report["consent_required"] is True
    assert report["automatic_execution_allowed"] is False
    assert report["provider_quota_consumption_allowed"] is False
    assert sent["url_buttons"] == [[("Open PropertyQuarry", "https://propertyquarry.com")]]
    assert "PropertyQuarry operator action required." in str(sent["text"])
    assert "automatic execution disabled" in str(sent["text"])
    assert RAW_RUNTIME_BLOCKER not in str(sent["text"])
    assert RAW_RUNTIME_BLOCKER not in json.dumps(report)
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["last_notified_status"] == "action_required"
    assert state_payload["last_receipt_status"] == "blocked"
    assert state_payload["message_ids"] == ["3097"]
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_gold_notification_falls_back_to_direct_chat_when_binding_missing(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    sent: dict[str, object] = {}
    monkeypatch.setattr(notify_gold_status, "build_tool_runtime", lambda: object())
    monkeypatch.setattr(
        notify_gold_status,
        "send_telegram_message_for_principal",
        lambda runtime, **kwargs: (_ for _ in ()).throw(RuntimeError("telegram_binding_not_found")),
    )
    monkeypatch.setenv("EA_PROACTIVE_OODA_TELEGRAM_CHAT_ID", "1354554303")
    monkeypatch.setattr(
        notify_gold_status,
        "_send_direct_telegram_message",
        lambda **kwargs: sent.update(kwargs) or {"message_ids": ["4201"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert report["delivery_mode"] == "direct_chat_fallback"
    assert report["message_ids"] == ["4201"]
    assert sent["chat_id"] == "1354554303"


def test_gold_notification_falls_back_to_direct_chat_when_runtime_bootstrap_fails(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        notify_gold_status,
        "build_tool_runtime",
        lambda: (_ for _ in ()).throw(RuntimeError("failed to resolve host 'ea-db'")),
    )
    monkeypatch.setattr(
        notify_gold_status,
        "_send_container_runtime_telegram_message",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("container runtime unavailable")),
    )
    monkeypatch.setenv("EA_PROACTIVE_OODA_TELEGRAM_CHAT_ID", "1354554303")
    monkeypatch.setattr(
        notify_gold_status,
        "_send_direct_telegram_message",
        lambda **kwargs: sent.update(kwargs) or {"message_ids": ["4202"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert report["delivery_mode"] == "direct_chat_fallback"
    assert report["runtime_error"] == "RuntimeError: failed to resolve host 'ea-db'"
    assert report["container_runtime_error"] == "RuntimeError: container runtime unavailable"
    assert sent["chat_id"] == "1354554303"


def test_gold_notification_falls_back_to_container_runtime_when_host_runtime_bootstrap_fails(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        notify_gold_status,
        "build_tool_runtime",
        lambda: (_ for _ in ()).throw(RuntimeError("failed to resolve host 'ea-db'")),
    )
    monkeypatch.setattr(
        notify_gold_status,
        "_send_container_runtime_telegram_message",
        lambda **kwargs: observed.update(kwargs) or {"message_ids": ["4301"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert report["delivery_mode"] == "container_runtime_fallback"
    assert report["runtime_error"] == "RuntimeError: failed to resolve host 'ea-db'"
    assert observed["principal_id"] == "cf-email:tibor.girschele@gmail.com"


def test_gold_notification_prefers_container_runtime_when_enabled(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    observed: dict[str, object] = {}
    monkeypatch.setenv("PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME", "1")
    monkeypatch.setattr(
        notify_gold_status,
        "build_tool_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("should not build host runtime")),
    )
    monkeypatch.setattr(
        notify_gold_status,
        "_send_container_runtime_telegram_message",
        lambda **kwargs: observed.update(kwargs) or {"message_ids": ["4302"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert report["delivery_mode"] == "container_runtime_preferred"
    assert report["message_ids"] == ["4302"]
    assert observed["principal_id"] == "cf-email:tibor.girschele@gmail.com"


def test_gold_notification_dedupes_same_operator_action(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    action = notify_gold_status.propertyquarry_operator_action_summary(payload, now=NOW)
    _write_json(state_path, {"last_notified_digest": notify_gold_status._payload_digest(action)}).chmod(0o600)
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("duplicate action must stay silent")),
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is False
    assert report["would_send"] is False
    assert report["skipped_reason"] == "already_notified_same_digest"


def test_gold_notification_dedupes_same_action_across_unrelated_gold_changes(tmp_path: Path, monkeypatch) -> None:
    prior_payload = _actionable_payload(generated_at="2026-08-26T03:37:58+00:00")
    current_payload = _actionable_payload(generated_at="2026-08-26T03:57:58+00:00")
    current_payload["pass_areas"] = [{"area": "unrelated_gold_refresh"}]
    receipt_path = _write_json(tmp_path / "gold.json", current_payload)
    state_path = tmp_path / "state.json"
    prior_action = notify_gold_status.propertyquarry_operator_action_summary(prior_payload, now=NOW)
    _write_json(state_path, {"last_notified_digest": notify_gold_status._payload_digest(prior_action)}).chmod(0o600)
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("same action must stay silent")),
    )

    report = _build_report(payload=current_payload, receipt_path=receipt_path, state_path=state_path)

    assert report["skipped_reason"] == "already_notified_same_digest"


def test_gold_notification_does_not_trust_insecure_dedupe_state(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    action = notify_gold_status.propertyquarry_operator_action_summary(payload, now=NOW)
    _write_json(
        state_path,
        {"last_notified_digest": notify_gold_status._payload_digest(action)},
    ).chmod(0o664)
    deliveries: list[dict[str, object]] = []
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: deliveries.append(kwargs)
        or {"delivery_mode": "principal_binding", "message_ids": ["4401"]},
    )

    report = _build_report(payload=payload, receipt_path=receipt_path, state_path=state_path)

    assert report["sent"] is True
    assert len(deliveries) == 1
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_gold_notification_suppresses_stale_or_malformed_action(tmp_path: Path, monkeypatch) -> None:
    stale_payload = _actionable_payload()
    stale_payload["live_mobile_surfaces"]["operator_action"]["source_generated_at"] = "2026-08-26T03:29:59+00:00"
    malformed_payload = _actionable_payload()
    malformed_payload["live_mobile_surfaces"]["operator_action"]["consent_gate"]["automatic_execution_allowed"] = True
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("inadmissible action must stay silent")),
    )

    for index, payload in enumerate((stale_payload, malformed_payload)):
        receipt_path = _write_json(tmp_path / f"gold-{index}.json", payload)
        report = _build_report(
            payload=payload,
            receipt_path=receipt_path,
            state_path=tmp_path / f"state-{index}.json",
        )
        assert report["sent"] is False
        assert report["skipped_reason"] == "no_fresh_operator_action"


def test_gold_notification_dry_run_does_not_deliver_or_write_state(tmp_path: Path, monkeypatch) -> None:
    payload = _actionable_payload()
    receipt_path = _write_json(tmp_path / "gold.json", payload)
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        notify_gold_status,
        "deliver_notification_for_principal",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("dry run must not deliver")),
    )

    report = _build_report(
        payload=payload,
        receipt_path=receipt_path,
        state_path=state_path,
        dry_run=True,
    )

    assert report["sent"] is False
    assert report["would_send"] is True
    assert report["skipped_reason"] == "dry_run"
    assert "PropertyQuarry operator action required." in str(report["message_preview"])
    assert RAW_RUNTIME_BLOCKER not in json.dumps(report)
    assert not state_path.exists()
