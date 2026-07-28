from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _signed_headers(secret: str, body: bytes) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Subscribr-Signature": f"sha256={signature}",
        "X-Subscribr-Timestamp": str(time.time()),
    }


def test_concurrent_signed_webhooks_claim_once_and_conflicting_replay_fails_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "content-webhook-test-secret"
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(tmp_path / "content-jobs.json"))
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_WEBHOOK_LEASE_SECONDS", "invalid-uses-safe-default")
    monkeypatch.setenv("SUBSCRIBR_PROPERTY_WEBHOOK_SECRET", secret)
    from app.api.app import create_app
    from app.services.property_content_job_ledger import PropertyContentJobLedger

    client = TestClient(create_app())
    PropertyContentJobLedger(path=tmp_path / "content-jobs.json").upsert_job(
        {
            "packet_id": "packet-1",
            "content_mode": "product_tutorial",
            "title": "Trusted webhook source",
        },
        principal_id="propertyquarry:system:content-studio",
        ownership_scope="system",
        search_run_id="",
        status="PROVIDER_GENERATING",
    )
    payload = {"id": "evt-concurrent", "type": "script.started", "packet_id": "packet-1"}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    barrier = threading.Barrier(2)

    def deliver() -> tuple[int, dict[str, object]]:
        barrier.wait(timeout=5)
        response = client.post(
            "/internal/providers/subscribr/webhook",
            content=body,
            headers=_signed_headers(secret, body),
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=15) for future in (pool.submit(deliver), pool.submit(deliver))]

    assert sorted(status for status, _payload in results) == [200, 200]
    assert sorted(str(result["status"]) for _status, result in results) == ["duplicate_ignored", "received"]

    conflict_payload = {**payload, "type": "script.tampered"}
    conflict_body = json.dumps(conflict_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    conflict = client.post(
        "/internal/providers/subscribr/webhook",
        content=conflict_body,
        headers=_signed_headers(secret, conflict_body),
    )
    assert conflict.status_code == 409
    assert "subscribr_webhook_event_payload_conflict" in json.dumps(conflict.json())

    ledger_path = tmp_path / "content-jobs.json"
    ledger_path.write_text('{"jobs":', encoding="utf-8")
    corrupt_payload = {"id": "evt-corrupt", "type": "script.started", "packet_id": "packet-1"}
    corrupt_body = json.dumps(corrupt_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    corrupt = client.post(
        "/internal/providers/subscribr/webhook",
        content=corrupt_body,
        headers=_signed_headers(secret, corrupt_body),
    )
    assert corrupt.status_code == 503
    assert "property_content_ledger_corrupt" in json.dumps(corrupt.json())

    metrics = client.app.state.runtime_metrics.render_prometheus(
        readiness_ready=True,
        environ={},
        now_epoch=time.time(),
    )
    assert 'propertyquarry_content_ledger_events_total{outcome="claimed"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="duplicate"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="completed"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="replay_conflict"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="corruption"} 1' in metrics
