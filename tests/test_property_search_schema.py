from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.product import property_search_schema as schema


def _relations_for(version: int) -> set[str]:
    if version == 1:
        return {
            "property_search_runs",
            "idx_property_search_runs_updated",
            "idx_property_search_runs_principal_updated",
            "idx_property_search_runs_status_updated",
            "idx_property_search_runs_principal_status_updated",
        }
    if version == 2:
        return {
            "property_search_work_jobs",
            "idx_property_search_work_idempotency",
            "idx_property_search_work_principal_run",
            "idx_property_search_work_claim",
        }
    if version == 3:
        return {
            "property_source_listing_cache",
            "idx_property_source_listing_cache_stored_at",
        }
    if version == 4:
        return {
            "delivery_outbox",
            "idx_delivery_outbox_status_created",
            "idx_delivery_outbox_principal_idempotency_unique",
            "idx_delivery_outbox_retry_schedule",
            "idx_delivery_outbox_principal_status_created",
            "idx_delivery_outbox_claim",
        }
    if version == 5:
        return {
            "property_content_jobs",
            "property_content_job_events",
            "property_content_webhook_events",
            "idx_property_content_jobs_status_updated",
            "idx_property_content_jobs_claim",
            "idx_property_content_job_events_packet_sequence",
            "idx_property_content_webhook_status_updated",
            "idx_property_content_webhook_claim",
        }
    if version == 6:
        return {
            "idx_property_search_runs_delivery_work_updated",
            "idx_property_search_runs_principal_delivery_work_updated",
        }
    if version == 7:
        return {"idx_delivery_outbox_principal_idempotency_unique"}
    if version == 8:
        return {
            "property_evidence_overlay_rollups",
            "idx_property_evidence_overlay_lookup",
            "idx_property_evidence_overlay_freshness",
            "property_evidence_overlay_snapshots",
            "idx_property_evidence_overlay_snapshots_ingested",
        }
    if version == 9:
        return {
            "property_evidence_overlay_active_snapshot",
            "idx_property_evidence_overlay_single_active",
            "idx_property_evidence_overlay_snapshot_lookup",
            "idx_property_evidence_overlay_snapshot_freshness",
        }
    if version == 10:
        return {
            "property_research_packet_links",
            "idx_property_research_packet_links_last_seen",
            "idx_property_research_packet_links_property_url",
            "idx_property_research_packet_links_retention",
            "property_research_packet_run_memberships",
            "idx_property_research_packet_memberships_ref",
            "idx_property_research_packet_memberships_observed",
            "property_research_packet_index_state",
        }
    if version == 11:
        return {
            "idx_property_search_runs_run_principal_key",
            "idx_property_research_packet_links_principal_key",
            "idx_property_research_packet_memberships_principal_key",
            "property_search_erasure_key_state",
            "property_search_erasure_fences",
        }
    if version == 12:
        return {
            "idx_property_content_jobs_principal_updated",
            "idx_property_content_job_events_principal_sequence",
            "idx_property_content_webhook_principal_updated",
        }
    if version == 13:
        return set()
    if version == 14:
        return set()
    if version == 15:
        return {
            "property_account_privacy_requests",
            "idx_property_privacy_request_idempotency",
            "idx_property_privacy_request_status_updated",
        }
    if version == 16:
        return {
            "propertyquarry_admission_quota_buckets",
            "idx_propertyquarry_admission_quota_expiry",
            "propertyquarry_admission_leases",
            "idx_propertyquarry_admission_lease_dimension_expiry",
            "idx_propertyquarry_admission_lease_expiry",
        }
    if version == 17:
        return {"propertyquarry_admission_capacity_state"}
    if version == 18:
        return set()
    if version == 19:
        return {
            "propertyquarry_retention_runs",
            "idx_propertyquarry_retention_runs_started",
            "idx_propertyquarry_retention_single_running",
        }
    raise AssertionError(f"unexpected migration version: {version}")


def _triggers_for(version: int) -> set[tuple[str, str]]:
    if version == 10:
        return {
            ("property_search_runs", "property_search_runs_writer_contract_guard")
        }
    if version == 11:
        return {
            (
                "property_search_work_jobs",
                "property_search_work_jobs_erasure_fence_guard",
            ),
            (
                "property_research_packet_links",
                "property_research_packet_links_erasure_fence_guard",
            ),
            (
                "property_research_packet_run_memberships",
                "property_research_packet_memberships_erasure_fence_guard",
            ),
            (
                "property_search_erasure_key_state",
                "property_search_erasure_key_state_immutable_guard",
            ),
        }
    if version == 12:
        return {
            (
                "property_content_jobs",
                "property_content_jobs_account_authority_guard",
            ),
            (
                "property_content_job_events",
                "property_content_job_events_account_authority_guard",
            ),
            (
                "property_content_webhook_events",
                "property_content_webhook_account_authority_guard",
            ),
        }
    if version == 17:
        return {
            (
                "propertyquarry_admission_quota_buckets",
                "propertyquarry_admission_quota_capacity_after_insert",
            ),
            (
                "propertyquarry_admission_quota_buckets",
                "propertyquarry_admission_quota_capacity_after_delete",
            ),
            (
                "propertyquarry_admission_quota_buckets",
                "propertyquarry_admission_quota_capacity_after_truncate",
            ),
            (
                "propertyquarry_admission_leases",
                "propertyquarry_admission_lease_capacity_after_insert",
            ),
            (
                "propertyquarry_admission_leases",
                "propertyquarry_admission_lease_capacity_after_delete",
            ),
            (
                "propertyquarry_admission_leases",
                "propertyquarry_admission_lease_capacity_after_truncate",
            ),
        }
    return set()


class _FakeDatabase:
    def __init__(self) -> None:
        self.ledger: dict[int, tuple[str, str]] = {}
        self.relations: set[str] = set()
        self.triggers: set[tuple[str, str]] = set()
        self.erasure_key_id = ""
        self.admission_write_authority = True
        self.admission_capacity = {"lease": 0, "quota": 0}
        self.admission_actual_counts = {"lease": 0, "quota": 0}
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_migration_version = 0

    def connect(self, _database_url: str, *, autocommit: bool):
        return _FakeConnection(self, autocommit=autocommit)

    def seed_migration(self, version: int, *, checksum: str = "") -> None:
        migration = schema.PROPERTY_SEARCH_MIGRATIONS[version - 1]
        self.ledger[version] = (migration.name, checksum or migration.checksum)
        self.relations.update(_relations_for(version))
        self.relations.add(schema.SCHEMA_LEDGER_TABLE)
        self.triggers.update(_triggers_for(version))


class _FakeConnection:
    def __init__(self, database: _FakeDatabase, *, autocommit: bool) -> None:
        self.database = database
        self.autocommit = autocommit
        self.closed = False
        self._snapshot = (
            deepcopy(database.ledger),
            set(database.relations),
            set(database.triggers),
            dict(database.admission_capacity),
            dict(database.admission_actual_counts),
        )

    def cursor(self):
        return _FakeCursor(self.database)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.close()
        return False

    def commit(self) -> None:
        self.database.commits += 1
        self._snapshot = (
            deepcopy(self.database.ledger),
            set(self.database.relations),
            set(self.database.triggers),
            dict(self.database.admission_capacity),
            dict(self.database.admission_actual_counts),
        )

    def rollback(self) -> None:
        self.database.rollbacks += 1
        self.database.ledger = deepcopy(self._snapshot[0])
        self.database.relations = set(self._snapshot[1])
        self.database.triggers = set(self._snapshot[2])
        self.database.admission_capacity = dict(self._snapshot[3])
        self.database.admission_actual_counts = dict(self._snapshot[4])

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
        normalized = " ".join(str(sql).split())
        arguments = tuple(params or ())
        self.database.executed.append((normalized, arguments))
        self._rows = []
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            self._rows = [(None,)]
            return
        if normalized.startswith(
            "CREATE TABLE IF NOT EXISTS propertyquarry_schema_migrations"
        ):
            self.database.relations.add(schema.SCHEMA_LEDGER_TABLE)
            return
        if normalized.startswith("SELECT version, migration_name, checksum_sha256"):
            self._rows = [
                (version, name, checksum)
                for version, (name, checksum) in sorted(self.database.ledger.items())
            ]
            return
        if normalized.startswith("INSERT INTO propertyquarry_schema_migrations"):
            version = int(arguments[1])
            self.database.ledger[version] = (str(arguments[2]), str(arguments[3]))
            return
        if normalized.startswith("SELECT to_regclass(%s), to_regclass(%s)"):
            self._rows = [
                tuple(
                    str(relation) if str(relation) in self.database.relations else None
                    for relation in arguments
                )
            ]
            return
        if normalized.startswith("SELECT to_regclass"):
            relation = str(arguments[0])
            self._rows = [(relation if relation in self.database.relations else None,)]
            return
        if normalized.startswith("SELECT has_table_privilege"):
            self._rows = [(self.database.admission_write_authority,)]
            return
        if normalized.startswith(
            "SELECT capacity_key, row_count, row_limit"
        ):
            include_actual_count = "AS actual_row_count" in normalized
            rows: list[tuple[object, ...]] = []
            for capacity_key, row_limit in (
                ("lease", schema.ADMISSION_LEASE_ROW_LIMIT),
                ("quota", schema.ADMISSION_QUOTA_ROW_LIMIT),
            ):
                row: tuple[object, ...] = (
                    capacity_key,
                    self.database.admission_capacity[capacity_key],
                    row_limit,
                )
                if include_actual_count:
                    row += (
                        self.database.admission_actual_counts[capacity_key],
                    )
                rows.append(row)
            self._rows = rows
            return
        for migration in schema.PROPERTY_SEARCH_MIGRATIONS:
            if str(sql) == migration.sql:
                if self.database.fail_migration_version == migration.version:
                    raise RuntimeError(
                        f"synthetic migration {migration.version} failure"
                    )
                self.database.relations.update(_relations_for(migration.version))
                self.database.triggers.update(_triggers_for(migration.version))
                if migration.version == 7:
                    self.database.relations.discard(
                        "idx_delivery_outbox_idempotency_key_unique"
                    )
                return
        if (
            "FROM propertyquarry_admission_quota_buckets" in normalized
            or "FROM propertyquarry_admission_leases" in normalized
        ):
            self._rows = []
            return
        if "FROM pg_trigger" in normalized:
            self._rows = [((str(arguments[0]), str(arguments[1])) in self.database.triggers,)]
            return
        if normalized.startswith(
            "SELECT key_id FROM property_search_erasure_key_state"
        ):
            self._rows = (
                [(self.database.erasure_key_id,)]
                if self.database.erasure_key_id
                else []
            )
            return

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


def test_clean_install_is_transactional_ordered_and_advisory_locked() -> None:
    database = _FakeDatabase()

    result = schema.migrate_property_search_schema(
        "postgresql://test/property",
        applied_by="release-test",
        connect=database.connect,
    )

    assert result.previous_version == 0
    assert result.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert result.applied_versions == tuple(
        range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert database.commits == 1
    assert database.rollbacks == 0
    assert tuple(database.ledger) == tuple(
        range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert "pg_advisory_xact_lock" in database.executed[0][0]
    assert database.executed[0][1] == (schema.SCHEMA_LOCK_ID,)
    for migration in schema.PROPERTY_SEARCH_MIGRATIONS:
        assert database.ledger[migration.version] == (
            migration.name,
            migration.checksum,
        )
    assert any(
        "propertyquarry.property_search_erasure_key_id" in sql
        for sql, _params in database.executed
    )
    assert any(
        sql == "SELECT property_search_assert_erasure_key()"
        for sql, _params in database.executed
    )


def test_production_v11_migration_requires_dedicated_erasure_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _FakeDatabase()
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.delenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", raising=False
    )
    monkeypatch.setenv("PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET", "not-authoritative")

    with pytest.raises(
        schema.PropertySearchSchemaError,
        match="property_search_erasure_secret_required",
    ):
        schema.migrate_property_search_schema(
            "postgresql://test/property",
            connect=database.connect,
        )

    assert database.commits == 0
    assert database.rollbacks == 1
    assert database.ledger == {}


def test_upgrade_from_run_schema_applies_queue_and_cache_once() -> None:
    database = _FakeDatabase()
    database.seed_migration(1)

    first = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    database.executed.clear()
    second = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert first.previous_version == 1
    assert first.applied_versions == tuple(
        range(2, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert second.previous_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert second.applied_versions == ()
    assert not any(
        sql == " ".join(migration.sql.split())
        for sql, _params in database.executed
        for migration in schema.PROPERTY_SEARCH_MIGRATIONS
    )


def test_upgrade_from_schema_v13_applies_erasure_privacy_and_admission() -> None:
    database = _FakeDatabase()
    for version in range(1, 14):
        database.seed_migration(version)

    result = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert result.previous_version == 13
    assert result.current_version == 19
    assert result.applied_versions == (14, 15, 16, 17, 18, 19)
    executed_migrations = {
        migration.version
        for sql, _params in database.executed
        for migration in schema.PROPERTY_SEARCH_MIGRATIONS
        if sql == " ".join(migration.sql.split())
    }
    assert executed_migrations == {14, 15, 16, 17, 18, 19}


def test_upgrade_from_schema_v14_applies_privacy_and_distributed_admission() -> None:
    database = _FakeDatabase()
    for version in range(1, 15):
        database.seed_migration(version)

    result = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert result.previous_version == 14
    assert result.current_version == 19
    assert result.applied_versions == (15, 16, 17, 18, 19)
    assert _relations_for(15).issubset(database.relations)
    assert _relations_for(16).issubset(database.relations)
    assert _relations_for(17).issubset(database.relations)
    assert _relations_for(18).issubset(database.relations)
    assert _relations_for(19).issubset(database.relations)


def test_upgrade_from_schema_v4_installs_content_ledger_and_delivery_projection() -> (
    None
):
    database = _FakeDatabase()
    for version in (1, 2, 3, 4):
        database.seed_migration(version)

    result = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert result.previous_version == 4
    assert result.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert result.applied_versions == tuple(
        range(5, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert _relations_for(5).issubset(database.relations)
    assert _relations_for(6).issubset(database.relations)
    assert _relations_for(8).issubset(database.relations)
    assert _relations_for(9).issubset(database.relations)
    assert _relations_for(10).issubset(database.relations)
    assert _relations_for(11).issubset(database.relations)
    assert _relations_for(12).issubset(database.relations)
    executed_migrations = {
        migration.version
        for sql, _params in database.executed
        for migration in schema.PROPERTY_SEARCH_MIGRATIONS
        if sql == " ".join(migration.sql.split())
    }
    assert executed_migrations == set(
        range(5, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )


def test_upgrade_from_schema_v6_removes_legacy_global_outbox_idempotency() -> None:
    database = _FakeDatabase()
    for version in (1, 2, 3, 4, 5, 6):
        database.seed_migration(version)
    database.relations.add("idx_delivery_outbox_idempotency_key_unique")

    result = schema.migrate_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert result.previous_version == 6
    assert result.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert result.applied_versions == tuple(
        range(7, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert "idx_delivery_outbox_idempotency_key_unique" not in database.relations
    assert "idx_delivery_outbox_principal_idempotency_unique" in database.relations
    migration_sql = " ".join(schema.PROPERTY_SEARCH_MIGRATIONS[6].sql.split())
    assert (
        "DROP INDEX IF EXISTS idx_delivery_outbox_idempotency_key_unique"
        in migration_sql
    )


def test_replayable_legacy_outbox_migration_preserves_tenant_scope() -> None:
    migration_sql = Path(
        "ea/schema/20260305_v0_8_channel_runtime_reliability.sql"
    ).read_text(encoding="utf-8")

    principal_guard = migration_sql.index("attname = 'principal_id'")
    scoped_index = migration_sql.index(
        "idx_delivery_outbox_principal_idempotency_unique"
    )
    obsolete_drop = migration_sql.index(
        "DROP INDEX IF EXISTS idx_delivery_outbox_idempotency_key_unique"
    )
    legacy_fallback = migration_sql.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_idempotency_key_unique"
    )
    assert principal_guard < scoped_index < obsolete_drop < legacy_fallback


def test_checksum_drift_and_version_gaps_fail_before_any_migration() -> None:
    drifted = _FakeDatabase()
    drifted.seed_migration(1, checksum="0" * 64)

    with pytest.raises(
        schema.PropertySearchSchemaDriftError,
        match="property_search_migration_checksum_drift:1",
    ):
        schema.migrate_property_search_schema(
            "postgresql://test/property",
            connect=drifted.connect,
        )
    assert drifted.rollbacks == 1
    assert tuple(drifted.ledger) == (1,)

    gapped = _FakeDatabase()
    gapped.seed_migration(2)
    with pytest.raises(
        schema.PropertySearchSchemaDriftError,
        match="property_search_migration_gap",
    ):
        schema.migrate_property_search_schema(
            "postgresql://test/property",
            connect=gapped.connect,
        )


def test_failed_upgrade_rolls_back_ledger_and_all_schema_changes() -> None:
    database = _FakeDatabase()
    database.fail_migration_version = 2

    with pytest.raises(RuntimeError, match="synthetic migration 2 failure"):
        schema.migrate_property_search_schema(
            "postgresql://test/property",
            connect=database.connect,
        )

    assert database.commits == 0
    assert database.rollbacks == 1
    assert database.ledger == {}
    assert database.relations == set()


def test_readiness_reports_missing_pending_drift_relation_and_ready() -> None:
    database = _FakeDatabase()
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.ready is False
    assert status.reason == "migration_ledger_missing"

    database.seed_migration(1)
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == "migration_pending"
    assert status.current_version == 1

    database.ledger[1] = (database.ledger[1][0], "f" * 64)
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == "property_search_migration_checksum_drift:1"

    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove("idx_property_search_work_claim")
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == "required_relation_missing:idx_property_search_work_claim"

    database.relations.add("idx_property_search_work_claim")
    database.relations.remove("idx_delivery_outbox_claim")
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == "required_relation_missing:idx_delivery_outbox_claim"

    database.relations.add("idx_delivery_outbox_claim")
    database.relations.remove("idx_property_content_webhook_claim")
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert (
        status.reason == "required_relation_missing:idx_property_content_webhook_claim"
    )

    database.relations.add("idx_property_content_webhook_claim")
    database.relations.add("idx_delivery_outbox_idempotency_key_unique")
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == (
        "forbidden_relation_present:idx_delivery_outbox_idempotency_key_unique"
    )

    database.relations.remove("idx_delivery_outbox_idempotency_key_unique")
    database.triggers.remove(
        ("property_search_runs", "property_search_runs_writer_contract_guard")
    )
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == "required_trigger_missing:property_search_runs_writer_contract_guard"

    database.triggers.add(
        ("property_search_runs", "property_search_runs_writer_contract_guard")
    )
    database.triggers.remove(
        ("property_search_work_jobs", "property_search_work_jobs_erasure_fence_guard")
    )
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == (
        "required_trigger_missing:property_search_work_jobs_erasure_fence_guard"
    )

    database.triggers.add(
        ("property_search_work_jobs", "property_search_work_jobs_erasure_fence_guard")
    )
    database.triggers.remove(
        ("property_content_jobs", "property_content_jobs_account_authority_guard")
    )
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.reason == (
        "required_trigger_missing:property_content_jobs_account_authority_guard"
    )

    database.triggers.add(
        ("property_content_jobs", "property_content_jobs_account_authority_guard")
    )
    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )
    assert status.ready is True
    assert status.reason == "schema_ready"
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION


def test_runtime_schema_access_fails_closed_without_running_ddl() -> None:
    database = _FakeDatabase()

    with pytest.raises(
        schema.PropertySearchSchemaNotReadyError,
        match="migration_ledger_missing",
    ):
        schema.require_property_search_schema_ready(
            "postgresql://test/property",
            connect=database.connect,
        )

    executed = "\n".join(sql for sql, _params in database.executed).upper()
    assert "CREATE TABLE" not in executed
    assert "ALTER TABLE" not in executed
    assert "CREATE INDEX" not in executed

    for path in (
        Path("ea/app/product/property_search_storage.py"),
        Path("ea/app/product/property_search_work_queue.py"),
        Path("ea/app/repositories/delivery_outbox_postgres.py"),
        Path("ea/app/repositories/property_evidence_overlays_postgres.py"),
        Path("ea/app/services/property_content_job_ledger.py"),
    ):
        runtime_source = path.read_text(encoding="utf-8").upper()
        assert "CREATE TABLE" not in runtime_source
        assert "ALTER TABLE" not in runtime_source
        assert "CREATE INDEX" not in runtime_source


def test_schema_v9_installs_staged_snapshot_pointer_without_runtime_ddl() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[8]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 9
    assert migration.name == "property_evidence_overlay_staged_snapshot_activation"
    assert "ADD COLUMN IF NOT EXISTS snapshot_id CHAR(64)" in migration_sql
    assert (
        "PRIMARY KEY (snapshot_id, layer_key, record_key, match_key, match_value)"
        in migration_sql
    )
    assert (
        "CREATE TABLE IF NOT EXISTS property_evidence_overlay_active_snapshot"
        in migration_sql
    )
    assert "CHECK (status IN ('staged', 'active', 'retired'))" in migration_sql
    assert "idx_property_evidence_overlay_snapshot_lookup" in migration_sql
    assert "idx_property_evidence_overlay_snapshot_freshness" in migration_sql


def test_schema_v10_installs_bounded_tenant_packet_links_and_writer_guard() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[9]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 10
    assert migration.name == "tenant_scoped_property_research_packet_links"
    assert "CREATE TABLE IF NOT EXISTS property_research_packet_links" in migration_sql
    assert "PRIMARY KEY (principal_id, candidate_ref)" in migration_sql
    assert "char_length(candidate_ref) BETWEEN 1 AND 256" in migration_sql
    assert "candidate_ref_algorithm IN ('explicit', 'derived_v1')" in migration_sql
    assert "jsonb_typeof(packet_json) = 'object'" in migration_sql
    assert "packet_json = packet_canonical_json::jsonb" in migration_sql
    assert "packet_size_bytes = octet_length(convert_to(packet_canonical_json, 'UTF8'))" in migration_sql
    assert "packet_size_bytes BETWEEN 2 AND 262144" in migration_sql
    assert "packet_schema_version >= 1" in migration_sql
    assert "packet_sha256 ~ '^[0-9a-f]{64}$'" in migration_sql
    assert "property_url_sha256 IS NULL" in migration_sql
    assert "last_seen_at >= first_seen_at" in migration_sql
    assert "idx_property_research_packet_links_last_seen" in migration_sql
    assert "idx_property_research_packet_links_property_url" in migration_sql
    assert "idx_property_research_packet_links_retention" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS property_research_packet_run_memberships" in migration_sql
    assert "PRIMARY KEY (principal_id, run_id, candidate_ref)" in migration_sql
    assert "REFERENCES property_search_runs(principal_id, run_id) ON DELETE CASCADE" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS property_research_packet_index_state" in migration_sql
    assert "property_search_runs_enforce_writer_contract" in migration_sql
    assert "property_search_writer_contract_required" in migration_sql
    assert "CREATE TRIGGER property_search_runs_writer_contract_guard" in migration_sql
    assert "IF TG_OP = 'DELETE' THEN RETURN OLD" in migration_sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON property_search_runs" in migration_sql

    assert "property_search_runs_compact_schema_version_match_check" in migration_sql
    assert "compact_schema_version = 0" in migration_sql
    assert "compact_json->'compact_schema_version'" in migration_sql
    assert (
        "compact_json->>'compact_schema_version' ~ '^[0-9]+$'" in migration_sql
    )
    assert ")::INTEGER = compact_schema_version" in migration_sql
    assert ") NOT VALID" in migration_sql


def test_schema_v11_installs_digest_only_erasure_fences_and_write_guards() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[10]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 11
    assert migration.name == "durable_property_search_erasure_fences"
    assert "CREATE TABLE IF NOT EXISTS property_search_erasure_key_state" in migration_sql
    assert "property_search_erasure_key_state_immutable_guard" in migration_sql
    assert "property_search_erasure_key_state_immutable" in migration_sql
    assert "CREATE OR REPLACE FUNCTION property_search_assert_erasure_key" in migration_sql
    assert "property_search_erasure_key_required" in migration_sql
    assert "property_search_erasure_key_mismatch" in migration_sql
    assert "PERFORM property_search_assert_erasure_key()" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS property_search_erasure_fences" in migration_sql
    assert "PRIMARY KEY (principal_key, run_id)" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS principal_key TEXT" in migration_sql
    assert "property_search_assert_write_allowed" in migration_sql
    assert "pg_advisory_xact_lock" in migration_sql
    assert "property_search_erasure:" in migration_sql
    assert "property_search_account_erased" in migration_sql
    assert "property_search_principal_key_required" in migration_sql
    assert "^hmac-sha256:[0-9a-f]{64}$" in migration_sql
    assert "hmac-sha256|sha256" not in migration_sql
    assert "current_setting('propertyquarry.property_search_writer_contract'" in migration_sql
    assert ") <> '3'" in migration_sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON property_search_work_jobs" in migration_sql
    assert "property_search_work_jobs_erasure_fence_guard" in migration_sql
    assert "writer_contract_version = 3" in migration_sql
    assert "principal_id" not in " ".join(
        migration_sql.split("CREATE TABLE IF NOT EXISTS property_search_erasure_fences", 1)[1]
        .split(");", 1)[0]
        .split()
    )
    assert "tgenabled IN ('O', 'A')" in Path(schema.__file__).read_text(
        encoding="utf-8"
    )


def test_schema_v12_installs_content_account_ownership_fences() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[11]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 12
    assert migration.name == "property_content_account_ownership_fence"
    assert "property_content_legacy_ownership_unresolved" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS principal_key TEXT" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS ownership_scope TEXT" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS search_run_id TEXT NOT NULL DEFAULT ''" in migration_sql
    assert "PRIMARY KEY (principal_key, ownership_scope, search_run_id, packet_id)" in migration_sql
    assert "property_content_jobs_owner_idempotency_key" in migration_sql
    assert "property_content_job_events_owner_packet_fkey" in migration_sql
    assert "property_content_webhook_events_owner_packet_fkey" in migration_sql
    assert "ownership_scope IN ('search_run', 'system')" in migration_sql
    assert "idx_property_content_jobs_principal_updated" in migration_sql
    assert "idx_property_content_job_events_principal_sequence" in migration_sql
    assert "idx_property_content_webhook_principal_updated" in migration_sql
    assert "propertyquarry.property_content_writer_contract" in migration_sql
    assert "property_content_writer_contract_required" in migration_sql
    assert "property_content_owner_run_immutable" in migration_sql
    assert "property_content_row_owner_mismatch" in migration_sql
    assert "property_content_system_owner_required" in migration_sql
    assert "PERFORM property_search_assert_run_owner" in migration_sql
    assert "PERFORM property_search_assert_write_allowed" in migration_sql
    for trigger_name in (
        "property_content_jobs_account_authority_guard",
        "property_content_job_events_account_authority_guard",
        "property_content_webhook_account_authority_guard",
    ):
        assert f"CREATE TRIGGER {trigger_name}" in migration_sql
    assert "BEFORE INSERT OR UPDATE ON property_content_jobs" in migration_sql
    assert "BEFORE INSERT OR UPDATE ON property_content_job_events" in migration_sql
    assert "BEFORE INSERT OR UPDATE ON property_content_webhook_events" in migration_sql


def test_schema_v13_fixes_polymorphic_content_authority_trigger() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[12]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 13
    assert migration.name == "property_content_polymorphic_authority_trigger_fix"
    assert "CREATE OR REPLACE FUNCTION property_content_enforce_account_authority" in migration_sql
    assert "embedded_row := to_jsonb(NEW)->'row_json'" in migration_sql
    assert "NEW.row_json" not in migration_sql
    assert "TG_TABLE_NAME IN ( 'property_content_jobs', 'property_content_webhook_events' )" in migration_sql
    assert "property_content_row_owner_mismatch" in migration_sql


def test_schema_v14_splits_packet_erasure_triggers_by_table_composite() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[13]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 14
    assert migration.name == "property_research_packet_erasure_trigger_split"
    link_body = migration.sql.split(
        "CREATE OR REPLACE FUNCTION "
        "property_research_packet_links_enforce_erasure_fence()",
        1,
    )[1].split(
        "$property_research_packet_links_erasure_fence_function$;",
        1,
    )[0]
    membership_body = migration.sql.split(
        "CREATE OR REPLACE FUNCTION "
        "property_research_packet_memberships_enforce_erasure_fence()",
        1,
    )[1].split(
        "$property_research_packet_memberships_erasure_fence_function$;",
        1,
    )[0]

    link_columns = {
        "candidate_ref",
        "last_run_id",
        "principal_id",
        "principal_key",
    }
    membership_columns = {
        "candidate_ref",
        "principal_id",
        "principal_key",
        "run_id",
    }
    assert set(re.findall(r"\b(?:NEW|OLD)\.([a-z_][a-z0-9_]*)\b", link_body)) <= (
        link_columns
    )
    assert set(
        re.findall(r"\b(?:NEW|OLD)\.([a-z_][a-z0-9_]*)\b", membership_body)
    ) <= membership_columns
    assert "NEW.run_id" not in link_body
    assert "OLD.run_id" not in link_body
    assert "NEW.last_run_id" in link_body
    assert "NEW.run_id IS DISTINCT FROM OLD.run_id" in membership_body
    assert migration_sql.count("BEFORE INSERT OR UPDATE ON property_research_packet") == 2
    assert (
        "EXECUTE FUNCTION property_research_packet_links_enforce_erasure_fence()"
        in migration_sql
    )
    assert (
        "EXECUTE FUNCTION property_research_packet_memberships_enforce_erasure_fence()"
        in migration_sql
    )
    assert (
        "DROP FUNCTION IF EXISTS property_research_packets_enforce_erasure_fence()"
        in migration_sql
    )


def test_schema_v16_installs_distributed_admission_state() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[15]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 16
    assert migration.name == "distributed_request_admission_control"
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_admission_quota_buckets" in migration_sql
    assert "bucket_key TEXT PRIMARY KEY" in migration_sql
    assert "window_index BIGINT NOT NULL" in migration_sql
    assert "used_units BIGINT NOT NULL" in migration_sql
    assert "limit_units BIGINT NOT NULL" in migration_sql
    assert "idx_propertyquarry_admission_quota_expiry" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_admission_leases" in migration_sql
    assert "lease_id UUID NOT NULL" in migration_sql
    assert migration_sql.count("limit_units BIGINT NOT NULL") == 2
    assert "PRIMARY KEY (lease_id, dimension_key)" in migration_sql
    assert "idx_propertyquarry_admission_lease_dimension_expiry" in migration_sql
    assert "idx_propertyquarry_admission_lease_expiry" in migration_sql
    assert "CHECK (char_length(bucket_key) BETWEEN 1 AND 512)" in migration_sql
    assert "CHECK (char_length(dimension_key) BETWEEN 1 AND 512)" in migration_sql
    assert migration_sql.count("CHECK (limit_units >= 1)") == 2


def test_schema_v17_installs_bounded_canonical_admission_capacity_state() -> None:
    migration = schema.PROPERTY_SEARCH_MIGRATIONS[16]
    migration_sql = " ".join(migration.sql.split())

    assert migration.version == 17
    assert migration.name == "bounded_admission_capacity_state"
    assert (
        "LOCK TABLE propertyquarry_admission_quota_buckets, "
        "propertyquarry_admission_leases IN ACCESS EXCLUSIVE MODE"
        in migration_sql
    )
    assert "quota_count > 1000000" in migration_sql
    assert "lease_count > 100000" in migration_sql
    assert "propertyquarry_admission_quota_capacity_exceeded" in migration_sql
    assert "propertyquarry_admission_lease_capacity_exceeded" in migration_sql
    assert "CREATE TABLE propertyquarry_admission_capacity_state" in migration_sql
    assert (
        "CREATE TABLE IF NOT EXISTS propertyquarry_admission_capacity_state"
        not in migration_sql
    )
    assert "capacity_key TEXT PRIMARY KEY" in migration_sql
    assert "row_count BIGINT NOT NULL" in migration_sql
    assert "row_limit BIGINT NOT NULL" in migration_sql
    assert "capacity_key = 'quota' AND row_limit = 1000000" in migration_sql
    assert "capacity_key = 'lease' AND row_limit = 100000" in migration_sql
    assert migration_sql.count("SECURITY DEFINER") == 3
    assert migration_sql.count("SET search_path = pg_catalog") == 3
    assert migration_sql.count("FOR EACH STATEMENT") == 6
    assert migration_sql.count("REFERENCING NEW TABLE AS") == 2
    assert migration_sql.count("REFERENCING OLD TABLE AS") == 2
    assert migration_sql.count("AFTER TRUNCATE ON") == 2
    assert "propertyquarry_admission_capacity_owner_role_unsafe" in migration_sql
    assert "ALTER FUNCTION %I.%I() OWNER TO %I" in migration_sql
    assert "REVOKE ALL PRIVILEGES ON FUNCTION" in migration_sql
    assert "GRANT SELECT, UPDATE ON TABLE %I.%I TO %I" in migration_sql


@pytest.mark.parametrize("relation", tuple(sorted(_relations_for(17))))
def test_schema_v17_relations_are_required_for_readiness(relation: str) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove(relation)

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_relation_missing:{relation}"


@pytest.mark.parametrize(
    ("relation", "trigger"),
    tuple(sorted(_triggers_for(17))),
)
def test_schema_v17_triggers_are_required_for_readiness(
    relation: str,
    trigger: str,
) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.triggers.remove((relation, trigger))

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_trigger_missing:{trigger}"


@pytest.mark.parametrize("relation", tuple(sorted(_relations_for(16))))
def test_schema_v16_relations_are_required_for_readiness(relation: str) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove(relation)

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_relation_missing:{relation}"


@pytest.mark.parametrize("relation", tuple(sorted(_relations_for(11))))
def test_schema_v11_relations_are_required_for_readiness(relation: str) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove(relation)

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_relation_missing:{relation}"


@pytest.mark.parametrize("relation", tuple(sorted(_relations_for(12))))
def test_schema_v12_relations_are_required_for_readiness(relation: str) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove(relation)

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_relation_missing:{relation}"


@pytest.mark.parametrize("relation", tuple(sorted(_relations_for(10))))
def test_schema_v10_relations_are_required_for_readiness(relation: str) -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.relations.remove(relation)

    status = schema.inspect_property_search_schema(
        "postgresql://test/property",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.current_version == schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    assert status.reason == f"required_relation_missing:{relation}"


def test_schema_readiness_is_mandatory_in_prod_and_opt_in_for_dev() -> None:
    for role in ("api", "worker", "scheduler"):
        assert schema.property_search_schema_readiness_required(
            runtime_mode="prod",
            role=role,
        )
    assert not schema.property_search_schema_readiness_required(
        runtime_mode="dev",
        role="api",
    )
    assert schema.property_search_schema_readiness_required(
        runtime_mode="dev",
        role="api",
        explicit="true",
    )


def test_schema_capacity_reconciliation_is_atomic_and_optional_for_hot_probes() -> None:
    database = _FakeDatabase()
    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    database.admission_capacity["lease"] = 1
    database.admission_actual_counts["lease"] = 2

    deep_cursor = _FakeCursor(database)
    deep_status = schema.inspect_property_search_schema_cursor(deep_cursor)

    assert deep_status.ready is False
    assert deep_status.reason == "ingress_admission_capacity_count_mismatch:lease"
    deep_capacity_queries = [
        sql
        for sql, _params in database.executed
        if sql.startswith(
            "SELECT capacity_key, row_count, row_limit"
        )
    ]
    assert len(deep_capacity_queries) == 1
    assert "AS actual_row_count" in deep_capacity_queries[0]

    database.executed.clear()
    hot_cursor = _FakeCursor(database)
    hot_status = schema.inspect_property_search_schema_cursor(
        hot_cursor,
        verify_capacity_counts=False,
    )

    assert hot_status.ready is True
    hot_capacity_queries = [
        sql
        for sql, _params in database.executed
        if sql.startswith(
            "SELECT capacity_key, row_count, row_limit"
        )
    ]
    assert len(hot_capacity_queries) == 1
    assert "COUNT(*)" not in hot_capacity_queries[0]


def test_container_readiness_requires_current_schema_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.container import ReadinessService

    database = _FakeDatabase()
    fake_psycopg = SimpleNamespace(
        connect=lambda _url, **kwargs: database.connect(
            _url,
            autocommit=bool(kwargs.get("autocommit")),
        )
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.delenv(
        "PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED",
        raising=False,
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "container-readiness-erasure-secret",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        "postgresql://test/property-admission",
    )
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    settings = SimpleNamespace(
        database_url="postgresql://test/property",
        storage=SimpleNamespace(database_url="postgresql://test/property"),
        runtime_mode="prod",
        role="api",
    )
    readiness = ReadinessService(settings)

    def fake_admission_probe(_cursor) -> None:  # noqa: ANN001
        if not database.admission_write_authority:
            from app.services.admission_control import AdmissionBackendUnavailable

            raise AdmissionBackendUnavailable(
                "admission_backend_write_authority_missing"
            )

    monkeypatch.setattr("app.container.probe_admission_cursor", fake_admission_probe)

    ready, reason = readiness._probe_database()
    assert ready is False
    assert reason == "property_search_schema_not_ready:migration_ledger_missing"

    for version in range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1):
        database.seed_migration(version)
    from app.product.property_search_storage import _property_search_erasure_key_id

    ready, reason = readiness._probe_database()
    assert ready is False
    assert reason == "property_search_erasure_key_not_ready:key_state_missing"

    database.erasure_key_id = "0" * 64
    ready, reason = readiness._probe_database()
    assert ready is False
    assert reason == "property_search_erasure_key_not_ready:key_id_mismatch"

    database.erasure_key_id = _property_search_erasure_key_id()
    ready, reason = readiness._probe_database()
    assert ready is True
    assert reason == (
        f"postgres_ready:property_search_schema_v{schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION}"
    )

    database.admission_write_authority = False
    ready, reason = readiness._probe_database()
    assert ready is False
    assert reason == (
        "propertyquarry_admission_not_ready:"
        "admission_backend_write_authority_missing"
    )
    database.admission_write_authority = True

    monkeypatch.delenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", raising=False
    )
    ready, reason = readiness._probe_database()
    assert ready is False
    assert reason == (
        "property_search_erasure_key_not_ready:"
        "property_search_erasure_secret_required"
    )


def test_health_ready_reports_authoritative_property_search_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes.health import health_ready

    monkeypatch.setenv("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", "0")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", "0")
    container = SimpleNamespace(
        readiness=SimpleNamespace(
            check=lambda: (True, "postgres_ready:property_search_schema_v14")
        )
    )

    payload = asyncio.run(health_ready(container))

    assert payload == {
        "status": "ready",
        "reason": "postgres_ready:property_search_schema_v14",
        "property_search_schema_version": schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
    }


def test_health_ready_accepts_required_fresh_worker_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.api.routes.health import health_ready

    heartbeat_path = tmp_path / "worker-heartbeat.json"
    heartbeat_path.write_text(
        json.dumps({"role": "worker", "epoch": time.time(), "pid": 42}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", "0")
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")
    container = SimpleNamespace(
        readiness=SimpleNamespace(
            check=lambda: (True, "postgres_ready:property_search_schema_v15")
        )
    )

    payload = asyncio.run(health_ready(container))

    assert payload["status"] == "ready"
    assert payload["reason"] == "postgres_ready:property_search_schema_v15"


@pytest.mark.parametrize(
    ("heartbeat_state", "expected_reason"),
    (
        ("missing", "worker_heartbeat_missing_or_invalid"),
        ("malformed", "worker_heartbeat_missing_or_invalid"),
        ("oversized", "worker_heartbeat_missing_or_invalid"),
        ("wrong_role", "worker_heartbeat_stale_or_invalid"),
        ("future", "worker_heartbeat_stale_or_invalid"),
        ("stale", "worker_heartbeat_stale_or_invalid"),
    ),
)
def test_health_ready_fails_closed_for_unhealthy_required_worker_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    heartbeat_state: str,
    expected_reason: str,
) -> None:
    from fastapi import HTTPException

    from app.api.routes.health import health_ready

    heartbeat_path = tmp_path / "worker-heartbeat.json"
    if heartbeat_state == "malformed":
        heartbeat_path.write_text("{", encoding="utf-8")
    elif heartbeat_state == "oversized":
        heartbeat_path.write_bytes(b"x" * ((64 * 1024) + 1))
    elif heartbeat_state == "wrong_role":
        heartbeat_path.write_text(
            json.dumps({"role": "scheduler", "epoch": time.time()}),
            encoding="utf-8",
        )
    elif heartbeat_state == "future":
        heartbeat_path.write_text(
            json.dumps({"role": "worker", "epoch": time.time() + 60}),
            encoding="utf-8",
        )
    elif heartbeat_state == "stale":
        heartbeat_path.write_text(
            json.dumps({"role": "worker", "epoch": time.time() - 60}),
            encoding="utf-8",
        )
    monkeypatch.setenv("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", "0")
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")
    container = SimpleNamespace(
        readiness=SimpleNamespace(
            check=lambda: (True, "postgres_ready:property_search_schema_v15")
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(health_ready(container))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == f"not_ready:{expected_reason}"


def test_health_ready_accepts_required_fresh_scheduler_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.api.routes.health import health_ready

    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    heartbeat_path.write_text(
        json.dumps({"role": "scheduler", "epoch": time.time(), "pid": 43}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", "0")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", "1")
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS", "30")
    container = SimpleNamespace(
        readiness=SimpleNamespace(
            check=lambda: (True, "postgres_ready:property_search_schema_v15")
        )
    )

    payload = asyncio.run(health_ready(container))

    assert payload["status"] == "ready"
    assert payload["reason"] == "postgres_ready:property_search_schema_v15"


def test_health_ready_fails_closed_for_missing_required_scheduler_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    from app.api.routes.health import health_ready

    monkeypatch.setenv("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", "0")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", "1")
    monkeypatch.setenv(
        "EA_SCHEDULER_HEARTBEAT_PATH",
        str(tmp_path / "missing-scheduler-heartbeat.json"),
    )
    container = SimpleNamespace(
        readiness=SimpleNamespace(
            check=lambda: (True, "postgres_ready:property_search_schema_v15")
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(health_ready(container))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "not_ready:scheduler_heartbeat_missing_or_invalid"


def test_release_manifest_parser_maps_complete_authority_envelope() -> None:
    from app.api.routes.health import (
        _load_release_manifest_values,
        _release_manifest_sha256,
    )

    from scripts.verify_generated_release_artifacts_clean import (
        RELEASE_MANIFEST_FIELDS,
        release_manifest_sha256,
    )

    _load_release_manifest_values.cache_clear()
    values, errors = _load_release_manifest_values()

    assert errors == ()
    assert tuple(values) == RELEASE_MANIFEST_FIELDS
    assert _release_manifest_sha256(values) == release_manifest_sha256(values)


def test_release_manifest_status_fails_closed_on_runtime_override_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes.health import _load_release_manifest_values, _release_manifest

    for name in (
        "PROPERTYQUARRY_RELEASE_REPOSITORY",
        "PROPERTYQUARRY_RELEASE_REPOSITORY_ORIGIN",
        "PROPERTYQUARRY_RELEASE_MIRROR_REPOSITORY",
        "PROPERTYQUARRY_RELEASE_MIRROR_ORIGIN",
        "PROPERTYQUARRY_RELEASE_BRANCH",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID",
        "PROPERTYQUARRY_RELEASE_PUBLIC_ORIGIN",
        "PROPERTYQUARRY_PUBLIC_BASE_URL",
        "EA_PUBLIC_APP_BASE_URL",
        "PROPERTYQUARRY_RELEASE_ARTIFACT_SET",
        "PROPERTYQUARRY_RELEASE_LABEL",
        "PROPERTYQUARRY_RELEASE_GENERATED_AT",
        "PROPERTYQUARRY_RELEASE_VERIFICATION_COMMANDS",
    ):
        monkeypatch.delenv(name, raising=False)
    _load_release_manifest_values.cache_clear()

    assert _release_manifest()["release_manifest_status"] == "complete"

    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_REPOSITORY", "wrong/repository")
    mismatched = _release_manifest()
    assert mismatched["release_manifest_status"] == "mismatch"
    assert mismatched["release_manifest_mismatch_fields"] == "release_repository"


def test_release_manifest_missing_field_cannot_be_filled_from_runtime_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import health as health_module

    manifest_values, _errors = health_module._load_release_manifest_values()
    incomplete = dict(manifest_values)
    incomplete.pop("release_repository")
    monkeypatch.setattr(
        health_module,
        "_load_release_manifest_values",
        lambda: (incomplete, ("missing_field:release_repository",)),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_RELEASE_REPOSITORY",
        manifest_values["release_repository"],
    )

    payload = health_module._release_manifest()

    assert payload["release_manifest_status"] == "invalid"
    assert payload["release_repository"] == ""
    assert payload["release_manifest_sha256"] == ""
    assert payload["release_manifest_mismatch_fields"] == "release_repository"
    assert payload["release_manifest_errors"] == "missing_field:release_repository"


def test_migration_checksums_are_stable_and_unique() -> None:
    checksums = [migration.checksum for migration in schema.PROPERTY_SEARCH_MIGRATIONS]

    assert [migration.version for migration in schema.PROPERTY_SEARCH_MIGRATIONS] == list(
        range(1, schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION + 1)
    )
    assert checksums == [
        "4938925d3679ca592f67de1fb5f5c5538ce0e2c93dd2435ffe1204674d02a37e",
        "9beb0cbc778018c9ea7ee5939cbd25a86830a904a8c2bfe8454a022219a078a6",
        "f89e047a0ed002e2da26884077001a91c7f69faa57e5719ac73881d68a14d93a",
        "b4a28da18a3d31d328ffa13c67b90a8ae1b1c3b1920c980dafc2343d226a20c3",
        "4f54431f5a138f03d697837b2c0940462a51ed3b6bafae754f316a4757edfe23",
        "5d3855e9cdbfc2b82b97f5be9101188e0a2907ed9ca080f39c533abbae143008",
        "5d7ac5e0d805546f2f4e282323c3ba5dcda1c25f7e5947b1b13ad6df590a93e3",
        "0a7159b3a8c03c070c7158578d4d55549e1dbb43957d035e00a1e7e91f0de956",
        "ab63b9217f8c6da7e4ef6d82af9ebc91723e261e3c9f1edba17ea0fd49ce19c4",
        "83f07c1d91968753e454c79972110881259a01953a6755cfef020adf55e92bc4",
        "83f78ac907ccfb82f8cd4c61eddb4e5437dfc13f7e66143250f6e6bbdd2e2d47",
        "92901d215583a8c41854e3c3236417aca61fa21f03460777f15e5cec7626d25f",
        "192d605e9a96e73bde817c51f28317b491313ebe3cb61f1b4c617256dbb2f8cf",
        "0e89b189e06f2fbaaed1639e80951f87780d4102704d3371bbfc6d48bd124d0b",
        "2f20534f4d824d1bceb763c6016358d2266c1f7e70fda60267005f50b2b53629",
        "11069fd9275f1150beb57cc95d911ce9b2a9ae6bc09793d25ccd4ca8732f4140",
        "25a1fcfc28060abc309f7c767889964b23e694c3ae88209105b23a6ca33ac797",
        "eae758d281c5447d984f15e7d367e3fb181d06f79b6f0b5d277d6b91a2d455e8",
        "3cee796e77912373a948ee9d9f4613ace502374cad74e753b5073f46366aa96a",
    ]
