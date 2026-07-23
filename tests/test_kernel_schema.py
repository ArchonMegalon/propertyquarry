from __future__ import annotations

import ast
import os
from pathlib import Path
import re
from typing import Iterator
import uuid

import pytest
import yaml

from app.kernel_schema import (
    KERNEL_MIGRATION_SPECS,
    KernelMigration,
    KernelSchemaDriftError,
    KernelSchemaNotReadyError,
    LATEST_KERNEL_SCHEMA_VERSION,
    _validate_applied_rows,
    inspect_kernel_schema_cursor,
    load_kernel_migrations,
    migrate_kernel_schema,
    require_kernel_schema_ready,
    required_kernel_relations,
)


ROOT = Path(__file__).resolve().parents[1]
CREATE_TABLE_PATTERN = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)",
    re.IGNORECASE,
)
AUTH_PROVIDER_ENV_KEYS = {
    "EMAILIT_API_KEY",
    "EA_EMAIL_DEFAULT_FROM",
    "EA_EMAIL_DEFAULT_NAME",
    "EA_REGISTRATION_EMAIL_FROM",
    "EA_REGISTRATION_EMAIL_NAME",
    "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
    "EA_REGISTRATION_EMAIL_NAME_FALLBACK",
    "EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
    "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
    "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
    "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
    "EA_PROVIDER_SECRET_KEY",
}
LEGACY_EA_GOOGLE_AUTH_ENV_KEYS = {
    "EA_GOOGLE_OAUTH_CLIENT_ID",
    "EA_GOOGLE_OAUTH_CLIENT_SECRET",
    "EA_GOOGLE_OAUTH_REDIRECT_URI",
    "EA_GOOGLE_OAUTH_STATE_SECRET",
}


class _Cursor:
    def __init__(
        self,
        *,
        applied_rows: list[tuple[object, ...]] | None = None,
        missing_relation: str = "",
    ) -> None:
        self.applied_rows = applied_rows or []
        self.missing_relation = missing_relation
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self._one: tuple[object, ...] | None = None
        self._all: list[tuple[object, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> None:
        sql = str(statement)
        self.executed.append((sql, params))
        self._one = None
        self._all = []
        if "SELECT version, migration_name, checksum_sha256" in sql:
            self._all = list(self.applied_rows)
            return
        if "SELECT to_regclass" in sql:
            relation = str((params or ("",))[0])
            self._one = (None if relation == self.missing_relation else relation,)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._all)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _runtime_schema_created_tables() -> set[str]:
    relations: set[str] = set()
    for path in sorted((ROOT / "ea" / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in {"__init__", "_ensure_schema", "ensure_schema"}:
                continue
            for value in ast.walk(node):
                if not isinstance(value, ast.Constant) or not isinstance(
                    value.value, str
                ):
                    continue
                relations.update(
                    match.group("name")
                    for match in CREATE_TABLE_PATTERN.finditer(value.value)
                )
    return relations


def test_kernel_manifest_is_contiguous_complete_and_loadable() -> None:
    migrations = load_kernel_migrations()

    assert tuple(spec.version for spec in KERNEL_MIGRATION_SPECS) == tuple(
        range(2, LATEST_KERNEL_SCHEMA_VERSION + 1)
    )
    assert tuple(migration.version for migration in migrations) == tuple(
        range(2, LATEST_KERNEL_SCHEMA_VERSION + 1)
    )
    assert all(len(migration.checksum) == 64 for migration in migrations)


def test_canonical_kernel_migrations_cover_every_runtime_created_table() -> None:
    migration_sql = "\n".join(migration.sql for migration in load_kernel_migrations())
    migrated_tables = {
        match.group("name") for match in CREATE_TABLE_PATTERN.finditer(migration_sql)
    }

    missing = sorted(_runtime_schema_created_tables() - migrated_tables)

    assert missing == []


def test_runtime_repository_contract_contains_previously_missing_tables() -> None:
    migration = next(
        migration for migration in load_kernel_migrations() if migration.version == 37
    )
    expected = {
        "evidence_objects",
        "onboarding_states",
        "person_profiles",
        "preference_nodes",
        "preference_evidence_events",
        "preference_decision_assessments",
        "preference_profile_corrections",
        "onemin_accounts",
        "onemin_credentials",
        "onemin_allocation_leases",
        "property_decision_ledger",
        "property_evidence_claims",
        "property_agent_question_tasks",
        "property_documents",
        "property_packet_publications",
        "property_packet_publication_events",
        "property_packet_schema_versions",
        "response_records",
    }

    assert migration.version == 37
    assert expected <= {
        match.group("name") for match in CREATE_TABLE_PATTERN.finditer(migration.sql)
    }


def test_operator_profile_principal_scope_migration_is_nondestructive_and_exact() -> (
    None
):
    migration = next(
        migration for migration in load_kernel_migrations() if migration.version == 38
    )

    assert "DROP CONSTRAINT IF EXISTS operator_profiles_pkey" in migration.sql
    assert "to_regclass('operator_profiles')" in migration.sql
    assert "public.operator_profiles" not in migration.sql
    assert "operator_profiles_relation" in migration.sql
    assert "operator_profiles_schema" in migration.sql
    assert "operator_profiles_table" in migration.sql
    assert "index_row.indisunique" in migration.sql
    assert "index_row.indisvalid" in migration.sql
    assert "index_row.indpred IS NULL" in migration.sql
    assert "index_row.indexprs IS NULL" in migration.sql
    assert "index_row.indnkeyatts = 2" in migration.sql
    assert "index_row.indnatts = 2" in migration.sql
    assert "= 'principal_id'" in migration.sql
    assert "= 'operator_id'" in migration.sql
    assert "GROUP BY principal_id, operator_id" in migration.sql
    assert "CREATE UNIQUE INDEX idx_operator_profiles_principal_operator" in (
        migration.sql
    )
    assert not re.search(r"\b(?:DELETE|TRUNCATE)\b", migration.sql, re.IGNORECASE)


def test_applied_kernel_ledger_rejects_checksum_drift_gap_and_future_version() -> None:
    migrations = (
        KernelMigration(2, "two", "two.sql", "SELECT 2"),
        KernelMigration(3, "three", "three.sql", "SELECT 3"),
    )
    valid_rows = [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations
    ]

    assert _validate_applied_rows(valid_rows, migrations) == (2, 3)
    with pytest.raises(KernelSchemaDriftError, match="checksum_drift:2"):
        _validate_applied_rows([(2, "two", "0" * 64)], migrations)
    with pytest.raises(KernelSchemaDriftError, match="migration_gap"):
        _validate_applied_rows([valid_rows[1]], migrations)
    with pytest.raises(KernelSchemaDriftError, match="schema_ahead:4"):
        _validate_applied_rows([(4, "future", "0" * 64)], migrations)


def test_migrate_kernel_schema_commits_all_files_with_ledger_rows() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    result = migrate_kernel_schema(
        "postgresql://migration.invalid/propertyquarry",
        applied_by="release-sha",
        connect=lambda *_args, **_kwargs: connection,
    )

    inserts = [
        params
        for statement, params in cursor.executed
        if "INSERT INTO ea_kernel_schema_migrations" in statement
    ]
    assert result.previous_version == 0
    assert result.current_version == LATEST_KERNEL_SCHEMA_VERSION
    assert result.applied_versions == tuple(range(2, LATEST_KERNEL_SCHEMA_VERSION + 1))
    assert len(inserts) == len(KERNEL_MIGRATION_SPECS)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1


def test_kernel_readiness_checks_ledger_checksums_and_declared_relations() -> None:
    migrations = load_kernel_migrations()
    applied_rows = [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations
    ]
    ready = inspect_kernel_schema_cursor(
        _Cursor(applied_rows=applied_rows),
        migrations,
    )
    missing_relation = required_kernel_relations(migrations)[-1]
    missing = inspect_kernel_schema_cursor(
        _Cursor(
            applied_rows=applied_rows,
            missing_relation=missing_relation,
        ),
        migrations,
    )

    assert ready.ready
    assert not missing.ready
    assert missing.reason == f"required_relation_missing:{missing_relation}"


def test_propertyquarry_schema_gate_runs_kernel_before_property_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import propertyquarry_schema

    events: list[str] = []

    class _Result:
        def __init__(self, component: str) -> None:
            self.component = component

        def as_dict(self) -> dict[str, object]:
            return {"component": self.component}

    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_kernel_schema",
        lambda *_args, **_kwargs: events.append("kernel:migrate") or _Result("kernel"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_kernel_schema_ready",
        lambda *_args, **_kwargs: events.append("kernel:verify"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_property_search_schema",
        lambda *_args, **_kwargs: (
            events.append("property_search:migrate") or _Result("property_search")
        ),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_property_search_schema_ready",
        lambda *_args, **_kwargs: events.append("property_search:verify"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_propertyquarry_google_identity_schema",
        lambda *_args, **_kwargs: (
            events.append("google_identity:migrate") or _Result("google_identity")
        ),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_propertyquarry_google_identity_schema_ready",
        lambda *_args, **_kwargs: events.append("google_identity:verify"),
    )

    result = propertyquarry_schema.migrate_propertyquarry_schema(
        "postgresql://migration.invalid/propertyquarry",
        applied_by="release-sha",
    )

    assert events == [
        "kernel:migrate",
        "kernel:verify",
        "property_search:migrate",
        "property_search:verify",
        "google_identity:migrate",
        "google_identity:verify",
    ]
    assert result["kernel"] == {"component": "kernel"}
    assert result["property_search"] == {"component": "property_search"}
    assert result["google_identity"] == {"component": "google_identity"}


def test_propertyquarry_schema_gate_stops_when_kernel_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.product import propertyquarry_schema

    property_search_called = False

    class _Result:
        def as_dict(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_kernel_schema",
        lambda *_args, **_kwargs: _Result(),
    )

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise KernelSchemaNotReadyError("kernel_schema_not_ready:test")

    def _unexpected(*_args: object, **_kwargs: object) -> _Result:
        nonlocal property_search_called
        property_search_called = True
        return _Result()

    monkeypatch.setattr(
        propertyquarry_schema,
        "require_kernel_schema_ready",
        _fail,
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_property_search_schema",
        _unexpected,
    )

    with pytest.raises(KernelSchemaNotReadyError):
        propertyquarry_schema.migrate_propertyquarry_schema(
            "postgresql://migration.invalid/propertyquarry",
            applied_by="release-sha",
        )

    assert not property_search_called


def test_propertyquarry_compose_uses_verified_combined_migration_gate() -> None:
    compose = yaml.safe_load(
        (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    assert services["propertyquarry-migrate"]["command"] == [
        "/usr/local/bin/python",
        "-m",
        "app.product.propertyquarry_schema",
        "migrate",
    ]
    for service_name in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
    ):
        assert services[service_name]["depends_on"]["propertyquarry-migrate"] == {
            "condition": "service_completed_successfully"
        }
    assert "COPY ea/schema /app/schema" in (
        ROOT / "ea" / "Dockerfile.property-web"
    ).read_text(encoding="utf-8")


def test_auth_provider_environment_is_api_only_and_value_free() -> None:
    compose = yaml.safe_load(
        (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    api_environment = services["propertyquarry-api"]["environment"]

    assert AUTH_PROVIDER_ENV_KEYS <= set(api_environment)
    assert LEGACY_EA_GOOGLE_AUTH_ENV_KEYS.isdisjoint(api_environment)
    assert api_environment["PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI"] == (
        "${PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI:"
        "-https://propertyquarry.com/google/callback}"
    )
    for key in AUTH_PROVIDER_ENV_KEYS:
        value = str(api_environment[key])
        assert value.startswith("${") and value.endswith("}")
    for service_name in (
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
        "propertyquarry-migrate",
    ):
        service_environment = services[service_name].get("environment", {})
        assert AUTH_PROVIDER_ENV_KEYS.isdisjoint(service_environment)
        assert LEGACY_EA_GOOGLE_AUTH_ENV_KEYS.isdisjoint(service_environment)


@pytest.fixture
def clean_postgres_schema_url() -> Iterator[str]:
    database_url = str(os.environ.get("PROPERTYQUARRY_TEST_POSTGRES_URL") or "").strip()
    if not database_url:
        pytest.skip("PROPERTYQUARRY_TEST_POSTGRES_URL is not configured")
    psycopg = pytest.importorskip("psycopg")
    schema_name = f"kernel_schema_test_{uuid.uuid4().hex}"
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        parameters = conninfo_to_dict(database_url)
        parameters["options"] = f"-csearch_path={schema_name}"
        isolated_url = make_conninfo(**parameters)
        yield isolated_url
    finally:
        try:
            with admin.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        finally:
            admin.close()


def test_kernel_migrations_are_repeatable_on_clean_real_postgres(
    clean_postgres_schema_url: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    first = migrate_kernel_schema(
        clean_postgres_schema_url,
        applied_by="integration-test",
    )
    require_kernel_schema_ready(clean_postgres_schema_url)
    second = migrate_kernel_schema(
        clean_postgres_schema_url,
        applied_by="integration-test",
    )

    assert first.applied_versions == tuple(range(2, LATEST_KERNEL_SCHEMA_VERSION + 1))
    assert second.applied_versions == ()
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT
                    index_row.indisunique,
                    index_row.indisvalid,
                    index_row.indisready,
                    index_row.indpred IS NULL,
                    index_row.indexprs IS NULL,
                    index_row.indnkeyatts,
                    index_row.indnatts,
                    pg_catalog.pg_get_indexdef(index_row.indexrelid, 1, TRUE),
                    pg_catalog.pg_get_indexdef(index_row.indexrelid, 2, TRUE)
                FROM pg_catalog.pg_index AS index_row
                JOIN pg_catalog.pg_class AS index_relation
                  ON index_relation.oid = index_row.indexrelid
                WHERE index_row.indrelid = 'operator_profiles'::pg_catalog.regclass
                  AND index_relation.relname =
                      'idx_operator_profiles_principal_operator'
                """
            )
            assert cur.fetchone() == (
                True,
                True,
                True,
                True,
                True,
                2,
                2,
                "principal_id",
                "operator_id",
            )
            cur.execute(
                """
                INSERT INTO operator_profiles (
                    operator_id,
                    principal_id,
                    display_name
                ) VALUES
                    ('shared-operator', 'principal-a', 'Operator A'),
                    ('shared-operator', 'principal-b', 'Operator B')
                """
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO operator_profiles (
                        operator_id,
                        principal_id,
                        display_name
                    ) VALUES (
                        'shared-operator',
                        'principal-a',
                        'Duplicate Operator A'
                    )
                    """
                )


def test_kernel_migration_ledger_adopts_legacy_bootstrapped_real_postgres(
    clean_postgres_schema_url: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            for migration in load_kernel_migrations():
                cur.execute(migration.sql)

    result = migrate_kernel_schema(
        clean_postgres_schema_url,
        applied_by="integration-test-adoption",
    )

    require_kernel_schema_ready(clean_postgres_schema_url)
    assert result.previous_version == 0
    assert result.applied_versions == tuple(range(2, LATEST_KERNEL_SCHEMA_VERSION + 1))


def test_operator_profile_migration_adopts_legacy_exact_unique_real_postgres(
    clean_postgres_schema_url: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            for migration in load_kernel_migrations():
                if migration.version >= 38:
                    break
                cur.execute(migration.sql)
            cur.execute(
                "ALTER TABLE operator_profiles DROP CONSTRAINT operator_profiles_pkey"
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX legacy_operator_profile_principal_identity
                ON operator_profiles(principal_id, operator_id)
                """
            )

    first = migrate_kernel_schema(
        clean_postgres_schema_url,
        applied_by="integration-test-legacy-unique-adoption",
    )
    second = migrate_kernel_schema(
        clean_postgres_schema_url,
        applied_by="integration-test-legacy-unique-repeat",
    )

    assert first.applied_versions == tuple(range(2, LATEST_KERNEL_SCHEMA_VERSION + 1))
    assert second.applied_versions == ()
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT index_relation.relname
                FROM pg_catalog.pg_index AS index_row
                JOIN pg_catalog.pg_class AS index_relation
                  ON index_relation.oid = index_row.indexrelid
                WHERE index_row.indrelid = 'operator_profiles'::pg_catalog.regclass
                  AND index_row.indisunique
                  AND index_row.indisvalid
                  AND index_row.indisready
                  AND index_row.indpred IS NULL
                  AND index_row.indexprs IS NULL
                  AND index_row.indnkeyatts = 2
                  AND index_row.indnatts = 2
                  AND pg_catalog.pg_get_indexdef(
                      index_row.indexrelid, 1, TRUE
                  ) = 'principal_id'
                  AND pg_catalog.pg_get_indexdef(
                      index_row.indexrelid, 2, TRUE
                  ) = 'operator_id'
                ORDER BY index_relation.relname
                """
            )
            assert cur.fetchall() == [("legacy_operator_profile_principal_identity",)]


def test_operator_profile_migration_preserves_duplicate_legacy_rows_on_failure(
    clean_postgres_schema_url: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            for migration in load_kernel_migrations():
                if migration.version >= 38:
                    break
                cur.execute(migration.sql)
            cur.execute(
                "ALTER TABLE operator_profiles DROP CONSTRAINT operator_profiles_pkey"
            )
            cur.execute(
                """
                INSERT INTO operator_profiles (
                    operator_id,
                    principal_id,
                    display_name
                ) VALUES
                    ('duplicate-operator', 'duplicate-principal', 'First'),
                    ('duplicate-operator', 'duplicate-principal', 'Second')
                """
            )

    with pytest.raises(
        psycopg.errors.RaiseException,
        match="operator profile principal identity duplicates",
    ):
        migrate_kernel_schema(
            clean_postgres_schema_url,
            applied_by="integration-test-duplicate-failure",
        )

    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT display_name
                FROM operator_profiles
                WHERE principal_id = 'duplicate-principal'
                  AND operator_id = 'duplicate-operator'
                ORDER BY display_name
                """
            )
            assert cur.fetchall() == [("First",), ("Second",)]
            cur.execute("SELECT to_regclass('ea_kernel_schema_migrations')")
            assert cur.fetchone() == (None,)


def test_operator_profile_migration_rolls_back_on_index_name_conflict_real_postgres(
    clean_postgres_schema_url: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            for migration in load_kernel_migrations():
                if migration.version >= 38:
                    break
                cur.execute(migration.sql)
            cur.execute(
                """
                CREATE INDEX idx_operator_profiles_principal_operator
                ON operator_profiles(display_name)
                """
            )

    with pytest.raises(
        psycopg.errors.RaiseException,
        match="operator profile principal identity index conflict",
    ):
        migrate_kernel_schema(
            clean_postgres_schema_url,
            applied_by="integration-test-name-conflict",
        )

    with psycopg.connect(clean_postgres_schema_url, autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT constraint_row.contype
                FROM pg_catalog.pg_constraint AS constraint_row
                WHERE constraint_row.conrelid =
                      'operator_profiles'::pg_catalog.regclass
                  AND constraint_row.conname = 'operator_profiles_pkey'
                """
            )
            assert cur.fetchone() == ("p",)
            cur.execute(
                """
                SELECT pg_catalog.pg_get_indexdef(index_row.indexrelid, 1, TRUE)
                FROM pg_catalog.pg_index AS index_row
                JOIN pg_catalog.pg_class AS index_relation
                  ON index_relation.oid = index_row.indexrelid
                WHERE index_row.indrelid = 'operator_profiles'::pg_catalog.regclass
                  AND index_relation.relname =
                      'idx_operator_profiles_principal_operator'
                """
            )
            assert cur.fetchone() == ("display_name",)
