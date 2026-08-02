from __future__ import annotations

from collections.abc import Sequence

import pytest
from app.product import property_search_schema as schema
from app.product import property_search_storage

_V1_TO_V16_CHECKSUMS = (
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
)


def _ledger_rows(
    migrations: Sequence[schema.PropertySearchMigration],
) -> list[tuple[int, str, str]]:
    return [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations
    ]


class _MigrationCursor:
    def __init__(self, *, fail_v17: bool = False) -> None:
        self.fail_v17 = fail_v17
        self.executed: list[tuple[str, object | None]] = []

    def __enter__(self) -> _MigrationCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.executed.append((sql, params))
        if self.fail_v17 and sql == schema.PROPERTY_SEARCH_MIGRATIONS[16].sql:
            raise RuntimeError("simulated_v17_ddl_failure")

    def fetchall(self) -> list[tuple[int, str, str]]:
        return _ledger_rows(schema.PROPERTY_SEARCH_MIGRATIONS[:16])


class _MigrationConnection:
    def __init__(self, *, fail_v17: bool = False) -> None:
        self.cursor_instance = _MigrationCursor(fail_v17=fail_v17)
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
                    schema.ADMISSION_LEASE_ROW_LIMIT,
                ),
                (
                    "quota",
                    0,
                    schema.ADMISSION_QUOTA_ROW_LIMIT,
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
        if "FROM propertyquarry_admission_capacity_state" in self.sql:
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


def test_v18_is_append_only_and_preserves_prior_migration_checksums() -> None:
    assert tuple(
        migration.version for migration in schema.PROPERTY_SEARCH_MIGRATIONS
    ) == tuple(range(1, 19))
    assert tuple(
        migration.checksum for migration in schema.PROPERTY_SEARCH_MIGRATIONS[:16]
    ) == _V1_TO_V16_CHECKSUMS
    v17 = schema.PROPERTY_SEARCH_MIGRATIONS[16]
    assert v17.version == 17
    assert v17.name == "bounded_admission_capacity_state"
    assert (
        v17.checksum
        == "25a1fcfc28060abc309f7c767889964b23e694c3ae88209105b23a6ca33ac797"
    )
    v18 = schema.PROPERTY_SEARCH_MIGRATIONS[17]
    assert v18.version == 18
    assert v18.name == "nonpublishing_work_lease_heartbeat"
    assert (
        v18.checksum
        == "eae758d281c5447d984f15e7d367e3fb181d06f79b6f0b5d277d6b91a2d455e8"
    )
    assert schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION == 18


def test_v16_and_v17_define_bounded_authoritative_capacity_contract() -> None:
    admission_sql = schema.PROPERTY_SEARCH_MIGRATIONS[15].sql
    capacity_sql = schema.PROPERTY_SEARCH_MIGRATIONS[16].sql
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_admission_quota_buckets" in admission_sql
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_admission_leases" in admission_sql
    assert "PRIMARY KEY (lease_id, dimension_key)" in admission_sql
    assert "CHECK (expires_at > acquired_at)" in admission_sql
    assert "CREATE TABLE propertyquarry_admission_capacity_state" in capacity_sql
    assert "capacity_key IN ('quota', 'lease')" in capacity_sql
    assert "row_limit = 100000" in capacity_sql
    assert "row_limit = 1000000" in capacity_sql
    assert "row_count <= row_limit - $1" in capacity_sql
    assert "row_count >= $1 AND row_count <= row_limit" in capacity_sql
    assert "AFTER INSERT ON propertyquarry_admission_quota_buckets" in capacity_sql
    assert "AFTER DELETE ON propertyquarry_admission_leases" in capacity_sql
    assert "AFTER TRUNCATE ON propertyquarry_admission_leases" in capacity_sql


def test_upgrade_from_v16_applies_v17_and_v18_and_commits(
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
        applied_by="schema-v17-test",
        connect=lambda *args, **kwargs: connection,
    )

    assert result.previous_version == 16
    assert result.current_version == 18
    assert result.applied_versions == (17, 18)
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True
    assert (
        schema.PROPERTY_SEARCH_MIGRATIONS[16].sql,
        None,
    ) in connection.cursor_instance.executed
    assert (
        schema.PROPERTY_SEARCH_MIGRATIONS[17].sql,
        None,
    ) in connection.cursor_instance.executed


def test_v17_failure_rolls_back_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "test-erasure-key",
    )
    connection = _MigrationConnection(fail_v17=True)

    with pytest.raises(RuntimeError, match="simulated_v17_ddl_failure"):
        schema.migrate_property_search_schema(
            "postgresql://schema.test/property",
            applied_by="schema-v17-test",
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
    assert status.current_version == 18


def test_readiness_rejects_capacity_contract_drift() -> None:
    status = schema.inspect_property_search_schema_cursor(
        _ReadinessCursor(
            capacity_rows=(
                (
                    "lease",
                    0,
                    schema.ADMISSION_LEASE_ROW_LIMIT + 1,
                ),
                (
                    "quota",
                    0,
                    schema.ADMISSION_QUOTA_ROW_LIMIT,
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
