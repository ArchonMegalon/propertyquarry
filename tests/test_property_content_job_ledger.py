from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from app.services.property_content_job_ledger import (
    PropertyContentJobClaimLostError,
    PropertyContentJobLedger,
    PropertyContentLedgerCorruptionError,
    PropertyContentLedgerError,
)
from app.services.property_content_packet_builder import build_product_tutorial_source_packet
from app.services.property_content_studio import PropertyContentStudio


_SYSTEM_OWNERSHIP = {
    "principal_id": "propertyquarry:system:content-studio",
    "ownership_scope": "system",
    "search_run_id": "",
}


def _packet(packet_id: str) -> dict[str, object]:
    return {
        "packet_id": packet_id,
        "content_mode": "product_tutorial",
        "subscribr_channel_key": "propertyquarry-product-tutorials",
        "title": f"Tutorial {packet_id}",
    }


def test_file_ledger_preserves_corrupt_source_instead_of_resetting(tmp_path) -> None:
    path = tmp_path / "content-jobs.json"
    path.write_bytes(b'{"jobs": {"partial"')
    original = path.read_bytes()
    ledger = PropertyContentJobLedger(path=path)

    with pytest.raises(PropertyContentLedgerCorruptionError, match="property_content_ledger_corrupt"):
        ledger.get_job("partial", **_SYSTEM_OWNERSHIP)
    with pytest.raises(PropertyContentLedgerCorruptionError, match="property_content_ledger_corrupt"):
        ledger.upsert_job(_packet("new"), **_SYSTEM_OWNERSHIP, status="QUEUED")

    assert path.read_bytes() == original


def test_file_ledger_serializes_parallel_writers_and_orders_events(tmp_path) -> None:
    path = tmp_path / "content-jobs.json"
    ledger_a = PropertyContentJobLedger(path=path)
    ledger_b = PropertyContentJobLedger(path=path)
    barrier = threading.Barrier(2)

    def write_batch(prefix: str, ledger: PropertyContentJobLedger) -> None:
        barrier.wait(timeout=5)
        for index in range(20):
            packet_id = f"{prefix}-{index}"
            ledger.upsert_job(_packet(packet_id), **_SYSTEM_OWNERSHIP, status="QUEUED")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_batch, "a", ledger_a)
        second = pool.submit(write_batch, "b", ledger_b)
        first.result(timeout=15)
        second.result(timeout=15)

    snapshot = ledger_a._load()
    assert len(dict(snapshot["jobs"])) == 40
    events = list(snapshot["job_events"])
    sequences = [int(event["event_sequence"]) for event in events]
    assert sequences == list(range(1, 41))
    assert len({str(event["idempotency_key"]) for event in events}) == 40
    json.dumps(snapshot)


def test_webhook_claim_is_atomic_and_replay_conflicts_are_fail_closed(tmp_path) -> None:
    path = tmp_path / "content-jobs.json"
    ledger_a = PropertyContentJobLedger(path=path)
    ledger_b = PropertyContentJobLedger(path=path)
    barrier = threading.Barrier(2)
    payload = {"id": "evt-race", "type": "script.started", "packet_id": "packet-1"}
    observed = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    ledger_a.upsert_job(_packet("packet-1"), **_SYSTEM_OWNERSHIP, status="QUEUED")

    def claim(ledger: PropertyContentJobLedger, owner: str) -> dict[str, object]:
        barrier.wait(timeout=5)
        return ledger.claim_webhook_event(
            event_id="evt-race",
            payload=payload,
            packet_id="packet-1",
            **_SYSTEM_OWNERSHIP,
            extra={"signature_status": "verified"},
            claim_owner=owner,
            lease_seconds=60,
            now=observed,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10)
            for future in (
                pool.submit(claim, ledger_a, "worker-a"),
                pool.submit(claim, ledger_b, "worker-b"),
            )
        ]

    assert sorted(bool(result["claimed"]) for result in results) == [False, True]
    winner = next(result for result in results if result["claimed"])
    winner_owner = str(dict(winner["row"])["lease_owner"])
    ledger_a.complete_webhook_event(
        event_id="evt-race",
        **_SYSTEM_OWNERSHIP,
        claim_owner=winner_owner,
        status="received",
    )

    duplicate = ledger_b.claim_webhook_event(
        event_id="evt-race",
        payload=payload,
        packet_id="packet-1",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="worker-c",
        lease_seconds=60,
        now=observed + timedelta(seconds=70),
    )
    conflict = ledger_b.claim_webhook_event(
        event_id="evt-race",
        payload={**payload, "packet_id": "tampered"},
        packet_id="packet-1",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="worker-d",
        lease_seconds=60,
        now=observed + timedelta(seconds=80),
    )

    assert duplicate["duplicate"] is True
    assert duplicate["conflict"] is False
    assert conflict["claimed"] is False
    assert conflict["conflict"] is True
    persisted = dict(conflict["row"])
    assert dict(persisted["payload_json"])["packet_id"] == "packet-1"
    assert persisted["last_error"] == "provider_event_payload_mismatch"


def test_expired_job_and_webhook_claims_recover_without_stale_owner_updates(tmp_path) -> None:
    ledger = PropertyContentJobLedger(path=tmp_path / "content-jobs.json")
    packet = _packet("packet-recovery")
    ledger.upsert_job(packet, **_SYSTEM_OWNERSHIP, status="QUEUED")
    observed = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)

    first_job = ledger.claim_job(
        "packet-recovery",
        **_SYSTEM_OWNERSHIP,
        lease_owner="worker-a",
        lease_seconds=30,
        now=observed,
    )
    assert first_job is not None
    assert ledger.claim_job(
        "packet-recovery",
        **_SYSTEM_OWNERSHIP,
        lease_owner="worker-b",
        lease_seconds=30,
        now=observed + timedelta(seconds=10),
    ) is None
    recovered_job = ledger.claim_job(
        "packet-recovery",
        **_SYSTEM_OWNERSHIP,
        lease_owner="worker-b",
        lease_seconds=30,
        now=observed + timedelta(seconds=31),
    )
    assert recovered_job is not None
    assert recovered_job["claim_recovered"] is True
    with pytest.raises(PropertyContentJobClaimLostError, match="property_content_job_claim_lost"):
        ledger.update_claimed_job(
            "packet-recovery",
            **_SYSTEM_OWNERSHIP,
            lease_owner="worker-a",
            status="STALE",
        )
    completed_job = ledger.update_claimed_job(
        "packet-recovery",
        **_SYSTEM_OWNERSHIP,
        lease_owner="worker-b",
        status="RECOVERED",
    )
    assert completed_job["status"] == "RECOVERED"
    assert completed_job["lease_owner"] == ""

    webhook_payload = {"id": "evt-recovery", "type": "script.generated", "packet_id": "packet-recovery"}
    first_webhook = ledger.claim_webhook_event(
        event_id="evt-recovery",
        payload=webhook_payload,
        packet_id="packet-recovery",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="webhook-a",
        lease_seconds=30,
        now=observed,
    )
    assert first_webhook["claimed"] is True
    recovered_webhook = ledger.claim_webhook_event(
        event_id="evt-recovery",
        payload=webhook_payload,
        packet_id="packet-recovery",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="webhook-b",
        lease_seconds=30,
        now=observed + timedelta(seconds=31),
    )
    assert recovered_webhook["claimed"] is True
    assert recovered_webhook["recovered"] is True
    with pytest.raises(PropertyContentJobClaimLostError, match="subscribr_webhook_claim_lost"):
        ledger.complete_webhook_event(
            event_id="evt-recovery",
            **_SYSTEM_OWNERSHIP,
            claim_owner="webhook-a",
            status="review_required",
        )
    ledger.complete_webhook_event(
        event_id="evt-recovery",
        **_SYSTEM_OWNERSHIP,
        claim_owner="webhook-b",
        status="review_required",
    )
    duplicate = ledger.claim_webhook_event(
        event_id="evt-recovery",
        payload=webhook_payload,
        packet_id="packet-recovery",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="webhook-c",
        lease_seconds=30,
        now=observed + timedelta(seconds=90),
    )
    assert duplicate["duplicate"] is True
    event_types = [str(event["event_type"]) for event in ledger._load()["job_events"]]
    assert "webhook_claim_recovered" in event_types


def test_conflicting_replay_revokes_an_active_webhook_claim(tmp_path) -> None:
    ledger = PropertyContentJobLedger(path=tmp_path / "content-jobs.json")
    payload = {"id": "evt-active-conflict", "type": "script.started", "packet_id": "packet-1"}
    observed = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
    ledger.upsert_job(_packet("packet-1"), **_SYSTEM_OWNERSHIP, status="QUEUED")
    claimed = ledger.claim_webhook_event(
        event_id="evt-active-conflict",
        payload=payload,
        packet_id="packet-1",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="worker-a",
        lease_seconds=60,
        now=observed,
    )
    assert claimed["claimed"] is True

    conflict = ledger.claim_webhook_event(
        event_id="evt-active-conflict",
        payload={**payload, "packet_id": "tampered"},
        packet_id="packet-1",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="worker-b",
        lease_seconds=60,
        now=observed + timedelta(seconds=1),
    )

    assert conflict["conflict"] is True
    assert dict(conflict["row"])["lease_owner"] == ""
    with pytest.raises(PropertyContentJobClaimLostError, match="subscribr_webhook_claim_lost"):
        ledger.complete_webhook_event(
            event_id="evt-active-conflict",
            **_SYSTEM_OWNERSHIP,
            claim_owner="worker-a",
            status="received",
        )
    original_replay = ledger.claim_webhook_event(
        event_id="evt-active-conflict",
        payload=payload,
        packet_id="packet-1",
        **_SYSTEM_OWNERSHIP,
        extra={},
        claim_owner="worker-c",
        lease_seconds=60,
        now=observed + timedelta(seconds=70),
    )
    assert original_replay["duplicate"] is True
    assert original_replay["claimed"] is False
    assert "webhook_replay_conflict" in {
        str(event["event_type"]) for event in ledger._load()["job_events"]
    }


class _NeverCalledSubscribrClient:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def create_idea(self, **_kwargs) -> dict[str, object]:
        self.calls += 1
        raise AssertionError("provider dispatch must not be repeated after ambiguous crash")


class _PartiallyFailingSubscribrClient:
    configured = True

    def __init__(self) -> None:
        self.idea_calls = 0
        self.script_calls = 0

    def create_idea(self, **_kwargs) -> dict[str, object]:
        self.idea_calls += 1
        return {"id": "idea-durable-1"}

    def create_script(self, **_kwargs) -> dict[str, object]:
        self.script_calls += 1
        raise RuntimeError("synthetic provider uncertainty")


class _CountingSubscribrClient:
    configured = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.idea_calls = 0
        self.script_calls = 0
        self.generate_calls = 0

    def create_idea(self, **_kwargs) -> dict[str, object]:
        with self._lock:
            self.idea_calls += 1
        return {"id": "idea-once"}

    def create_script(self, **_kwargs) -> dict[str, object]:
        with self._lock:
            self.script_calls += 1
        return {"id": "script-once"}

    def generate_script(self, **_kwargs) -> dict[str, object]:
        with self._lock:
            self.generate_calls += 1
        return {"status": "generating"}


def test_provider_dispatch_crash_recovers_to_reconciliation_without_resend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_API_ENABLED", "1")
    ledger = PropertyContentJobLedger(path=tmp_path / "content-jobs.json")
    packet = build_product_tutorial_source_packet(
        title="How PropertyQuarry evidence works",
        language="en",
        jurisdiction="GLOBAL",
    )
    packet_id = str(packet["packet_id"])
    ledger.upsert_job(packet, **_SYSTEM_OWNERSHIP, status="PROVIDER_REQUEST_QUEUED")
    old_time = datetime.now(timezone.utc) - timedelta(hours=2)
    ledger.claim_job(
        packet_id,
        **_SYSTEM_OWNERSHIP,
        lease_owner="crashed-worker",
        lease_seconds=30,
        now=old_time,
    )
    ledger.update_claimed_job(
        packet_id,
        **_SYSTEM_OWNERSHIP,
        lease_owner="crashed-worker",
        status="PROVIDER_DISPATCHING",
        extra={"provider_dispatch_started_at": old_time.isoformat()},
        release=False,
    )
    client = _NeverCalledSubscribrClient()

    result = PropertyContentStudio(ledger=ledger, client=client).request_subscribr_script(
        packet,
        **_SYSTEM_OWNERSHIP,
    )

    assert result["status"] == "PROVIDER_RECONCILIATION_REQUIRED"
    assert result["claim_status"] == "recovered_without_resend"
    assert result["provider_status"] == "outcome_unknown"
    assert client.calls == 0


def test_provider_progress_is_persisted_before_later_provider_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_API_ENABLED", "1")
    ledger = PropertyContentJobLedger(path=tmp_path / "content-jobs.json")
    packet = build_product_tutorial_source_packet(
        title="How PropertyQuarry provenance works",
        language="en",
        jurisdiction="GLOBAL",
    )
    client = _PartiallyFailingSubscribrClient()

    result = PropertyContentStudio(
        ledger=ledger,
        client=client,
    ).request_subscribr_script(packet, **_SYSTEM_OWNERSHIP)
    retried = PropertyContentStudio(ledger=ledger, client=client).request_subscribr_script(
        packet,
        **_SYSTEM_OWNERSHIP,
    )

    assert result["status"] == "PROVIDER_RECONCILIATION_REQUIRED"
    assert result["provider_status"] == "outcome_unknown"
    assert result["provider_idea_id"] == "idea-durable-1"
    assert result["provider_script_id"] == ""
    assert result["lease_owner"] == ""
    assert retried["claim_status"] == "manual_reconciliation_required"
    assert client.idea_calls == 1
    assert client.script_calls == 1


def test_parallel_provider_requests_dispatch_only_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_API_ENABLED", "1")
    path = tmp_path / "content-jobs.json"
    client = _CountingSubscribrClient()
    studios = (
        PropertyContentStudio(ledger=PropertyContentJobLedger(path=path), client=client),
        PropertyContentStudio(ledger=PropertyContentJobLedger(path=path), client=client),
    )
    packet = build_product_tutorial_source_packet(
        title="How PropertyQuarry diligence works",
        language="en",
        jurisdiction="GLOBAL",
    )
    barrier = threading.Barrier(2)

    def request(studio: PropertyContentStudio) -> dict[str, object]:
        barrier.wait(timeout=5)
        return studio.request_subscribr_script(packet, **_SYSTEM_OWNERSHIP)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=15)
            for future in (pool.submit(request, studios[0]), pool.submit(request, studios[1]))
        ]

    assert client.idea_calls == 1
    assert client.script_calls == 1
    assert client.generate_calls == 1
    final_job = studios[0].ledger.get_job(str(packet["packet_id"]), **_SYSTEM_OWNERSHIP)
    assert final_job is not None
    assert final_job["provider_script_id"] == "script-once"
    assert any(bool(result.get("idempotent")) for result in results)


def test_prod_rejects_file_compatibility_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(PropertyContentLedgerError, match="property_content_postgres_required_in_prod"):
        PropertyContentJobLedger()
