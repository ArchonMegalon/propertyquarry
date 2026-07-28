from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.product import property_search_schema as schema


def _database_url() -> str:
    value = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not value:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    return value


def test_postgres_legacy_run_upgrade_queue_install_and_idempotency() -> None:
    database_url = _database_url()
    import psycopg
    from psycopg import sql

    namespace = f"property_search_migration_{uuid4().hex}"

    def isolated_connect(_database_url: str, *, autocommit: bool):
        conn = psycopg.connect(database_url, autocommit=autocommit, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(namespace))
            )
        return conn

    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    try:
        with isolated_connect(database_url, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE property_search_runs (
                        run_id TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL,
                        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO property_search_runs
                        (run_id, principal_id, payload_json, created_at, updated_at)
                    VALUES (
                        'legacy-run',
                        'legacy-principal',
                        '{"status":"queued","summary":{"status":"queued"}}'::jsonb,
                        NOW(),
                        NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE delivery_outbox (
                        delivery_id TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL,
                        recipient TEXT NOT NULL,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL,
                        metadata_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        sent_at TIMESTAMPTZ NULL,
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        attempt_count INT NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ NULL,
                        last_error TEXT NOT NULL DEFAULT '',
                        receipt_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        dead_lettered_at TIMESTAMPTZ NULL
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO delivery_outbox
                        (delivery_id, principal_id, channel, recipient, content,
                         status, metadata_json, created_at, idempotency_key)
                    VALUES (
                        'legacy-delivery', 'legacy-principal', 'email',
                        'legacy@example.com', 'legacy memo', 'queued',
                        '{"principal_id":"legacy-principal"}'::jsonb,
                        NOW(), 'legacy-idempotency-key'
                    )
                    """
                )

        first = schema.migrate_property_search_schema(
            database_url,
            applied_by="postgres-contract",
            connect=isolated_connect,
        )
        second = schema.migrate_property_search_schema(
            database_url,
            applied_by="postgres-contract",
            connect=isolated_connect,
        )
        status = schema.inspect_property_search_schema(
            database_url,
            connect=isolated_connect,
        )

        assert first.applied_versions == tuple(range(1, 14))
        assert second.applied_versions == ()
        assert status.ready is True

        with isolated_connect(database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    ALTER TABLE property_search_work_jobs
                    ENABLE REPLICA TRIGGER property_search_work_jobs_erasure_fence_guard
                    """
                )
        replica_only = schema.inspect_property_search_schema(
            database_url,
            connect=isolated_connect,
        )
        assert replica_only.ready is False
        assert replica_only.reason == (
            "required_trigger_missing:property_search_work_jobs_erasure_fence_guard"
        )
        with isolated_connect(database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    ALTER TABLE property_search_work_jobs
                    ENABLE ALWAYS TRIGGER property_search_work_jobs_erasure_fence_guard
                    """
                )
        assert schema.inspect_property_search_schema(
            database_url,
            connect=isolated_connect,
        ).ready is True

        with isolated_connect(database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, compact_json->>'status'
                    FROM property_search_runs
                    WHERE principal_id = 'legacy-principal' AND run_id = 'legacy-run'
                    """
                )
                assert cur.fetchone() == ("queued", "queued")
                cur.execute(
                    """
                    SELECT status, lease_owner, lease_expires_at,
                           claimed_at, dispatch_started_at
                    FROM delivery_outbox
                    WHERE delivery_id = 'legacy-delivery'
                    """
                )
                assert cur.fetchone() == ("queued", "", None, None, None)
                cur.execute(
                    """
                    SELECT to_regclass('property_content_jobs'),
                           to_regclass('property_content_job_events'),
                           to_regclass('property_content_webhook_events')
                    """
                )
                assert cur.fetchone() == (
                    "property_content_jobs",
                    "property_content_job_events",
                    "property_content_webhook_events",
                )
                cur.execute(
                    """
                    SELECT to_regclass('property_search_erasure_fences'),
                           EXISTS (
                               SELECT 1 FROM pg_trigger
                               WHERE tgrelid = 'property_search_runs'::regclass
                                 AND tgname = 'property_search_runs_writer_contract_guard'
                                 AND NOT tgisinternal
                           ),
                           EXISTS (
                               SELECT 1 FROM pg_trigger
                               WHERE tgrelid = 'property_search_work_jobs'::regclass
                                 AND tgname = 'property_search_work_jobs_erasure_fence_guard'
                                 AND NOT tgisinternal
                           )
                    """
                )
                assert cur.fetchone() == (
                    "property_search_erasure_fences",
                    True,
                    True,
                )
                from app.product.property_search_storage import (
                    _property_search_erasure_key_id,
                )

                cur.execute(
                    "SELECT key_id FROM property_search_erasure_key_state WHERE singleton = TRUE"
                )
                assert cur.fetchone() == (_property_search_erasure_key_id(),)
                with pytest.raises(psycopg.Error) as immutable_key_state:
                    cur.execute(
                        "UPDATE property_search_erasure_key_state SET key_id = %s WHERE singleton = TRUE",
                        ("0" * 64,),
                    )
                assert immutable_key_state.value.sqlstate == "23514"
                assert "property_search_erasure_key_state_immutable" in str(
                    immutable_key_state.value
                )
                cur.execute(
                    "SELECT key_id FROM property_search_erasure_key_state WHERE singleton = TRUE"
                )
                assert cur.fetchone() == (_property_search_erasure_key_id(),)
                cur.execute(
                    """
                    SELECT version, checksum_sha256
                    FROM propertyquarry_schema_migrations
                    WHERE component = 'property_search'
                    ORDER BY version
                    """
                )
                assert cur.fetchall() == [
                    (migration.version, migration.checksum)
                    for migration in schema.PROPERTY_SEARCH_MIGRATIONS
                ]
    finally:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
            with admin.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(namespace)
                    )
                )
