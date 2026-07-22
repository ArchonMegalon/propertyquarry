from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
from typing import Callable, Sequence


SCHEMA_COMPONENT = "propertyquarry_google_identity"
SCHEMA_LEDGER_TABLE = "propertyquarry_schema_migrations"
SCHEMA_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"propertyquarry:google_identity:migrations:v1").digest()[:8],
    byteorder="big",
    signed=True,
)
GOOGLE_IDENTITY_TABLES = (
    "propertyquarry_google_identity_accounts",
    "propertyquarry_google_identity_sessions",
    "propertyquarry_google_identity_audit",
    "propertyquarry_google_identity_consumed_states",
    "propertyquarry_registration_challenges",
)
GOOGLE_IDENTITY_API_ROLE = "propertyquarry_api"
GOOGLE_IDENTITY_API_TABLE_GRANTS = (
    (
        "propertyquarry_google_identity_accounts",
        ("SELECT", "INSERT", "UPDATE"),
    ),
    (
        "propertyquarry_google_identity_sessions",
        ("SELECT", "INSERT", "UPDATE"),
    ),
    ("propertyquarry_google_identity_audit", ("INSERT",)),
    (
        "propertyquarry_google_identity_consumed_states",
        ("SELECT", "INSERT", "DELETE"),
    ),
    (
        "propertyquarry_registration_challenges",
        ("SELECT", "INSERT", "UPDATE"),
    ),
)
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_IDENTITY_FORBIDDEN_RUNTIME_ROLES = (
    "propertyquarry_worker",
    "propertyquarry_scheduler",
)


@dataclass(frozen=True)
class PropertyQuarryGoogleIdentityMigration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        material = (
            f"{SCHEMA_COMPONENT}\0{self.version}\0{self.name}\0{self.sql.strip()}\n"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class PropertyQuarryGoogleIdentitySchemaStatus:
    ready: bool
    reason: str
    current_version: int
    required_version: int
    applied_versions: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "component": SCHEMA_COMPONENT,
            "ready": self.ready,
            "reason": self.reason,
            "current_version": self.current_version,
            "required_version": self.required_version,
            "applied_versions": list(self.applied_versions),
        }


@dataclass(frozen=True)
class PropertyQuarryGoogleIdentityMigrationResult:
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "component": SCHEMA_COMPONENT,
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "applied_versions": list(self.applied_versions),
        }


class PropertyQuarryGoogleIdentitySchemaError(RuntimeError):
    """Base failure for the privileged PropertyQuarry identity schema boundary."""


class PropertyQuarryGoogleIdentitySchemaDriftError(
    PropertyQuarryGoogleIdentitySchemaError
):
    """The immutable identity migration ledger no longer matches source."""


class PropertyQuarryGoogleIdentitySchemaNotReadyError(
    PropertyQuarryGoogleIdentitySchemaError
):
    """Runtime identity access was attempted before deploy-time migration."""


_IDENTITY_SCHEMA_V1 = r"""
CREATE TABLE IF NOT EXISTS propertyquarry_google_identity_accounts (
    principal_id TEXT PRIMARY KEY,
    subject_hash TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email_verified BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS propertyquarry_google_identity_sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS propertyquarry_google_identity_audit (
    audit_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS propertyquarry_google_identity_consumed_states (
    state_hash TEXT PRIMARY KEY,
    consumed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS propertyquarry_registration_challenges (
    email_hash CHAR(64) PRIMARY KEY,
    challenge_id TEXT UNIQUE NOT NULL,
    token_hash CHAR(64) UNIQUE NOT NULL,
    email TEXT NOT NULL,
    return_to TEXT NOT NULL,
    code_digest CHAR(64) NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    send_count INTEGER NOT NULL DEFAULT 1,
    window_started_at TIMESTAMPTZ NOT NULL,
    last_sent_at TIMESTAMPTZ NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    CHECK (attempt_count >= 0),
    CHECK (send_count > 0),
    CHECK (status IN ('active', 'consumed', 'expired', 'locked'))
);

REVOKE ALL PRIVILEGES ON TABLE
    propertyquarry_google_identity_accounts,
    propertyquarry_google_identity_sessions,
    propertyquarry_google_identity_audit,
    propertyquarry_google_identity_consumed_states,
    propertyquarry_registration_challenges
    FROM PUBLIC, propertyquarry_api, propertyquarry_worker,
         propertyquarry_scheduler;
GRANT SELECT, INSERT, UPDATE
    ON TABLE propertyquarry_google_identity_accounts
    TO propertyquarry_api;
GRANT SELECT, INSERT, UPDATE
    ON TABLE propertyquarry_google_identity_sessions
    TO propertyquarry_api;
GRANT INSERT
    ON TABLE propertyquarry_google_identity_audit
    TO propertyquarry_api;
GRANT SELECT, INSERT, DELETE
    ON TABLE propertyquarry_google_identity_consumed_states
    TO propertyquarry_api;
GRANT SELECT, INSERT, UPDATE
    ON TABLE propertyquarry_registration_challenges
    TO propertyquarry_api;
"""

GOOGLE_IDENTITY_MIGRATIONS = (
    PropertyQuarryGoogleIdentityMigration(
        1,
        "propertyquarry_local_google_identity",
        _IDENTITY_SCHEMA_V1,
    ),
)
LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION = GOOGLE_IDENTITY_MIGRATIONS[-1].version

_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_LEDGER_TABLE} (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    migration_name TEXT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (component, version),
    CHECK (version > 0),
    CHECK (checksum_sha256 ~ '^[0-9a-f]{{64}}$')
)
"""

_SCHEMA_READY_LOCK = threading.Lock()
_SCHEMA_READY_DATABASES: set[str] = set()


def _migration_by_version() -> dict[int, PropertyQuarryGoogleIdentityMigration]:
    return {migration.version: migration for migration in GOOGLE_IDENTITY_MIGRATIONS}


def _validate_applied_rows(rows: Sequence[Sequence[object]]) -> tuple[int, ...]:
    expected = _migration_by_version()
    observed: dict[int, tuple[str, str]] = {}
    for row in rows:
        version = int(row[0])
        name = str(row[1] or "")
        checksum = str(row[2] or "").strip().lower()
        if version in observed:
            raise PropertyQuarryGoogleIdentitySchemaDriftError(
                f"duplicate_propertyquarry_google_identity_migration_version:{version}"
            )
        observed[version] = (name, checksum)
    for version, (name, checksum) in sorted(observed.items()):
        migration = expected.get(version)
        if migration is None:
            raise PropertyQuarryGoogleIdentitySchemaDriftError(
                f"propertyquarry_google_identity_schema_ahead:{version}"
            )
        if name != migration.name or checksum != migration.checksum:
            raise PropertyQuarryGoogleIdentitySchemaDriftError(
                f"propertyquarry_google_identity_migration_checksum_drift:{version}"
            )
    versions = tuple(sorted(observed))
    if versions and versions != tuple(range(1, versions[-1] + 1)):
        raise PropertyQuarryGoogleIdentitySchemaDriftError(
            "propertyquarry_google_identity_migration_gap"
        )
    return versions


def _connect(database_url: str, *, autocommit: bool):  # type: ignore[no-untyped-def]
    import psycopg

    return psycopg.connect(database_url, autocommit=autocommit, connect_timeout=5)


def migrate_propertyquarry_google_identity_schema(
    database_url: str,
    *,
    applied_by: str = "deploy",
    connect: Callable[..., object] | None = None,
) -> PropertyQuarryGoogleIdentityMigrationResult:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise PropertyQuarryGoogleIdentitySchemaError("database_url_required")
    connector = connect or _connect
    conn = connector(normalized_url, autocommit=False)
    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(_LEDGER_DDL)
            cur.execute(
                f"""
                SELECT version, migration_name, checksum_sha256
                FROM {SCHEMA_LEDGER_TABLE}
                WHERE component = %s
                ORDER BY version
                """,
                (SCHEMA_COMPONENT,),
            )
            before = _validate_applied_rows(cur.fetchall())
            before_set = set(before)
            applied: list[int] = []
            for migration in GOOGLE_IDENTITY_MIGRATIONS:
                if migration.version in before_set:
                    continue
                cur.execute(migration.sql)
                cur.execute(
                    f"""
                    INSERT INTO {SCHEMA_LEDGER_TABLE}
                        (component, version, migration_name, checksum_sha256, applied_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        SCHEMA_COMPONENT,
                        migration.version,
                        migration.name,
                        migration.checksum,
                        str(applied_by or "deploy").strip()[:120] or "deploy",
                    ),
                )
                applied.append(migration.version)
        conn.commit()  # type: ignore[attr-defined]
    except Exception:
        conn.rollback()  # type: ignore[attr-defined]
        raise
    finally:
        conn.close()  # type: ignore[attr-defined]
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY_DATABASES.discard(normalized_url)
    return PropertyQuarryGoogleIdentityMigrationResult(
        previous_version=before[-1] if before else 0,
        current_version=LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
        applied_versions=tuple(applied),
    )


def inspect_propertyquarry_google_identity_schema_cursor(
    cur,  # type: ignore[no-untyped-def]
) -> PropertyQuarryGoogleIdentitySchemaStatus:
    cur.execute("SELECT to_regclass(%s)", (SCHEMA_LEDGER_TABLE,))
    ledger_row = cur.fetchone()
    if not ledger_row or ledger_row[0] is None:
        return PropertyQuarryGoogleIdentitySchemaStatus(
            False,
            "migration_ledger_missing",
            0,
            LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
            (),
        )
    cur.execute(
        f"""
        SELECT version, migration_name, checksum_sha256
        FROM {SCHEMA_LEDGER_TABLE}
        WHERE component = %s
        ORDER BY version
        """,
        (SCHEMA_COMPONENT,),
    )
    try:
        versions = _validate_applied_rows(cur.fetchall())
    except PropertyQuarryGoogleIdentitySchemaDriftError as exc:
        return PropertyQuarryGoogleIdentitySchemaStatus(
            False,
            str(exc),
            0,
            LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
            (),
        )
    current = versions[-1] if versions else 0
    if current != LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION:
        return PropertyQuarryGoogleIdentitySchemaStatus(
            False,
            "migration_pending",
            current,
            LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
            versions,
        )
    for relation in GOOGLE_IDENTITY_TABLES:
        cur.execute("SELECT to_regclass(%s)", (relation,))
        relation_row = cur.fetchone()
        if not relation_row or relation_row[0] is None:
            return PropertyQuarryGoogleIdentitySchemaStatus(
                False,
                f"required_relation_missing:{relation}",
                current,
                LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
                versions,
            )
    for relation, privileges in GOOGLE_IDENTITY_API_TABLE_GRANTS:
        allowed_api_privileges = set(privileges)
        for privilege in privileges:
            cur.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (GOOGLE_IDENTITY_API_ROLE, relation, privilege),
            )
            privilege_row = cur.fetchone()
            if not privilege_row or privilege_row[0] is not True:
                return PropertyQuarryGoogleIdentitySchemaStatus(
                    False,
                    f"required_privilege_missing:{relation}:{privilege.lower()}",
                    current,
                    LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
                    versions,
                )
        for privilege in _TABLE_PRIVILEGES:
            if privilege in allowed_api_privileges:
                continue
            cur.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (GOOGLE_IDENTITY_API_ROLE, relation, privilege),
            )
            privilege_row = cur.fetchone()
            if privilege_row and privilege_row[0] is True:
                return PropertyQuarryGoogleIdentitySchemaStatus(
                    False,
                    f"unexpected_privilege_present:{GOOGLE_IDENTITY_API_ROLE}:{relation}:{privilege.lower()}",
                    current,
                    LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
                    versions,
                )
        for role in _IDENTITY_FORBIDDEN_RUNTIME_ROLES:
            for privilege in _TABLE_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, relation, privilege),
                )
                privilege_row = cur.fetchone()
                if privilege_row and privilege_row[0] is True:
                    return PropertyQuarryGoogleIdentitySchemaStatus(
                        False,
                        f"unexpected_privilege_present:{role}:{relation}:{privilege.lower()}",
                        current,
                        LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
                        versions,
                    )
    return PropertyQuarryGoogleIdentitySchemaStatus(
        True,
        "schema_ready",
        current,
        LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
        versions,
    )


def inspect_propertyquarry_google_identity_schema(
    database_url: str,
    *,
    connect: Callable[..., object] | None = None,
) -> PropertyQuarryGoogleIdentitySchemaStatus:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        return PropertyQuarryGoogleIdentitySchemaStatus(
            False,
            "database_url_missing",
            0,
            LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
            (),
        )
    connector = connect or _connect
    try:
        conn = connector(normalized_url, autocommit=True)
        try:
            with conn.cursor() as cur:  # type: ignore[attr-defined]
                return inspect_propertyquarry_google_identity_schema_cursor(cur)
        finally:
            conn.close()  # type: ignore[attr-defined]
    except Exception as exc:
        return PropertyQuarryGoogleIdentitySchemaStatus(
            False,
            f"schema_probe_failed:{exc.__class__.__name__}",
            0,
            LATEST_GOOGLE_IDENTITY_SCHEMA_VERSION,
            (),
        )


def require_propertyquarry_google_identity_schema_ready(
    database_url: str,
    *,
    connect: Callable[..., object] | None = None,
) -> None:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise PropertyQuarryGoogleIdentitySchemaNotReadyError("database_url_missing")
    if connect is None:
        with _SCHEMA_READY_LOCK:
            if normalized_url in _SCHEMA_READY_DATABASES:
                return
    status = inspect_propertyquarry_google_identity_schema(
        normalized_url,
        connect=connect,
    )
    if not status.ready:
        raise PropertyQuarryGoogleIdentitySchemaNotReadyError(
            f"propertyquarry_google_identity_schema_not_ready:{status.reason}"
        )
    if connect is None:
        with _SCHEMA_READY_LOCK:
            _SCHEMA_READY_DATABASES.add(normalized_url)


def reset_propertyquarry_google_identity_schema_cache_for_tests() -> None:
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY_DATABASES.clear()
