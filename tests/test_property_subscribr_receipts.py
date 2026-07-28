from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.services.property_content_job_ledger import PropertyContentJobLedger
from app.services.property_content_packet_builder import build_synthetic_dossier_source_packet
from app.services.property_content_studio import PropertyContentStudio


_SYSTEM_OWNERSHIP = {
    "principal_id": "propertyquarry:system:content-studio",
    "ownership_scope": "system",
    "search_run_id": "",
}


def _markdown() -> str:
    return (
        "# Why this listing matched\n\n"
        "Generated from the reviewed dossier and source packet. "
        "Heating system and reserve fund remain unknown and should be verified before the viewing.\n"
    )


def test_script_receipt_is_review_required_and_never_publishable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR", str(tmp_path / "completion"))
    packet = build_synthetic_dossier_source_packet()
    receipt = PropertyContentStudio(ledger=PropertyContentJobLedger(path=tmp_path / "ledger.json")).materialize_script_receipt(
        packet=packet,
        markdown=_markdown(),
        **_SYSTEM_OWNERSHIP,
        provider_channel_id="212",
        provider_idea_id="idea-1",
        provider_script_id="script-1",
    )

    receipt_path = Path(receipt["receipt_path"])
    assert receipt["contract_name"] == "propertyquarry.subscribr_script_draft.v1"
    assert receipt["status"] == "review_required"
    assert receipt["publication_allowed"] is False
    assert receipt["production_allowed"] is False
    assert receipt["human_review"]["status"] == "pending"
    assert receipt["validation"]["privacy"] == "pass"
    assert receipt_path.exists()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["script_sha256"] == receipt["script_sha256"]


def test_subscribr_webhook_requires_signature_and_rejects_replay(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR", str(tmp_path / "completion"))
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(ledger_path))
    monkeypatch.setenv("SUBSCRIBR_PROPERTY_WEBHOOK_SECRET", "webhook-secret")
    packet = build_synthetic_dossier_source_packet()
    ledger = PropertyContentJobLedger(path=ledger_path)
    PropertyContentStudio(ledger=ledger).prepare_source_packet(
        packet,
        **_SYSTEM_OWNERSHIP,
    )
    ledger.record_provider_ids(
        packet_id=str(packet["packet_id"]),
        **_SYSTEM_OWNERSHIP,
        provider_channel_id="212",
        provider_idea_id="idea-1",
        provider_script_id="script-1",
    )
    payload = {
        "event_id": "evt-1",
        "event_type": "script.generated",
        "packet_id": packet["packet_id"],
        "markdown": _markdown(),
        "channel_id": "212",
        "idea_id": "idea-1",
        "script_id": "script-1",
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(b"webhook-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(create_app(), base_url="https://propertyquarry.com")

    rejected = client.post("/internal/providers/subscribr/webhook", content=raw)
    accepted = client.post("/internal/providers/subscribr/webhook", content=raw, headers={"x-subscribr-signature": signature})
    replay = client.post("/internal/providers/subscribr/webhook", content=raw, headers={"x-subscribr-signature": signature})

    assert rejected.status_code == 401
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "review_required"
    assert accepted.json()["receipt"]["publication_allowed"] is False
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate_ignored"
    event_row = next(
        row
        for row in PropertyContentJobLedger(path=ledger_path)._load()["webhook_events"].values()
        if row["event_id"] == "evt-1"
    )
    assert event_row["signature_status"] == "verified"
    assert event_row["raw_body_sha256"] == hashlib.sha256(raw).hexdigest()


def test_subscribr_webhook_uses_only_local_source_packets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR", str(tmp_path / "completion"))
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("SUBSCRIBR_PROPERTY_WEBHOOK_SECRET", "webhook-secret")
    payload = {
        "event_id": "evt-inline-packet",
        "event_type": "script.generated",
        "packet": build_synthetic_dossier_source_packet(),
        "markdown": _markdown(),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(b"webhook-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(create_app(), base_url="https://propertyquarry.com")

    response = client.post("/internal/providers/subscribr/webhook", content=raw, headers={"x-subscribr-signature": signature})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "subscribr_webhook_job_missing"
    assert not list((tmp_path / "completion").glob("*.generated.json"))


def test_subscribr_webhook_does_not_treat_non_completion_event_as_script(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR", str(tmp_path / "completion"))
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(ledger_path))
    monkeypatch.setenv("SUBSCRIBR_PROPERTY_WEBHOOK_SECRET", "webhook-secret")
    packet = build_synthetic_dossier_source_packet()
    PropertyContentStudio(ledger=PropertyContentJobLedger(path=ledger_path)).prepare_source_packet(
        packet,
        **_SYSTEM_OWNERSHIP,
    )
    payload = {
        "event_id": "evt-idea-created",
        "event_type": "idea.created",
        "packet_id": packet["packet_id"],
        "markdown": _markdown(),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(b"webhook-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(create_app(), base_url="https://propertyquarry.com")

    response = client.post("/internal/providers/subscribr/webhook", content=raw, headers={"x-subscribr-signature": signature})

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert response.json()["next"] == "wait_for_script_generated_or_export_completed"
    assert not list((tmp_path / "completion").glob("*.generated.json"))
