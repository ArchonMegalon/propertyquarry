from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "propertyquarry_onemin_live_ops.py"
SPEC = importlib.util.spec_from_file_location("propertyquarry_onemin_live_ops", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
live_ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_ops)


@dataclass
class _Record:
    binding_id: str
    principal_id: str
    provider_key: str = "onemin"
    status: str = "enabled"
    priority: int = 10
    probe_state: str = "degraded"
    updated_at: str = "2026-08-12T10:00:00Z"


class _Registry:
    def __init__(self) -> None:
        self.record: _Record | None = None
        self.upsert_payload: dict[str, object] = {}
        self.probe_payload: dict[str, object] = {}

    def list_persisted_binding_records(self, *, principal_id: str, limit: int):
        assert principal_id == "cf-email:test@example.com"
        assert limit == 100
        return () if self.record is None else (self.record,)

    def upsert_binding_record(self, **payload: object) -> _Record:
        self.upsert_payload = dict(payload)
        self.record = _Record(
            binding_id=str(payload["binding_id"]),
            principal_id=str(payload["principal_id"]),
            probe_state=str(payload["probe_state"]),
        )
        return self.record

    def route_tool_with_context(self, tool_name: str, *, principal_id: str):
        assert tool_name == live_ops.TOOL_NAME
        assert principal_id == "cf-email:test@example.com"
        return SimpleNamespace(
            provider_key="onemin",
            capability_key="code_generate",
            tool_name=tool_name,
        )

    def set_persisted_binding_probe(self, **payload: object) -> _Record:
        self.probe_payload = dict(payload)
        assert self.record is not None
        self.record.probe_state = str(payload["probe_state"])
        return self.record


def _health() -> dict[str, object]:
    return {
        "providers": {
            "onemin": {
                "backend": "1min",
                "state": "degraded",
                "configured_slots": 2,
                "slots": [
                    {
                        "slot": "fallback_35",
                        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_35",
                        "slot_role": "reserve",
                        "state": "ready",
                        "api_key": "must-never-escape",
                        "secret": "also-must-never-escape",
                    },
                    {
                        "slot": "primary",
                        "account_name": "ONEMIN_AI_API_KEY",
                        "state": "degraded",
                        "last_error": "provider response must not escape",
                    },
                ],
            }
        }
    }


def test_safe_health_snapshot_is_secret_free_and_truthful() -> None:
    snapshot = live_ops.safe_onemin_health_snapshot(
        _health(),
        observed_at="2026-08-12T10:00:00Z",
    )
    rendered = json.dumps(snapshot, sort_keys=True)

    assert snapshot["configured_slots"] == 2
    assert snapshot["ready_slots"] == 1
    assert snapshot["state"] == "degraded"
    assert snapshot["balance"]["state"] == "unknown"
    assert snapshot["ready_credentials"] == [
        {
            "slot": "fallback_35",
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_35",
            "slot_role": "reserve",
            "state": "ready",
        }
    ]
    assert "must-never-escape" not in rendered
    assert "also-must-never-escape" not in rendered
    assert "provider response must not escape" not in rendered


def test_live_ops_persists_principal_scope_and_secret_free_call_receipt(
    monkeypatch,
) -> None:
    registry = _Registry()
    container = SimpleNamespace(
        provider_registry=registry,
        tool_execution=object(),
    )
    monkeypatch.setattr(
        live_ops,
        "exercise_onemin_route",
        lambda **_: {
            "status": "succeeded",
            "observed_at": "2026-08-12T10:00:01Z",
            "provider": "onemin",
            "backend": "1min",
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_35",
            "slot": "fallback_35",
            "model": "gpt-5.4",
            "tokens_in": 5,
            "tokens_out": 3,
            "latency_ms": 1495,
            "response_body_persisted": False,
            "balance_state": "unknown",
        },
    )

    payload = live_ops.run_live_ops(
        container=container,
        health=_health(),
        principal_id="cf-email:test@example.com",
        exercise=True,
        model="gpt-5.4",
    )
    rendered = json.dumps(payload, sort_keys=True)

    assert registry.upsert_payload["provider_key"] == "onemin"
    assert registry.upsert_payload["scope_json"] == {
        "product": "propertyquarry",
        "actions": ["property.evaluate"],
        "allowed_capabilities": ["code_generate"],
        "allowed_tools": ["provider.onemin.code_generate"],
    }
    assert registry.upsert_payload["auth_metadata_json"]["secret_persisted"] is False
    assert registry.probe_payload["probe_details_json"]["live_call"]["status"] == "succeeded"
    assert payload["route"]["verified"] is True
    assert payload["live_call"]["slot"] == "fallback_35"
    assert payload["secret_material_persisted"] is False
    assert len(payload["receipt_sha256"]) == 64
    assert "must-never-escape" not in rendered
    assert "also-must-never-escape" not in rendered
