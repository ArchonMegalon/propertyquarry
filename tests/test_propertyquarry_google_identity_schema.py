from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.product import propertyquarry_google_identity_schema as schema
from app.product import propertyquarry_schema
from scripts import provision_propertyquarry_runtime_database as database_roles


class _Cursor:
    def __init__(self, database: "_Database") -> None:
        self.database = database
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(str(sql).split())
        self.database.executed.append((normalized, params))
        self.rows = []
        if normalized.startswith("CREATE TABLE IF NOT EXISTS propertyquarry_schema_migrations"):
            self.database.relations.add(schema.SCHEMA_LEDGER_TABLE)
            return
        if normalized.startswith("SELECT version, migration_name, checksum_sha256"):
            component = str(params[0])
            self.rows = [
                (version, name, checksum)
                for (current_component, version), (name, checksum) in sorted(
                    self.database.ledger.items()
                )
                if current_component == component
            ]
            return
        if str(sql) == schema.GOOGLE_IDENTITY_MIGRATIONS[0].sql:
            self.database.relations.update(schema.GOOGLE_IDENTITY_TABLES)
            for relation, privileges in schema.GOOGLE_IDENTITY_API_TABLE_GRANTS:
                self.database.privileges.update(
                    (schema.GOOGLE_IDENTITY_API_ROLE, relation, privilege)
                    for privilege in privileges
                )
            return
        if normalized.startswith("INSERT INTO propertyquarry_schema_migrations"):
            component, version, name, checksum, _applied_by = params
            self.database.ledger[(str(component), int(version))] = (
                str(name),
                str(checksum),
            )
            return
        if normalized.startswith("SELECT to_regclass"):
            relation = str(params[0])
            self.rows = [
                (relation if relation in self.database.relations else None,)
            ]
            return
        if normalized.startswith("SELECT has_table_privilege"):
            role, relation, privilege = (str(value) for value in params)
            self.rows = [
                ((role, relation, privilege) in self.database.privileges,)
            ]

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, database: "_Database") -> None:
        self.database = database

    def cursor(self) -> _Cursor:
        return _Cursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1

    def rollback(self) -> None:
        self.database.rollbacks += 1

    def close(self) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.ledger: dict[tuple[str, int], tuple[str, str]] = {}
        self.relations: set[str] = set()
        self.privileges: set[tuple[str, str, str]] = set()
        self.commits = 0
        self.rollbacks = 0

    def connect(self, _database_url: str, *, autocommit: bool) -> _Connection:
        _ = autocommit
        return _Connection(self)


def test_privileged_migration_owns_identity_ddl_and_runtime_probe_is_read_only() -> None:
    database = _Database()

    result = schema.migrate_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        applied_by="release-test",
        connect=database.connect,
    )

    assert result.previous_version == 0
    assert result.current_version == schema.LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION
    assert result.applied_versions == (1,)
    assert set(schema.GOOGLE_IDENTITY_TABLES).issubset(database.relations)
    assert database.privileges == {
        (schema.GOOGLE_IDENTITY_API_ROLE, relation, privilege)
        for relation, privileges in schema.GOOGLE_IDENTITY_API_TABLE_GRANTS
        for privilege in privileges
    }
    assert database.commits == 1
    assert database.rollbacks == 0

    database.executed.clear()
    schema.require_propertyquarry_google_identity_schema_ready(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )

    assert database.executed
    assert all(sql.upper().startswith("SELECT ") for sql, _params in database.executed)
    assert not any(
        verb in sql.upper()
        for sql, _params in database.executed
        for verb in ("CREATE ", "ALTER ", "DROP ", "GRANT ", "REVOKE ")
    )


def test_runtime_readiness_fails_when_one_required_api_privilege_is_missing() -> None:
    database = _Database()
    schema.migrate_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )
    database.privileges.remove(
        (
            schema.GOOGLE_IDENTITY_API_ROLE,
            "propertyquarry_google_identity_audit",
            "INSERT",
        )
    )

    status = schema.inspect_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.reason == (
        "required_privilege_missing:propertyquarry_google_identity_audit:insert"
    )


@pytest.mark.parametrize(
    ("role", "relation", "privilege"),
    (
        (
            schema.GOOGLE_IDENTITY_API_ROLE,
            "propertyquarry_google_identity_audit",
            "SELECT",
        ),
        (
            "propertyquarry_worker",
            "propertyquarry_google_identity_accounts",
            "SELECT",
        ),
        (
            "propertyquarry_scheduler",
            "propertyquarry_google_identity_sessions",
            "DELETE",
        ),
    ),
)
def test_runtime_readiness_rejects_identity_privilege_expansion(
    role: str,
    relation: str,
    privilege: str,
) -> None:
    database = _Database()
    schema.migrate_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )
    database.privileges.add((role, relation, privilege))

    status = schema.inspect_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )

    assert status.ready is False
    assert status.reason == (
        f"unexpected_privilege_present:{role}:{relation}:{privilege.lower()}"
    )


def test_database_role_hardening_reapplies_only_the_identity_api_acl() -> None:
    acl_sql = database_roles._runtime_acl_sql()  # noqa: SLF001

    for privileges, tables in database_roles.GOOGLE_IDENTITY_API_TABLE_GRANTS:
        for table in tables:
            assert (
                f"GRANT {privileges} ON TABLE public.{table} "
                f"TO {database_roles.API_ROLE}"
            ) in acl_sql
            assert f"public.{table} TO {database_roles.WORKER_ROLE}" not in acl_sql
            assert f"public.{table} TO {database_roles.SCHEDULER_ROLE}" not in acl_sql


def test_identity_migration_replays_once_and_detects_checksum_drift() -> None:
    database = _Database()
    first = schema.migrate_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )
    second = schema.migrate_propertyquarry_google_identity_schema(
        "postgresql://test/propertyquarry",
        connect=database.connect,
    )

    assert first.applied_versions == (1,)
    assert second.previous_version == 1
    assert second.applied_versions == ()

    database.ledger[(schema.SCHEMA_COMPONENT, 1)] = (
        schema.GOOGLE_IDENTITY_MIGRATIONS[0].name,
        "0" * 64,
    )
    with pytest.raises(
        schema.PropertyQuarryGoogleIdentitySchemaDriftError,
        match="propertyquarry_google_identity_migration_checksum_drift:1",
    ):
        schema.migrate_propertyquarry_google_identity_schema(
            "postgresql://test/propertyquarry",
            connect=database.connect,
        )


@dataclass(frozen=True)
class _Result:
    component: str

    def as_dict(self) -> dict[str, object]:
        return {"component": self.component}


@dataclass(frozen=True)
class _Status:
    component: str
    ready: bool = True

    def as_dict(self) -> dict[str, object]:
        return {"component": self.component, "ready": self.ready}


def test_aggregate_propertyquarry_gate_migrates_and_requires_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_kernel_schema",
        lambda *_args, **_kwargs: events.append("migrate-kernel") or _Result("kernel"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_kernel_schema_ready",
        lambda *_args, **_kwargs: events.append("require-kernel"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_property_search_schema",
        lambda *_args, **_kwargs: events.append("migrate-search") or _Result("search"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_property_search_schema_ready",
        lambda *_args, **_kwargs: events.append("require-search"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "migrate_propertyquarry_google_identity_schema",
        lambda *_args, **_kwargs: events.append("migrate-identity") or _Result("identity"),
    )
    monkeypatch.setattr(
        propertyquarry_schema,
        "require_propertyquarry_google_identity_schema_ready",
        lambda *_args, **_kwargs: events.append("require-identity"),
    )

    result = propertyquarry_schema.migrate_propertyquarry_schema(
        "postgresql://test/propertyquarry",
        applied_by="release-test",
    )

    assert events == [
        "migrate-kernel",
        "require-kernel",
        "migrate-search",
        "require-search",
        "migrate-identity",
        "require-identity",
    ]
    assert result["google_identity"] == {"component": "identity"}
