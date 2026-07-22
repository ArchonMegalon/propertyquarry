#!/usr/bin/env python3
"""Provision and fence PropertyQuarry's primary PostgreSQL runtime roles.

The release controller supplies the generated mode-0600 env file to Compose;
Compose then maps each selector to one service and explicitly clears the other
database credentials.  This tool never prints credentials and never places a
password in an argv entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence
from urllib.parse import parse_qs, quote, unquote, urlsplit


TARGET_DATABASE = "propertyquarry"
LEGACY_DATABASE = "postgres"
DATABASE_HOST = "propertyquarry-db"
OWNER_ROLE = "propertyquarry_owner"
ADMISSION_CAPACITY_OWNER_ROLE = "propertyquarry_admission_capacity_owner"
ADMISSION_DATABASE = "propertyquarry_admission"
ADMISSION_RUNTIME_ROLE = "propertyquarry_admission_runtime"
MIGRATOR_ROLE = "propertyquarry_migrator"
API_ROLE = "propertyquarry_api"
WORKER_ROLE = "propertyquarry_worker"
SCHEDULER_ROLE = "propertyquarry_scheduler"
RUNTIME_ROLES = (API_ROLE, WORKER_ROLE, SCHEDULER_ROLE)
GOOGLE_IDENTITY_API_TABLE_GRANTS = (
    (
        "SELECT, INSERT, UPDATE",
        (
            "propertyquarry_google_identity_accounts",
            "propertyquarry_google_identity_sessions",
        ),
    ),
    ("INSERT", ("propertyquarry_google_identity_audit",)),
    (
        "SELECT, INSERT, DELETE",
        ("propertyquarry_google_identity_consumed_states",),
    ),
)
MIGRATION_TABLES = (
    "ea_kernel_schema_migrations",
    "propertyquarry_schema_migrations",
)
ADMISSION_CAPACITY_FUNCTIONS = (
    "propertyquarry_admission_capacity_after_insert",
    "propertyquarry_admission_capacity_after_delete",
    "propertyquarry_admission_capacity_after_truncate",
)
ADMISSION_CAPACITY_TABLES = (
    "propertyquarry_admission_quota_buckets",
    "propertyquarry_admission_leases",
    "propertyquarry_admission_capacity_state",
)
V0_37_RUNTIME_REPOSITORY_TABLES = (
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
)
V0_37_API_TABLE_GRANTS = (
    (
        "SELECT, INSERT, UPDATE, DELETE",
        (
            "onboarding_states",
            "person_profiles",
            "preference_nodes",
            "onemin_accounts",
            "onemin_credentials",
            "property_packet_publications",
            "property_packet_publication_events",
        ),
    ),
    (
        "SELECT, INSERT, UPDATE",
        (
            "evidence_objects",
            "onemin_allocation_leases",
            "property_decision_ledger",
            "property_evidence_claims",
            "property_agent_question_tasks",
            "property_documents",
            "response_records",
        ),
    ),
    (
        "SELECT, INSERT, DELETE",
        (
            "preference_evidence_events",
            "preference_decision_assessments",
            "preference_profile_corrections",
        ),
    ),
    ("SELECT", ("property_packet_schema_versions",)),
)
V0_37_WORKER_TABLE_GRANTS = (
    (
        "SELECT, INSERT, UPDATE",
        (
            "evidence_objects",
            "preference_evidence_events",
            "preference_decision_assessments",
            "property_evidence_claims",
            "property_agent_question_tasks",
            "property_documents",
        ),
    ),
    ("SELECT, INSERT", ("property_decision_ledger",)),
    (
        "SELECT",
        (
            "onboarding_states",
            "person_profiles",
            "preference_nodes",
        ),
    ),
)
PRE_V0_37_RUNTIME_REPOSITORY_TABLES = (
    "approval_decisions",
    "approval_requests",
    "artifacts",
    "authority_bindings",
    "commitments",
    "communication_policies",
    "connector_bindings",
    "deadline_windows",
    "decision_windows",
    "delivery_outbox",
    "delivery_preferences",
    "entities",
    "execution_events",
    "execution_queue",
    "execution_sessions",
    "execution_steps",
    "follow_up_rules",
    "follow_ups",
    "human_tasks",
    "interruption_budgets",
    "memory_candidates",
    "memory_items",
    "observation_events",
    "operator_profiles",
    "policy_decisions",
    "provider_bindings",
    "relationships",
    "run_costs",
    "stakeholders",
    "task_contracts",
    "tool_receipts",
    "tool_registry",
)
KERNEL_RUNTIME_REPOSITORY_TABLES = (
    *PRE_V0_37_RUNTIME_REPOSITORY_TABLES,
    *V0_37_RUNTIME_REPOSITORY_TABLES,
)
KERNEL_NO_RUNTIME_GRANT_TABLES = (
    "attachments",
    "channel_accounts",
    "channel_bindings",
    "channel_checkpoints",
    "channel_health_events",
    "channel_scope_grants",
    "consent_bundles",
    "consent_events",
    "conversation_participants",
    "conversations",
    "history_import_chunks",
    "history_import_jobs",
    "identity_accounts",
    "import_verification_events",
    "message_parts",
    "message_source_receipts",
    "messages",
    "oauth_refresh_token_refs",
    "principals",
    "propertyquarry_listing_instances",
    "propertyquarry_property_claims",
    "propertyquarry_property_entities",
    "propertyquarry_property_events",
    "sync_cursors",
    "tenants",
)
PRE_V0_37_API_TABLE_GRANTS = (
    (
        "SELECT, INSERT, UPDATE",
        (
            "approval_requests",
            "artifacts",
            "authority_bindings",
            "commitments",
            "communication_policies",
            "connector_bindings",
            "deadline_windows",
            "decision_windows",
            "delivery_preferences",
            "entities",
            "execution_queue",
            "execution_sessions",
            "execution_steps",
            "follow_up_rules",
            "follow_ups",
            "human_tasks",
            "interruption_budgets",
            "memory_candidates",
            "operator_profiles",
            "relationships",
            "stakeholders",
            "task_contracts",
            "tool_registry",
        ),
    ),
    (
        "SELECT, INSERT",
        (
            "approval_decisions",
            "execution_events",
            "memory_items",
            "policy_decisions",
            "run_costs",
            "tool_receipts",
        ),
    ),
    ("SELECT, INSERT, DELETE", ("observation_events",)),
    (
        "SELECT, INSERT, UPDATE, DELETE",
        ("delivery_outbox", "provider_bindings"),
    ),
)
PRE_V0_37_WORKER_TABLE_GRANTS = (
    (
        "SELECT, INSERT",
        (
            "execution_events",
            "execution_sessions",
            "human_tasks",
            "observation_events",
        ),
    ),
    ("SELECT", ("operator_profiles", "provider_bindings")),
    ("SELECT, INSERT, UPDATE", ("tool_registry",)),
)
PRE_V0_37_SCHEDULER_TABLE_GRANTS = (
    (
        "SELECT, INSERT",
        ("execution_events", "execution_sessions", "observation_events"),
    ),
    ("SELECT, INSERT, UPDATE", ("human_tasks",)),
    ("SELECT", ("operator_profiles", "provider_bindings")),
    ("SELECT, INSERT, UPDATE", ("tool_registry",)),
)
KERNEL_ROLE_TABLE_GRANTS = {
    API_ROLE: (*PRE_V0_37_API_TABLE_GRANTS, *V0_37_API_TABLE_GRANTS),
    WORKER_ROLE: (*PRE_V0_37_WORKER_TABLE_GRANTS, *V0_37_WORKER_TABLE_GRANTS),
    SCHEDULER_ROLE: PRE_V0_37_SCHEDULER_TABLE_GRANTS,
}
ACTIVATION_SENTINEL_TABLE = "propertyquarry_runtime_database_activation"
ACTIVATION_SENTINEL_VERSION = 1
ACTIVATION_SENTINEL_DIGEST = hashlib.sha256(
    b"propertyquarry:runtime-database-activation:v1"
).hexdigest()
DEFAULT_DATABASE_CONTAINER = "propertyquarry-db-live"
DEFAULT_ENV_FILE = Path("state/runtime/propertyquarry_database_roles.env")
DEFAULT_ADMISSION_ENV_FILE = Path("state/runtime/propertyquarry_admission.env")
ERASURE_KEY = "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
ROLE_KEYS = {
    "PROPERTYQUARRY_API_DATABASE_URL": API_ROLE,
    "PROPERTYQUARRY_WORKER_DATABASE_URL": WORKER_ROLE,
    "PROPERTYQUARRY_SCHEDULER_DATABASE_URL": SCHEDULER_ROLE,
    "PROPERTYQUARRY_MIGRATION_DATABASE_URL": MIGRATOR_ROLE,
}
ADMISSION_KEY = "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
RENDER_KEY = "PROPERTYQUARRY_RENDER_DATABASE_URL"
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{48,128}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RuntimeDatabaseError(RuntimeError):
    """The primary database authority contract could not be established."""


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise RuntimeDatabaseError("sql_literal_contains_nul")
    return "'" + value.replace("'", "''") + "'"


def _redact(value: str, hidden: Sequence[str]) -> str:
    result = str(value or "")
    for secret in hidden:
        if secret:
            result = result.replace(secret, "***")
            result = result.replace(quote(secret, safe=""), "***")
    return result


def _validated_cli_path(
    value: str | os.PathLike[str],
    *,
    label: str,
) -> Path:
    """Return an absolute lexical path only after rejecting symlink components."""
    path = Path(os.path.abspath(os.fspath(value)))
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeDatabaseError(f"{label}_path_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeDatabaseError(f"{label}_path_symlink")
    return path


def _run(
    argv: Sequence[str],
    *,
    stdin: str,
    hidden: Sequence[str],
    timeout_seconds: int = 120,
) -> str:
    try:
        result = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={
                "HOME": os.environ.get("HOME", "/nonexistent"),
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeDatabaseError("docker_psql_unavailable") from exc
    if result.returncode:
        detail = _redact(result.stderr.strip(), hidden)[-2000:]
        raise RuntimeDatabaseError(f"docker_psql_failed:{result.returncode}:{detail}")
    return result.stdout.strip()


def _psql(
    *,
    container: str,
    database: str,
    sql: str,
    hidden: Sequence[str],
) -> str:
    return _run(
        (
            "/usr/bin/docker",
            "exec",
            "-i",
            container,
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--username=postgres",
            f"--dbname={database}",
            "--tuples-only",
            "--no-align",
            "--quiet",
        ),
        stdin=sql,
        hidden=hidden,
    )


def _password() -> str:
    value = secrets.token_urlsafe(48)
    if not PASSWORD_RE.fullmatch(value):  # pragma: no cover
        raise RuntimeDatabaseError("generated_password_invalid")
    return value


def _runtime_url(*, role: str, password: str) -> str:
    return (
        f"postgresql://{role}:{quote(password, safe='')}@{DATABASE_HOST}:5432/"
        f"{TARGET_DATABASE}"
    )


def _migrator_url(*, password: str) -> str:
    options = f"-c role={OWNER_ROLE} -c search_path=public,pg_catalog"
    query = "options=" + quote(
        options,
        safe="",
    )
    return f"{_runtime_url(role=MIGRATOR_ROLE, password=password)}?{query}"


def _admission_url(*, password: str) -> str:
    return (
        f"postgresql://{ADMISSION_RUNTIME_ROLE}:{quote(password, safe='')}@"
        f"{DATABASE_HOST}:5432/{ADMISSION_DATABASE}"
    )


def _parse_admission_url(value: str) -> str:
    parsed = urlsplit(value)
    password = unquote(parsed.password or "")
    if (
        parsed.scheme != "postgresql"
        or parsed.username != ADMISSION_RUNTIME_ROLE
        or parsed.hostname != DATABASE_HOST
        or parsed.port != 5432
        or parsed.path != f"/{ADMISSION_DATABASE}"
        or parsed.query
        or parsed.fragment
        or not PASSWORD_RE.fullmatch(password)
    ):
        raise RuntimeDatabaseError("admission_database_url_invalid")
    if value != _admission_url(password=password):
        raise RuntimeDatabaseError("admission_database_url_noncanonical")
    return password


def _read_regular_env(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeDatabaseError(f"env_unavailable:{path.name}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeDatabaseError(f"env_metadata_invalid:{path.name}")
    raw = path.read_bytes()
    if len(raw) > 32768 or b"\x00" in raw:
        raise RuntimeDatabaseError(f"env_size_invalid:{path.name}")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeDatabaseError(f"env_encoding_invalid:{path.name}") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeDatabaseError(f"env_assignment_invalid:{path.name}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) or key in result:
            raise RuntimeDatabaseError(f"env_key_invalid:{path.name}")
        if not value or "\r" in value or "\n" in value:
            raise RuntimeDatabaseError(f"env_value_invalid:{path.name}:{key}")
        result[key] = value
    return result


def _parse_database_url(
    value: str,
    *,
    expected_role: str,
    migrator: bool = False,
) -> str:
    parsed = urlsplit(value)
    password = unquote(parsed.password or "")
    expected_query: Mapping[str, list[str]]
    if migrator:
        expected_query = {
            "options": [f"-c role={OWNER_ROLE} -c search_path=public,pg_catalog"]
        }
    else:
        expected_query = {}
    if (
        parsed.scheme != "postgresql"
        or parsed.username != expected_role
        or parsed.hostname != DATABASE_HOST
        or parsed.port != 5432
        or parsed.path != f"/{TARGET_DATABASE}"
        or parsed.fragment
        or parse_qs(parsed.query, keep_blank_values=True) != expected_query
        or not PASSWORD_RE.fullmatch(password)
    ):
        raise RuntimeDatabaseError(f"runtime_database_url_invalid:{expected_role}")
    canonical = (
        _migrator_url(password=password)
        if migrator
        else _runtime_url(role=expected_role, password=password)
    )
    if canonical != value and not migrator:
        raise RuntimeDatabaseError(f"runtime_database_url_noncanonical:{expected_role}")
    return password


def _load_or_create_values(
    *,
    env_file: Path,
    admission_env_file: Path,
) -> tuple[dict[str, str], bool, bool]:
    admission = _read_regular_env(admission_env_file)
    if set(admission) != {ADMISSION_KEY}:
        raise RuntimeDatabaseError("admission_env_fields_invalid")
    admission_url = admission[ADMISSION_KEY]
    _parse_admission_url(admission_url)
    if env_file.exists() or env_file.is_symlink():
        values = _read_regular_env(env_file)
        expected = {*ROLE_KEYS, ADMISSION_KEY, RENDER_KEY, ERASURE_KEY}
        if set(values) != expected:
            raise RuntimeDatabaseError("runtime_env_fields_invalid")
        repaired = False
        for key, role in ROLE_KEYS.items():
            password = _parse_database_url(
                values[key],
                expected_role=role,
                migrator=(role == MIGRATOR_ROLE),
            )
            if role == MIGRATOR_ROLE:
                canonical = _migrator_url(password=password)
                repaired = repaired or values[key] != canonical
                values[key] = canonical
        if (
            values[ADMISSION_KEY] != admission_url
            or values[RENDER_KEY] != admission_url
            or not PASSWORD_RE.fullmatch(values[ERASURE_KEY])
        ):
            raise RuntimeDatabaseError("runtime_env_authority_mismatch")
        _parse_admission_url(values[ADMISSION_KEY])
        _parse_admission_url(values[RENDER_KEY])
        return values, True, repaired

    values: dict[str, str] = {}
    for key, role in ROLE_KEYS.items():
        password = _password()
        values[key] = (
            _migrator_url(password=password)
            if role == MIGRATOR_ROLE
            else _runtime_url(role=role, password=password)
        )
    values[ADMISSION_KEY] = admission_url
    values[RENDER_KEY] = admission_url
    values[ERASURE_KEY] = _password()
    return values, False, False


def _write_env(
    path: Path,
    values: Mapping[str, str],
    *,
    replace: bool = False,
) -> None:
    if (path.exists() or path.is_symlink()) and not replace:
        raise RuntimeDatabaseError("runtime_env_destination_exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise RuntimeDatabaseError("runtime_env_parent_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _passwords(values: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, role in ROLE_KEYS.items():
        result[role] = _parse_database_url(
            values[key],
            expected_role=role,
            migrator=(role == MIGRATOR_ROLE),
        )
    return result


def _role_authority_guard_sql() -> str:
    return f"""
DO $propertyquarry_runtime_role_guard$
DECLARE
    role_row pg_catalog.pg_roles%ROWTYPE;
BEGIN
    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{OWNER_ROLE}';
    IF NOT FOUND
       OR role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            WHERE membership.member = role_row.oid
              AND granted_role.rolname = '{ADMISSION_CAPACITY_OWNER_ROLE}'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            WHERE membership.member = role_row.oid
              AND granted_role.rolname <> '{ADMISSION_CAPACITY_OWNER_ROLE}'
       ) THEN
        RAISE EXCEPTION 'unsafe propertyquarry owner role or memberships';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{MIGRATOR_ROLE}';
    IF NOT FOUND
       OR NOT role_row.rolcanlogin OR NOT role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            WHERE membership.member = role_row.oid
              AND granted_role.rolname = '{OWNER_ROLE}'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            WHERE membership.member = role_row.oid
              AND granted_role.rolname <> '{OWNER_ROLE}'
       ) THEN
        RAISE EXCEPTION 'unsafe propertyquarry migrator role or memberships';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{ADMISSION_CAPACITY_OWNER_ROLE}';
    IF NOT FOUND
       OR role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_row.oid
       ) THEN
        RAISE EXCEPTION 'unsafe propertyquarry admission capacity owner role';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{ADMISSION_RUNTIME_ROLE}';
    IF NOT FOUND
       OR NOT role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_row.oid
       ) THEN
        RAISE EXCEPTION 'unsafe propertyquarry admission runtime role';
    END IF;

    FOR role_row IN SELECT * FROM pg_catalog.pg_roles WHERE rolname IN (
        '{API_ROLE}', '{WORKER_ROLE}', '{SCHEDULER_ROLE}'
    ) LOOP
        IF NOT role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
           OR role_row.rolcreatedb OR role_row.rolcreaterole
           OR role_row.rolreplication OR role_row.rolbypassrls
           OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                WHERE membership.member = role_row.oid
           ) THEN
            RAISE EXCEPTION 'unsafe propertyquarry runtime role:%', role_row.rolname;
        END IF;
    END LOOP;
    IF (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.pg_roles
        WHERE rolname IN ('{API_ROLE}', '{WORKER_ROLE}', '{SCHEDULER_ROLE}')
    ) <> 3 THEN
        RAISE EXCEPTION 'propertyquarry runtime role missing';
    END IF;
END
$propertyquarry_runtime_role_guard$;
"""


def _prepare_roles_sql(passwords: Mapping[str, str]) -> str:
    create_rows = [
        (
            OWNER_ROLE,
            "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS",
        ),
        (
            MIGRATOR_ROLE,
            "LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS",
        ),
        *[
            (
                role,
                "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS",
            )
            for role in RUNTIME_ROLES
        ],
    ]
    create_sql = "\n".join(
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {role} {attributes}; END IF;"
        for role, attributes in create_rows
    )
    password_sql = "\n".join(
        f"ALTER ROLE {role} PASSWORD {_sql_literal(password)};"
        for role, password in passwords.items()
    )
    return f"""
BEGIN;
DO $propertyquarry_runtime_roles$
BEGIN
{create_sql}
END
$propertyquarry_runtime_roles$;
GRANT {OWNER_ROLE} TO {MIGRATOR_ROLE};
GRANT {ADMISSION_CAPACITY_OWNER_ROLE} TO {OWNER_ROLE};
{password_sql}
{_role_authority_guard_sql()}
COMMIT;
"""


def _database_state_sql() -> str:
    return f"""
SELECT COALESCE(
           (SELECT oid::TEXT FROM pg_catalog.pg_database
            WHERE datname = '{LEGACY_DATABASE}'),
           ''
       ) || '|' || COALESCE(
           (SELECT oid::TEXT FROM pg_catalog.pg_database
            WHERE datname = '{TARGET_DATABASE}'),
           ''
       );
"""


def _database_oids(
    *,
    container: str,
    hidden: Sequence[str],
) -> tuple[int | None, int | None]:
    output = _psql(
        container=container,
        database="template1",
        sql=_database_state_sql(),
        hidden=hidden,
    )
    line = output.splitlines()[-1].strip() if output.splitlines() else ""
    fields = line.split("|")
    if len(fields) != 2:
        raise RuntimeDatabaseError("database_state_invalid")

    def parse_oid(value: str) -> int | None:
        if not value:
            return None
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeDatabaseError("database_state_invalid")
        return int(value)

    legacy_oid, target_oid = (parse_oid(value) for value in fields)
    if legacy_oid is None and target_oid is None:
        raise RuntimeDatabaseError("propertyquarry_database_missing")
    return legacy_oid, target_oid


def _sentinel_sql(*, expected_oid: int, install: bool) -> str:
    if (
        isinstance(expected_oid, bool)
        or not isinstance(expected_oid, int)
        or expected_oid <= 0
    ):
        raise RuntimeDatabaseError("activation_sentinel_database_oid_invalid")
    install_sql = ""
    if install:
        install_sql = f"""
DO $propertyquarry_activation_source_guard$
BEGIN
    IF pg_catalog.current_database() = '{LEGACY_DATABASE}'
       AND pg_catalog.to_regclass('public.property_search_runs') IS NULL THEN
        RAISE EXCEPTION 'legacy database is not a PropertyQuarry database';
    END IF;
END
$propertyquarry_activation_source_guard$;
CREATE TABLE IF NOT EXISTS public.{ACTIVATION_SENTINEL_TABLE} (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    contract_version INTEGER NOT NULL,
    database_oid OID NOT NULL,
    legacy_database TEXT NOT NULL,
    target_database TEXT NOT NULL,
    contract_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.statement_timestamp()
);
INSERT INTO public.{ACTIVATION_SENTINEL_TABLE} (
    singleton,
    contract_version,
    database_oid,
    legacy_database,
    target_database,
    contract_sha256
) VALUES (
    TRUE,
    {ACTIVATION_SENTINEL_VERSION},
    {expected_oid},
    '{LEGACY_DATABASE}',
    '{TARGET_DATABASE}',
    '{ACTIVATION_SENTINEL_DIGEST}'
)
ON CONFLICT (singleton) DO NOTHING;
ALTER TABLE public.{ACTIVATION_SENTINEL_TABLE} OWNER TO {OWNER_ROLE};
REVOKE ALL PRIVILEGES ON TABLE public.{ACTIVATION_SENTINEL_TABLE}
    FROM PUBLIC, {API_ROLE}, {WORKER_ROLE}, {SCHEDULER_ROLE};
"""
    return f"""
BEGIN;
DO $propertyquarry_activation_database_guard$
DECLARE
    observed_oid OID;
BEGIN
    SELECT database_row.oid
    INTO observed_oid
    FROM pg_catalog.pg_database AS database_row
    WHERE database_row.datname = pg_catalog.current_database();
    IF observed_oid IS DISTINCT FROM {expected_oid}::OID
       OR pg_catalog.current_database() NOT IN (
           '{LEGACY_DATABASE}', '{TARGET_DATABASE}'
       ) THEN
        RAISE EXCEPTION 'activation database identity drift';
    END IF;
END
$propertyquarry_activation_database_guard$;
{install_sql}
DO $propertyquarry_activation_sentinel_guard$
DECLARE
    sentinel_valid BOOLEAN := FALSE;
    sentinel_owner TEXT;
BEGIN
    IF pg_catalog.to_regclass(
        'public.{ACTIVATION_SENTINEL_TABLE}'
    ) IS NULL THEN
        RAISE EXCEPTION 'activation sentinel missing';
    END IF;
    EXECUTE $sentinel_probe$
        SELECT pg_catalog.count(*) = 1
           AND pg_catalog.bool_and(
               singleton
               AND contract_version = {ACTIVATION_SENTINEL_VERSION}
               AND database_oid = {expected_oid}::OID
               AND legacy_database = '{LEGACY_DATABASE}'
               AND target_database = '{TARGET_DATABASE}'
               AND contract_sha256 = '{ACTIVATION_SENTINEL_DIGEST}'
           )
        FROM public.{ACTIVATION_SENTINEL_TABLE}
    $sentinel_probe$
    INTO sentinel_valid;
    SELECT owner_role.rolname
    INTO sentinel_owner
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace_row
      ON namespace_row.oid = relation.relnamespace
    JOIN pg_catalog.pg_roles AS owner_role
      ON owner_role.oid = relation.relowner
    WHERE namespace_row.nspname = 'public'
      AND relation.relname = '{ACTIVATION_SENTINEL_TABLE}'
      AND relation.relkind IN ('r', 'p');
    IF NOT COALESCE(sentinel_valid, FALSE)
       OR sentinel_owner IS DISTINCT FROM '{OWNER_ROLE}' THEN
        RAISE EXCEPTION 'activation sentinel invalid';
    END IF;
END
$propertyquarry_activation_sentinel_guard$;
COMMIT;
SELECT 'activation-sentinel-ok';
"""


def _check_sentinel(
    *,
    container: str,
    database: str,
    database_oid: int,
    hidden: Sequence[str],
    install: bool,
) -> None:
    result = _psql(
        container=container,
        database=database,
        sql=_sentinel_sql(expected_oid=database_oid, install=install),
        hidden=hidden,
    )
    if (
        not result.splitlines()
        or result.splitlines()[-1].strip() != "activation-sentinel-ok"
    ):
        raise RuntimeDatabaseError("activation_sentinel_probe_invalid")


def _rename_sql(*, source: str, target: str, expected_oid: int) -> str:
    if (
        source not in {LEGACY_DATABASE, TARGET_DATABASE}
        or target
        not in {
            LEGACY_DATABASE,
            TARGET_DATABASE,
        }
        or source == target
        or isinstance(expected_oid, bool)
        or expected_oid <= 0
    ):
        raise RuntimeDatabaseError("database_rename_invalid")
    return f"""
DO $propertyquarry_database_rename_guard$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_database
        WHERE datname = '{source}' AND oid = {expected_oid}::OID
    ) THEN
        RAISE EXCEPTION 'database rename source identity mismatch';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{target}') THEN
        RAISE EXCEPTION 'database rename target exists';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_prepared_xacts
        WHERE database = '{source}'
    ) THEN
        RAISE EXCEPTION 'database has prepared transactions';
    END IF;
END
$propertyquarry_database_rename_guard$;
SELECT pg_catalog.pg_terminate_backend(pid)
FROM pg_catalog.pg_stat_activity
WHERE datname = '{source}' AND pid <> pg_backend_pid();
ALTER DATABASE {source} RENAME TO {target};
"""


def _runtime_acl_sql() -> str:
    table_grants = (
        (
            API_ROLE,
            "SELECT",
            (
                "propertyquarry_schema_migrations",
                "property_evidence_overlay_rollups",
                "property_evidence_overlay_snapshots",
                "property_evidence_overlay_active_snapshot",
                "property_research_packet_index_state",
            ),
        ),
        (
            API_ROLE,
            "SELECT, INSERT, UPDATE, DELETE",
            (
                "property_search_runs",
                "property_source_listing_cache",
                "property_content_jobs",
                "property_content_webhook_events",
                "property_research_packet_links",
                "property_research_packet_run_memberships",
                "property_account_privacy_requests",
            ),
        ),
        (API_ROLE, "SELECT, INSERT, DELETE", ("property_search_work_jobs",)),
        (API_ROLE, "SELECT, INSERT", ("property_content_job_events",)),
        (API_ROLE, "SELECT, INSERT", ("property_search_erasure_key_state",)),
        (
            API_ROLE,
            "SELECT, INSERT, DELETE",
            ("property_search_erasure_fences",),
        ),
        (
            WORKER_ROLE,
            "SELECT",
            (
                "propertyquarry_schema_migrations",
                "property_content_jobs",
                "property_content_webhook_events",
                "property_evidence_overlay_rollups",
                "property_evidence_overlay_snapshots",
                "property_evidence_overlay_active_snapshot",
                "property_research_packet_index_state",
                "property_search_erasure_fences",
                "property_account_privacy_requests",
            ),
        ),
        (
            WORKER_ROLE,
            "SELECT, INSERT, UPDATE, DELETE",
            (
                "property_search_runs",
                "property_source_listing_cache",
                "property_research_packet_links",
                "property_research_packet_run_memberships",
            ),
        ),
        (
            WORKER_ROLE,
            "SELECT, UPDATE, DELETE",
            ("property_search_work_jobs",),
        ),
        (WORKER_ROLE, "SELECT, INSERT", ("property_content_job_events",)),
        (WORKER_ROLE, "SELECT, INSERT", ("property_search_erasure_key_state",)),
        (WORKER_ROLE, "SELECT, UPDATE", ("property_account_privacy_requests",)),
        (
            SCHEDULER_ROLE,
            "SELECT",
            (
                "propertyquarry_schema_migrations",
                "property_search_work_jobs",
                "property_research_packet_index_state",
                "property_search_erasure_fences",
            ),
        ),
        (
            SCHEDULER_ROLE,
            "SELECT, INSERT, UPDATE, DELETE",
            (
                "property_content_jobs",
                "property_content_webhook_events",
                "property_evidence_overlay_rollups",
                "property_evidence_overlay_snapshots",
                "property_evidence_overlay_active_snapshot",
            ),
        ),
        (
            SCHEDULER_ROLE,
            "SELECT, UPDATE, DELETE",
            ("property_search_runs", "property_source_listing_cache"),
        ),
        (
            SCHEDULER_ROLE,
            "SELECT, DELETE",
            (
                "property_research_packet_links",
                "property_research_packet_run_memberships",
            ),
        ),
        (SCHEDULER_ROLE, "SELECT, INSERT", ("property_content_job_events",)),
        (
            SCHEDULER_ROLE,
            "SELECT, INSERT",
            ("property_search_erasure_key_state",),
        ),
        *(
            (API_ROLE, privileges, tables)
            for privileges, tables in GOOGLE_IDENTITY_API_TABLE_GRANTS
        ),
    )
    table_grants += tuple(
        (role, privileges, tables)
        for role, grant_groups in KERNEL_ROLE_TABLE_GRANTS.items()
        for privileges, tables in grant_groups
    )
    statements: list[str] = []
    for role, privileges, tables in table_grants:
        for table in tables:
            statements.append(
                f"""
    IF pg_catalog.to_regclass('public.{table}') IS NOT NULL THEN
        EXECUTE 'GRANT {privileges} ON TABLE public.{table} TO {role}';
    END IF;"""
            )
    for role in RUNTIME_ROLES:
        statements.append(
            f"""
    IF pg_catalog.to_regclass(
        'public.property_content_job_events_event_sequence_seq'
    ) IS NOT NULL THEN
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE '
            || 'public.property_content_job_events_event_sequence_seq TO {role}';
    END IF;"""
        )
    return (
        """
DO $propertyquarry_runtime_acl_install$
BEGIN
"""
        + "\n".join(statements)
        + """
END
$propertyquarry_runtime_acl_install$;
"""
    )


def _configure_sql() -> str:
    runtime_list = ", ".join(RUNTIME_ROLES)
    capacity_function_list = ", ".join(
        f"'{function_name}'" for function_name in ADMISSION_CAPACITY_FUNCTIONS
    )
    role_settings = "\n".join(
        f"ALTER ROLE {role} IN DATABASE {TARGET_DATABASE} "
        "SET search_path TO public, pg_catalog;"
        for role in (*RUNTIME_ROLES, MIGRATOR_ROLE)
    )
    return f"""
BEGIN;
{_role_authority_guard_sql()}
ALTER DATABASE {TARGET_DATABASE} OWNER TO {OWNER_ROLE};
REVOKE ALL PRIVILEGES ON DATABASE {TARGET_DATABASE} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE {TARGET_DATABASE} FROM {runtime_list};
GRANT CONNECT ON DATABASE {TARGET_DATABASE} TO {runtime_list}, {MIGRATOR_ROLE};
{role_settings}
ALTER SCHEMA public OWNER TO {OWNER_ROLE};
REVOKE CREATE ON SCHEMA public FROM PUBLIC, {runtime_list};
GRANT USAGE ON SCHEMA public TO {runtime_list};

DO $propertyquarry_transfer_relations$
DECLARE relation_row RECORD;
BEGIN
    FOR relation_row IN
        SELECT namespace_row.nspname, relation.relname, relation.relkind
        FROM pg_class relation
        JOIN pg_namespace namespace_row ON namespace_row.oid = relation.relnamespace
        JOIN pg_roles owner_row ON owner_row.oid = relation.relowner
        WHERE namespace_row.nspname = 'public'
          AND owner_row.rolname <> '{OWNER_ROLE}'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND (
              relation.relkind <> 'S'
              OR NOT EXISTS (
                  SELECT 1
                  FROM pg_depend dependency
                  WHERE dependency.classid = 'pg_class'::regclass
                    AND dependency.objid = relation.oid
                    AND dependency.deptype IN ('a', 'i')
              )
          )
        ORDER BY
            CASE WHEN relation.relkind = 'S' THEN 2 ELSE 1 END,
            relation.relname
    LOOP
        IF relation_row.relkind = 'S' THEN
            EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO {OWNER_ROLE}', relation_row.nspname, relation_row.relname);
        ELSE
            EXECUTE format('ALTER TABLE %I.%I OWNER TO {OWNER_ROLE}', relation_row.nspname, relation_row.relname);
        END IF;
    END LOOP;
END
$propertyquarry_transfer_relations$;

DO $propertyquarry_transfer_functions$
DECLARE function_row RECORD;
BEGIN
    FOR function_row IN
        SELECT namespace_row.nspname,
               routine_row.proname,
               pg_get_function_identity_arguments(routine_row.oid) AS arguments
        FROM pg_proc routine_row
        JOIN pg_namespace namespace_row ON namespace_row.oid = routine_row.pronamespace
        JOIN pg_roles owner_row ON owner_row.oid = routine_row.proowner
        WHERE namespace_row.nspname = 'public'
          AND owner_row.rolname <> '{OWNER_ROLE}'
          AND routine_row.prokind = 'f'
          AND NOT (
              routine_row.proname IN ({capacity_function_list})
              AND pg_get_function_identity_arguments(routine_row.oid) = ''
          )
    LOOP
        EXECUTE format(
            'ALTER FUNCTION %I.%I(%s) OWNER TO {OWNER_ROLE}',
            function_row.nspname,
            function_row.proname,
            function_row.arguments
        );
    END LOOP;
END
$propertyquarry_transfer_functions$;

DO $propertyquarry_capacity_function_owner_guard$
DECLARE
    observed_count INTEGER;
    invalid_count INTEGER;
    function_name TEXT;
BEGIN
    SELECT pg_catalog.count(*),
           pg_catalog.count(*) FILTER (
               WHERE owner_role.rolname <> '{ADMISSION_CAPACITY_OWNER_ROLE}'
                  OR NOT routine_row.prosecdef
           )
    INTO observed_count, invalid_count
    FROM pg_catalog.pg_proc AS routine_row
    JOIN pg_catalog.pg_namespace AS namespace_row
      ON namespace_row.oid = routine_row.pronamespace
    JOIN pg_catalog.pg_roles AS owner_role
      ON owner_role.oid = routine_row.proowner
    WHERE namespace_row.nspname = 'public'
      AND routine_row.proname IN ({capacity_function_list})
      AND pg_catalog.pg_get_function_identity_arguments(routine_row.oid) = '';
    IF observed_count NOT IN (0, {len(ADMISSION_CAPACITY_FUNCTIONS)})
       OR invalid_count <> 0 THEN
        RAISE EXCEPTION 'propertyquarry admission capacity function authority drift';
    END IF;
    IF observed_count = {len(ADMISSION_CAPACITY_FUNCTIONS)} THEN
        FOREACH function_name IN ARRAY ARRAY[{capacity_function_list}]::TEXT[]
        LOOP
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON FUNCTION public.%I() '
                'FROM PUBLIC, {API_ROLE}, {WORKER_ROLE}, {SCHEDULER_ROLE}',
                function_name
            );
        END LOOP;
    END IF;
END
$propertyquarry_capacity_function_owner_guard$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
    FROM PUBLIC, {runtime_list};
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
    FROM PUBLIC, {runtime_list};
ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, {runtime_list};
ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, {runtime_list};
{_runtime_acl_sql()}

DO $propertyquarry_runtime_ownership_guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class relation
        JOIN pg_namespace namespace_row ON namespace_row.oid = relation.relnamespace
        JOIN pg_roles owner_row ON owner_row.oid = relation.relowner
        WHERE namespace_row.nspname = 'public'
          AND owner_row.rolname IN ('{API_ROLE}', '{WORKER_ROLE}', '{SCHEDULER_ROLE}')
    ) THEN
        RAISE EXCEPTION 'runtime role owns a public relation';
    END IF;
END
$propertyquarry_runtime_ownership_guard$;
COMMIT;
"""


def _write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(
            descriptor,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _receipt(*, operation: str, env_file: Path, reused: bool) -> dict[str, object]:
    return {
        "schema": "propertyquarry.runtime_database.provision_receipt.v1",
        "status": "pass",
        "operation": operation,
        "database": TARGET_DATABASE,
        "legacy_database": LEGACY_DATABASE,
        "owner_role": OWNER_ROLE,
        "migrator_role": MIGRATOR_ROLE,
        "runtime_roles": list(RUNTIME_ROLES),
        "runtime_acl_profile": "propertyquarry-role-specific-v3",
        "activation_sentinel_table": ACTIVATION_SENTINEL_TABLE,
        "activation_sentinel_contract_sha256": ACTIVATION_SENTINEL_DIGEST,
        "rename_recovery": "database-oid-bound-resumable-v1",
        "env_file": str(env_file),
        "env_file_sha256": hashlib.sha256(env_file.read_bytes()).hexdigest(),
        "credential_reused": reused,
        "secret_values_emitted": False,
    }


def execute(args: argparse.Namespace) -> dict[str, object]:
    container = str(args.database_container)
    if not NAME_RE.fullmatch(container):
        raise RuntimeDatabaseError("database_container_invalid")
    env_file = _validated_cli_path(args.env_file, label="runtime_env")
    admission_env = _validated_cli_path(
        args.admission_env_file,
        label="admission_env",
    )
    receipt_file = _validated_cli_path(args.receipt, label="receipt")
    values, reused, needs_repair = _load_or_create_values(
        env_file=env_file,
        admission_env_file=admission_env,
    )
    passwords = _passwords(values)
    admission_password = _parse_admission_url(values[ADMISSION_KEY])
    hidden = (
        *passwords.values(),
        admission_password,
        values[ERASURE_KEY],
        values[ADMISSION_KEY],
    )

    if args.operation == "prepare":
        _psql(
            container=container,
            database="template1",
            sql=_prepare_roles_sql(passwords),
            hidden=hidden,
        )
        legacy_oid, target_oid = _database_oids(container=container, hidden=hidden)
        active_database = TARGET_DATABASE if target_oid is not None else LEGACY_DATABASE
        active_oid = target_oid if target_oid is not None else legacy_oid
        if active_oid is None:  # pragma: no cover - _database_oids rejects this state
            raise RuntimeDatabaseError("propertyquarry_database_missing")
        _check_sentinel(
            container=container,
            database=active_database,
            database_oid=active_oid,
            hidden=hidden,
            install=True,
        )
        if not reused or needs_repair:
            _write_env(env_file, values, replace=needs_repair)
    else:
        if not reused:
            raise RuntimeDatabaseError("runtime_env_must_be_prepared_before_activation")
        _psql(
            container=container,
            database="template1",
            sql=_role_authority_guard_sql(),
            hidden=hidden,
        )
        legacy_oid, target_oid = _database_oids(container=container, hidden=hidden)
        if args.operation in {"activate", "rename-forward"}:
            if target_oid is not None:
                _check_sentinel(
                    container=container,
                    database=TARGET_DATABASE,
                    database_oid=target_oid,
                    hidden=hidden,
                    install=False,
                )
            elif legacy_oid is not None:
                _check_sentinel(
                    container=container,
                    database=LEGACY_DATABASE,
                    database_oid=legacy_oid,
                    hidden=hidden,
                    install=False,
                )
                _psql(
                    container=container,
                    database="template1",
                    sql=_rename_sql(
                        source=LEGACY_DATABASE,
                        target=TARGET_DATABASE,
                        expected_oid=legacy_oid,
                    ),
                    hidden=hidden,
                )
                _check_sentinel(
                    container=container,
                    database=TARGET_DATABASE,
                    database_oid=legacy_oid,
                    hidden=hidden,
                    install=False,
                )
            else:  # pragma: no cover - _database_oids rejects this state
                raise RuntimeDatabaseError("propertyquarry_database_missing")
            _psql(
                container=container,
                database=TARGET_DATABASE,
                sql=_configure_sql(),
                hidden=hidden,
            )
        elif args.operation == "finalize":
            if target_oid is None:
                raise RuntimeDatabaseError("target_database_missing")
            _check_sentinel(
                container=container,
                database=TARGET_DATABASE,
                database_oid=target_oid,
                hidden=hidden,
                install=False,
            )
            _psql(
                container=container,
                database=TARGET_DATABASE,
                sql=_configure_sql(),
                hidden=hidden,
            )
        elif args.operation == "rename-back":
            if target_oid is not None:
                if legacy_oid is not None:
                    raise RuntimeDatabaseError("legacy_database_already_exists")
                _check_sentinel(
                    container=container,
                    database=TARGET_DATABASE,
                    database_oid=target_oid,
                    hidden=hidden,
                    install=False,
                )
                _psql(
                    container=container,
                    database="template1",
                    sql=_rename_sql(
                        source=TARGET_DATABASE,
                        target=LEGACY_DATABASE,
                        expected_oid=target_oid,
                    ),
                    hidden=hidden,
                )
                _check_sentinel(
                    container=container,
                    database=LEGACY_DATABASE,
                    database_oid=target_oid,
                    hidden=hidden,
                    install=False,
                )
            elif legacy_oid is not None:
                _check_sentinel(
                    container=container,
                    database=LEGACY_DATABASE,
                    database_oid=legacy_oid,
                    hidden=hidden,
                    install=False,
                )
            else:  # pragma: no cover - _database_oids rejects this state
                raise RuntimeDatabaseError("propertyquarry_database_missing")
        else:  # pragma: no cover - argparse enforces choices
            raise RuntimeDatabaseError("operation_invalid")

    receipt = _receipt(operation=args.operation, env_file=env_file, reused=reused)
    _write_receipt(receipt_file, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("prepare", "activate", "rename-forward", "finalize", "rename-back"),
    )
    parser.add_argument("--database-container", default=DEFAULT_DATABASE_CONTAINER)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--admission-env-file", type=Path, default=DEFAULT_ADMISSION_ENV_FILE
    )
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        receipt = execute(build_parser().parse_args(argv))
    except RuntimeDatabaseError as exc:
        print(f"runtime database provisioning rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
