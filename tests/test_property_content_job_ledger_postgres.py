from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
from uuid import uuid4

import pytest

from app.product.property_search_schema import migrate_property_search_schema
from app.services.property_content_job_ledger import PropertyContentJobLedger


_SYSTEM_OWNERSHIP = {
    "principal_id": "propertyquarry:system:content-studio",
    "ownership_scope": "system",
    "search_run_id": "",
}


def _database_url() -> str:
    value = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not value:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    return value


def test_postgres_content_ledger_claims_once_orders_events_and_recovers_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    namespace = f"property_content_ledger_{uuid4().hex}"
    isolated_url = make_conninfo(database_url, options=f"-csearch_path={namespace},public")
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    try:
        monkeypatch.setenv("DATABASE_URL", isolated_url)
        monkeypatch.setenv(
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
            "property-content-postgres-test-erasure-secret-v1",
        )
        migrated = migrate_property_search_schema(isolated_url, applied_by="postgres-content-ledger-test")
        assert migrated.applied_versions == tuple(range(1, 14))
        ledger_a = PropertyContentJobLedger(database_url=isolated_url, backend="postgres")
        ledger_b = PropertyContentJobLedger(database_url=isolated_url, backend="postgres")
        packet = {
            "packet_id": "packet-postgres",
            "content_mode": "product_tutorial",
            "subscribr_channel_key": "propertyquarry-product-tutorials",
        }
        ledger_a.upsert_job(packet, **_SYSTEM_OWNERSHIP, status="QUEUED")
        observed = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)
        barrier = threading.Barrier(2)

        def claim_job(ledger: PropertyContentJobLedger, owner: str):  # type: ignore[no-untyped-def]
            barrier.wait(timeout=5)
            return ledger.claim_job(
                "packet-postgres",
                **_SYSTEM_OWNERSHIP,
                lease_owner=owner,
                lease_seconds=60,
                now=observed,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            job_claims = [
                future.result(timeout=15)
                for future in (
                    pool.submit(claim_job, ledger_a, "job-a"),
                    pool.submit(claim_job, ledger_b, "job-b"),
                )
            ]
        assert sum(claim is not None for claim in job_claims) == 1

        payload = {"id": "evt-postgres", "type": "script.started", "packet_id": "packet-postgres"}
        barrier = threading.Barrier(2)

        def claim_webhook(ledger: PropertyContentJobLedger, owner: str) -> dict[str, object]:
            barrier.wait(timeout=5)
            return ledger.claim_webhook_event(
                event_id="evt-postgres",
                payload=payload,
                packet_id="packet-postgres",
                **_SYSTEM_OWNERSHIP,
                extra={"signature_status": "verified"},
                claim_owner=owner,
                lease_seconds=60,
                now=observed,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            webhook_claims = [
                future.result(timeout=15)
                for future in (
                    pool.submit(claim_webhook, ledger_a, "webhook-a"),
                    pool.submit(claim_webhook, ledger_b, "webhook-b"),
                )
            ]
        assert sum(bool(claim["claimed"]) for claim in webhook_claims) == 1
        recovered = ledger_b.claim_webhook_event(
            event_id="evt-postgres",
            payload=payload,
            packet_id="packet-postgres",
            **_SYSTEM_OWNERSHIP,
            extra={},
            claim_owner="webhook-recovery",
            lease_seconds=60,
            now=observed + timedelta(seconds=61),
        )
        assert recovered["claimed"] is True
        assert recovered["recovered"] is True
        ledger_b.complete_webhook_event(
            event_id="evt-postgres",
            **_SYSTEM_OWNERSHIP,
            claim_owner="webhook-recovery",
            status="received",
        )

        snapshot = ledger_a._load()
        event_sequences = [int(event["event_sequence"]) for event in snapshot["job_events"]]
        assert event_sequences == sorted(event_sequences)
        assert len(event_sequences) == len(set(event_sequences))
        assert "webhook_claim_recovered" in {
            str(event["event_type"]) for event in snapshot["job_events"]
        }
    finally:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
            with admin.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(namespace)))
