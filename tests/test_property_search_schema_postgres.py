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


def test_postgres_legacy_run_upgrade_queue_install_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    database_url = _database_url()
    import psycopg
    from psycopg import sql

    namespace = f"property_search_migration_{uuid4().hex}"
    fallback_namespace = f"property_search_fallback_{uuid4().hex}"
    capacity_owner_role = f"pq_schema_capacity_{uuid4().hex[:12]}"
    monkeypatch.setenv(
        "PROPERTYQUARRY_ADMISSION_CAPACITY_OWNER_ROLE",
        capacity_owner_role,
    )

    def isolated_connect(_database_url: str, *, autocommit: bool):
        conn = psycopg.connect(database_url, autocommit=autocommit, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET search_path TO {}, {}, public").format(
                    sql.Identifier(namespace),
                    sql.Identifier(fallback_namespace),
                )
            )
        return conn

    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(capacity_owner_role))
            )

            def drop_capacity_owner_role() -> None:
                with psycopg.connect(
                    database_url,
                    autocommit=True,
                    connect_timeout=5,
                ) as cleanup_admin:
                    with cleanup_admin.cursor() as cleanup_cursor:
                        cleanup_cursor.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(
                                sql.Identifier(capacity_owner_role)
                            )
                        )

            request.addfinalizer(drop_capacity_owner_role)
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
            cur.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(fallback_namespace)
                )
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.property_account_privacy_requests (
                        principal_key TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        idempotency_key_hash TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
                        payload_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (principal_key, request_id)
                    )
                    """
                ).format(sql.Identifier(fallback_namespace))
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE UNIQUE INDEX idx_property_privacy_request_idempotency
                    ON {}.property_account_privacy_requests(
                        principal_key, idempotency_key_hash
                    ) WHERE idempotency_key_hash <> ''
                    """
                ).format(sql.Identifier(fallback_namespace))
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE INDEX idx_property_privacy_request_status_updated
                    ON {}.property_account_privacy_requests(status, updated_at DESC)
                    """
                ).format(sql.Identifier(fallback_namespace))
            )
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

        assert first.applied_versions == tuple(
            range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
        )
        assert second.applied_versions == ()
        assert status.ready is True

        with psycopg.connect(
            database_url,
            autocommit=True,
            connect_timeout=5,
        ) as admin:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s), to_regclass(%s)",
                    (
                        f"{fallback_namespace}.idx_property_privacy_request_idempotency",
                        f"{fallback_namespace}.idx_property_privacy_request_status_updated",
                    ),
                )
                assert cur.fetchone() == (
                    f"{fallback_namespace}.idx_property_privacy_request_idempotency",
                    f"{fallback_namespace}.idx_property_privacy_request_status_updated",
                )

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
                           to_regclass('property_content_webhook_events'),
                           to_regclass('property_account_privacy_requests'),
                           to_regclass('idx_property_privacy_request_idempotency'),
                           to_regclass('idx_property_privacy_request_status_updated')
                    """
                )
                assert cur.fetchone() == (
                    "property_content_jobs",
                    "property_content_job_events",
                    "property_content_webhook_events",
                    "property_account_privacy_requests",
                    "idx_property_privacy_request_idempotency",
                    "idx_property_privacy_request_status_updated",
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
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(fallback_namespace)
                    )
                )
