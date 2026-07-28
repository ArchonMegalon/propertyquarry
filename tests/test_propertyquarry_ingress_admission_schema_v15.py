from __future__ import annotations

from collections.abc import Sequence

import pytest
from app.product import property_search_schema as schema
from app.product import property_search_storage

_V1_TO_V14_CHECKSUMS = (
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
)


def _ledger_rows(
    migrations: Sequence[schema.PropertySearchMigration],
) -> list[tuple[int, str, str]]:
    return [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations
    ]


class _MigrationCursor:
    def __init__(self, *, fail_v15: bool = False) -> None:
        self.fail_v15 = fail_v15
        self.executed: list[tuple[str, object | None]] = []

    def __enter__(self) -> _MigrationCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.executed.append((sql, params))
        if self.fail_v15 and sql == schema.PROPERTY_SEARCH_MIGRATIONS[-1].sql:
            raise RuntimeError("simulated_v15_ddl_failure")

    def fetchall(self) -> list[tuple[int, str, str]]:
        return _ledger_rows(schema.PROPERTY_SEARCH_MIGRATIONS[:-1])


class _MigrationConnection:
    def __init__(self, *, fail_v15: bool = False) -> None:
        self.cursor_instance = _MigrationCursor(fail_v15=fail_v15)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _MigrationCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _ReadinessCursor:
    def __init__(
        self,
        *,
        capacity_rows: Sequence[Sequence[object]] | None = None,
        actual_counts: tuple[int, int] = (0, 0),
    ) -> None:
        self.capacity_rows = list(
            capacity_rows
            if capacity_rows is not None
            else (
                (
                    "lease",
                    0,
                    schema.PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT,
                    schema.PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
                ),
                (
                    "quota",
                    0,
                    schema.PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT,
                    schema.PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
                ),
            )
        )
        self.actual_counts = actual_counts
        self.sql = ""
        self.params: object | None = None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[Sequence[object]]:
        if "FROM propertyquarry_ingress_admission_capacity" in self.sql:
            if "AS actual_row_count" in self.sql:
                return [
                    (*row, self.actual_counts[index])
                    for index, row in enumerate(self.capacity_rows)
                ]
            return self.capacity_rows
        return _ledger_rows(schema.PROPERTY_SEARCH_MIGRATIONS)

    def fetchone(self) -> tuple[object, ...]:
        if "to_regclass" in self.sql:
            assert isinstance(self.params, tuple)
            relation = str(self.params[0])
            if relation in schema._FORBIDDEN_RELATIONS:
                return (None,)
            return (relation,)
        if "to_regprocedure" in self.sql:
            assert isinstance(self.params, tuple)
            return (self.params[0],)
        if "SELECT EXISTS" in self.sql:
            return (True,)
        raise AssertionError(f"unexpected readiness query: {self.sql}")


def test_v15_is_append_only_and_preserves_prior_migration_checksums() -> None:
    assert tuple(
        migration.version for migration in schema.PROPERTY_SEARCH_MIGRATIONS
    ) == tuple(range(1, 16))
    assert tuple(
        migration.checksum for migration in schema.PROPERTY_SEARCH_MIGRATIONS[:-1]
    ) == _V1_TO_V14_CHECKSUMS
    latest = schema.PROPERTY_SEARCH_MIGRATIONS[-1]
    assert latest.version == 15
    assert latest.name == "authoritative_distributed_ingress_admission"
    assert (
        latest.checksum
        == "02fe41df2ce7fa85eeea60d989a4b7f0aa15bac713227ce5a288c2b724313feb"
    )
    assert schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION == 15


def test_v15_defines_bounded_authoritative_capacity_contract() -> None:
    sql = schema.PROPERTY_SEARCH_MIGRATIONS[-1].sql
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_ingress_quota_buckets" in sql
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_ingress_leases" in sql
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "propertyquarry_ingress_admission_capacity" in sql
    )
    assert "PRIMARY KEY (quota_kind, dimension, subject_digest, window_id)" in sql
    assert "quota_kind IN ('request', 'cost')" in sql
    assert "dimension IN ('ip', 'account')" in sql
    assert "lease_token UUID PRIMARY KEY" in sql
    assert "^hmac-sha256:[0-9a-f]{64}$" in sql
    assert "propertyquarry_ingress_lease_horizon_check" in sql
    assert "expires_at >= heartbeat_at + INTERVAL '1 second'" in sql
    assert "expires_at <= heartbeat_at + INTERVAL '3600 seconds'" in sql
    assert "capacity_key IN ('lease', 'quota')" in sql
    assert "hard_limit = 100000" in sql
    assert "hard_limit = 1000000" in sql
    assert "row_count < hard_limit" in sql
    assert "row_count > 0" in sql
    assert "pg_trigger_depth() <> 2" in sql
    assert "BEFORE TRUNCATE ON propertyquarry_ingress_quota_buckets" in sql
    assert "BEFORE TRUNCATE ON propertyquarry_ingress_leases" in sql
    assert (
        "BEFORE TRUNCATE ON propertyquarry_ingress_admission_capacity" in sql
    )


def test_upgrade_from_v14_applies_only_v15_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "test-erasure-key",
    )
    connection = _MigrationConnection()

    result = schema.migrate_property_search_schema(
        "postgresql://schema.test/property",
        applied_by="schema-v15-test",
        connect=lambda *args, **kwargs: connection,
    )

    assert result.previous_version == 14
    assert result.current_version == 15
    assert result.applied_versions == (15,)
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True
    assert (
        schema.PROPERTY_SEARCH_MIGRATIONS[-1].sql,
        None,
    ) in connection.cursor_instance.executed


def test_v15_failure_rolls_back_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "test-erasure-key",
    )
    connection = _MigrationConnection(fail_v15=True)

    with pytest.raises(RuntimeError, match="simulated_v15_ddl_failure"):
        schema.migrate_property_search_schema(
            "postgresql://schema.test/property",
            applied_by="schema-v15-test",
            connect=lambda *args, **kwargs: connection,
        )

    assert connection.committed is False
    assert connection.rolled_back is True
    assert connection.closed is True
    ledger_inserts = [
        params
        for sql, params in connection.cursor_instance.executed
        if f"INSERT INTO {schema.SCHEMA_LEDGER_TABLE}" in sql
    ]
    assert ledger_inserts == []


def test_readiness_accepts_exact_capacity_contract_and_counts() -> None:
    status = schema.inspect_property_search_schema_cursor(_ReadinessCursor())

    assert status.ready is True
    assert status.reason == "schema_ready"
    assert status.current_version == 15


def test_readiness_rejects_capacity_contract_drift() -> None:
    status = schema.inspect_property_search_schema_cursor(
        _ReadinessCursor(
            capacity_rows=(
                (
                    "lease",
                    0,
                    schema.PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT + 1,
                    schema.PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
                ),
                (
                    "quota",
                    0,
                    schema.PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT,
                    schema.PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
                ),
            )
        )
    )

    assert status.ready is False
    assert status.reason == "ingress_admission_capacity_contract_invalid"


def test_readiness_rejects_capacity_counter_drift() -> None:
    status = schema.inspect_property_search_schema_cursor(
        _ReadinessCursor(actual_counts=(1, 0))
    )

    assert status.ready is False
    assert status.reason == "ingress_admission_capacity_count_mismatch:lease"
