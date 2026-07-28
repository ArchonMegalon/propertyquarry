#!/usr/bin/env python3
"""Provision PropertyQuarry's least-privilege PostgreSQL admission authority.

The API deliberately refuses production startup when request admission shares
the application database credential.  This host-side provisioner creates the
closed database/role/schema contracts exercised by both
``admission_control.py`` and ``api.ingress_admission`` and writes only a
dedicated, mode-0600 env file.  Secrets are passed to PostgreSQL over stdin and
are never printed or placed in process arguments.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from typing import Sequence
from urllib.parse import quote, unquote, urlsplit


DATABASE_NAME = "propertyquarry_admission"
SCHEMA_NAME = "propertyquarry_admission"
OWNER_ROLE = "propertyquarry_admission_owner"
CAPACITY_OWNER_ROLE = "propertyquarry_admission_capacity_owner"
RUNTIME_ROLE = "propertyquarry_admission_runtime"
INGRESS_RUNTIME_ROLE = "propertyquarry_ingress_runtime"
ENV_KEY = "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
INGRESS_ENV_KEY = "PROPERTYQUARRY_API_INGRESS_DATABASE_URL"
DEFAULT_DATABASE_CONTAINER = "propertyquarry-db-live"
DEFAULT_DATABASE_HOST = "propertyquarry-db"
DEFAULT_DOCKER_NETWORK = "property_default"
DEFAULT_ENV_FILE = Path("state/runtime/propertyquarry_admission.env")
IMAGE_RE = re.compile(
    r"^(?:[a-z0-9./_-]+@)?sha256:[0-9a-f]{64}$"
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{48,128}$")
INGRESS_QUOTA_TABLE = "propertyquarry_ingress_quota_buckets"
INGRESS_LEASE_TABLE = "propertyquarry_ingress_leases"
INGRESS_CAPACITY_TABLE = "propertyquarry_ingress_admission_capacity"
INGRESS_CAPACITY_INSERT_FUNCTION = (
    "propertyquarry_ingress_admission_capacity_after_insert"
)
INGRESS_CAPACITY_DELETE_FUNCTION = (
    "propertyquarry_ingress_admission_capacity_after_delete"
)
INGRESS_CAPACITY_TRUNCATE_FUNCTION = (
    "propertyquarry_ingress_admission_capacity_after_truncate"
)


class ProvisioningError(RuntimeError):
    """The admission authority could not be provisioned safely."""


@dataclass(frozen=True)
class RuntimeCredential:
    password: str
    database_url: str
    ingress_password: str
    ingress_database_url: str


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ProvisioningError("sql_literal_contains_nul")
    return "'" + value.replace("'", "''") + "'"


def _redact(value: str, secrets_to_hide: Sequence[str]) -> str:
    redacted = str(value or "")
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "***")
            redacted = redacted.replace(quote(secret, safe=""), "***")
    return redacted


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
            raise ProvisioningError(f"{label}_path_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ProvisioningError(f"{label}_path_symlink")
    return path


def _run(
    argv: Sequence[str],
    *,
    stdin: str = "",
    timeout_seconds: int = 60,
    secrets_to_hide: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
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
        raise ProvisioningError(f"command_unavailable:{Path(argv[0]).name}") from exc
    if result.returncode:
        detail = _redact(result.stderr.strip(), secrets_to_hide)[-2000:]
        raise ProvisioningError(
            f"command_failed:{Path(argv[0]).name}:{result.returncode}:{detail}"
        )
    return result


def _psql(
    *,
    container: str,
    database: str,
    sql: str,
    password: str,
) -> str:
    result = _run(
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
        timeout_seconds=120,
        secrets_to_hide=(password,),
    )
    return result.stdout.strip()


def _database_url(*, password: str, host: str) -> str:
    if not PASSWORD_RE.fullmatch(password):
        raise ProvisioningError("runtime_password_invalid")
    if not NAME_RE.fullmatch(host):
        raise ProvisioningError("database_host_invalid")
    return (
        f"postgresql://{RUNTIME_ROLE}:{quote(password, safe='')}@"
        f"{host}:5432/{DATABASE_NAME}"
    )


def _ingress_database_url(*, password: str, host: str) -> str:
    if not PASSWORD_RE.fullmatch(password):
        raise ProvisioningError("ingress_runtime_password_invalid")
    if not NAME_RE.fullmatch(host):
        raise ProvisioningError("database_host_invalid")
    return (
        f"postgresql://{INGRESS_RUNTIME_ROLE}:{quote(password, safe='')}@"
        f"{host}:5432/{DATABASE_NAME}"
    )


def _parse_env_file(path: Path, *, database_host: str) -> RuntimeCredential:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProvisioningError("admission_env_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProvisioningError("admission_env_must_be_regular")
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise ProvisioningError("admission_env_metadata_invalid")
    raw = path.read_bytes()
    if len(raw) > 8192 or b"\x00" in raw:
        raise ProvisioningError("admission_env_size_invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProvisioningError("admission_env_encoding_invalid") from exc
    assignments = [line for line in lines if line and not line.startswith("#")]
    values: dict[str, str] = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        if (
            separator != "="
            or key not in {ENV_KEY, INGRESS_ENV_KEY}
            or key in values
            or not value
        ):
            raise ProvisioningError("admission_env_fields_invalid")
        values[key] = value
    if set(values) not in ({ENV_KEY}, {ENV_KEY, INGRESS_ENV_KEY}):
        raise ProvisioningError("admission_env_fields_invalid")
    value = values[ENV_KEY]
    parsed = urlsplit(value)
    password = unquote(parsed.password or "")
    if (
        parsed.scheme != "postgresql"
        or parsed.username != RUNTIME_ROLE
        or parsed.hostname != database_host
        or parsed.port != 5432
        or parsed.path != f"/{DATABASE_NAME}"
        or parsed.query
        or parsed.fragment
        or not PASSWORD_RE.fullmatch(password)
    ):
        raise ProvisioningError("admission_env_database_url_invalid")
    if value != _database_url(password=password, host=database_host):
        raise ProvisioningError("admission_env_database_url_noncanonical")
    ingress_value = values.get(INGRESS_ENV_KEY, "")
    ingress_password = ""
    if ingress_value:
        ingress_parsed = urlsplit(ingress_value)
        ingress_password = unquote(ingress_parsed.password or "")
        if (
            ingress_parsed.scheme != "postgresql"
            or ingress_parsed.username != INGRESS_RUNTIME_ROLE
            or ingress_parsed.hostname != database_host
            or ingress_parsed.port != 5432
            or ingress_parsed.path != f"/{DATABASE_NAME}"
            or ingress_parsed.query
            or ingress_parsed.fragment
            or not PASSWORD_RE.fullmatch(ingress_password)
        ):
            raise ProvisioningError("ingress_env_database_url_invalid")
        if ingress_value != _ingress_database_url(
            password=ingress_password,
            host=database_host,
        ):
            raise ProvisioningError("ingress_env_database_url_noncanonical")
    return RuntimeCredential(
        password=password,
        database_url=value,
        ingress_password=ingress_password,
        ingress_database_url=ingress_value,
    )


def _new_credential(*, database_host: str) -> RuntimeCredential:
    password = secrets.token_urlsafe(48)
    ingress_password = secrets.token_urlsafe(48)
    if not PASSWORD_RE.fullmatch(password):  # pragma: no cover - token_urlsafe contract
        raise ProvisioningError("generated_runtime_password_invalid")
    if not PASSWORD_RE.fullmatch(
        ingress_password
    ):  # pragma: no cover - token_urlsafe contract
        raise ProvisioningError("generated_ingress_runtime_password_invalid")
    return RuntimeCredential(
        password=password,
        database_url=_database_url(password=password, host=database_host),
        ingress_password=ingress_password,
        ingress_database_url=_ingress_database_url(
            password=ingress_password,
            host=database_host,
        ),
    )


def _with_new_ingress_credential(
    credential: RuntimeCredential,
    *,
    database_host: str,
) -> RuntimeCredential:
    if credential.ingress_password or credential.ingress_database_url:
        return credential
    ingress_password = secrets.token_urlsafe(48)
    if not PASSWORD_RE.fullmatch(
        ingress_password
    ):  # pragma: no cover - token_urlsafe contract
        raise ProvisioningError("generated_ingress_runtime_password_invalid")
    return RuntimeCredential(
        password=credential.password,
        database_url=credential.database_url,
        ingress_password=ingress_password,
        ingress_database_url=_ingress_database_url(
            password=ingress_password,
            host=database_host,
        ),
    )


def _temporary_env_file(destination: Path, credential: RuntimeCredential) -> Path:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise ProvisioningError("admission_env_parent_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = (
            f"{ENV_KEY}={credential.database_url}\n"
            f"{INGRESS_ENV_KEY}={credential.ingress_database_url}\n"
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return temporary


def _publish_env_file(
    temporary: Path,
    destination: Path,
    *,
    replace: bool = False,
) -> None:
    if (destination.exists() or destination.is_symlink()) and not replace:
        raise ProvisioningError("admission_env_destination_exists")
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _role_sql(password: str, ingress_password: str) -> str:
    password_literal = _sql_literal(password)
    ingress_password_literal = _sql_literal(ingress_password)
    return f"""
DO $propertyquarry_roles$
DECLARE
    role_row pg_catalog.pg_roles%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
        CREATE ROLE {OWNER_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{CAPACITY_OWNER_ROLE}') THEN
        CREATE ROLE {CAPACITY_OWNER_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = '{INGRESS_RUNTIME_ROLE}'
    ) THEN
        CREATE ROLE {INGRESS_RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;

    FOR role_row IN
        SELECT * FROM pg_catalog.pg_roles
        WHERE rolname IN ('{OWNER_ROLE}', '{CAPACITY_OWNER_ROLE}')
    LOOP
        IF role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
           OR role_row.rolcreatedb OR role_row.rolcreaterole
           OR role_row.rolreplication OR role_row.rolbypassrls THEN
            RAISE EXCEPTION 'unsafe admission authority role:%', role_row.rolname;
        END IF;
    END LOOP;

    SELECT * INTO role_row FROM pg_catalog.pg_roles WHERE rolname = '{OWNER_ROLE}';
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        WHERE membership.member = role_row.oid
          AND granted_role.rolname <> '{CAPACITY_OWNER_ROLE}'
    ) THEN
        RAISE EXCEPTION 'unsafe admission owner memberships';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{CAPACITY_OWNER_ROLE}';
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = role_row.oid
    ) THEN
        RAISE EXCEPTION 'unsafe admission capacity owner memberships';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles WHERE rolname = '{RUNTIME_ROLE}';
    IF NOT role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members membership
            WHERE membership.member = role_row.oid
       ) THEN
        RAISE EXCEPTION 'unsafe admission runtime role';
    END IF;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = '{INGRESS_RUNTIME_ROLE}';
    IF NOT role_row.rolcanlogin OR role_row.rolinherit OR role_row.rolsuper
       OR role_row.rolcreatedb OR role_row.rolcreaterole
       OR role_row.rolreplication OR role_row.rolbypassrls
       OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members membership
            WHERE membership.member = role_row.oid
       ) THEN
        RAISE EXCEPTION 'unsafe ingress runtime role';
    END IF;
END
$propertyquarry_roles$;
GRANT {CAPACITY_OWNER_ROLE} TO {OWNER_ROLE};
ALTER ROLE {RUNTIME_ROLE} PASSWORD {password_literal};
ALTER ROLE {INGRESS_RUNTIME_ROLE} PASSWORD {ingress_password_literal};
SELECT CASE
    WHEN NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{DATABASE_NAME}'
    ) THEN 'absent'
    WHEN (
        SELECT owner_role.rolname
        FROM pg_catalog.pg_database database_row
        JOIN pg_catalog.pg_roles owner_role ON owner_role.oid = database_row.datdba
        WHERE database_row.datname = '{DATABASE_NAME}'
    ) = '{OWNER_ROLE}' THEN 'present'
    ELSE 'wrong-owner'
END;
"""


def _database_security_sql() -> str:
    return f"""
REVOKE ALL PRIVILEGES ON DATABASE {DATABASE_NAME} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE {DATABASE_NAME} FROM {RUNTIME_ROLE};
REVOKE ALL PRIVILEGES ON DATABASE {DATABASE_NAME} FROM {INGRESS_RUNTIME_ROLE};
GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {RUNTIME_ROLE};
GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {INGRESS_RUNTIME_ROLE};
ALTER ROLE {RUNTIME_ROLE} IN DATABASE {DATABASE_NAME}
    SET search_path TO {SCHEMA_NAME}, pg_catalog;
ALTER ROLE {INGRESS_RUNTIME_ROLE} IN DATABASE {DATABASE_NAME}
    SET search_path TO {SCHEMA_NAME}, pg_catalog;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
DO $propertyquarry_schema$
DECLARE
    observed_owner TEXT;
BEGIN
    SELECT owner_role.rolname
    INTO observed_owner
    FROM pg_catalog.pg_namespace namespace_row
    JOIN pg_catalog.pg_roles owner_role ON owner_role.oid = namespace_row.nspowner
    WHERE namespace_row.nspname = '{SCHEMA_NAME}';
    IF observed_owner IS NULL THEN
        EXECUTE 'CREATE SCHEMA {SCHEMA_NAME} AUTHORIZATION {OWNER_ROLE}';
    ELSIF observed_owner <> '{OWNER_ROLE}' THEN
        RAISE EXCEPTION 'admission schema owner drift';
    END IF;
END
$propertyquarry_schema$;
SELECT concat_ws('|',
    CASE WHEN to_regclass('{SCHEMA_NAME}.propertyquarry_admission_quota_buckets') IS NULL THEN '0' ELSE '1' END,
    CASE WHEN to_regclass('{SCHEMA_NAME}.propertyquarry_admission_leases') IS NULL THEN '0' ELSE '1' END,
    CASE WHEN to_regclass('{SCHEMA_NAME}.propertyquarry_admission_capacity_state') IS NULL THEN '0' ELSE '1' END,
    CASE WHEN to_regclass('{SCHEMA_NAME}.{INGRESS_QUOTA_TABLE}') IS NULL THEN '0' ELSE '1' END,
    CASE WHEN to_regclass('{SCHEMA_NAME}.{INGRESS_LEASE_TABLE}') IS NULL THEN '0' ELSE '1' END,
    CASE WHEN to_regclass('{SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}') IS NULL THEN '0' ELSE '1' END
);
"""


def _ingress_migration_sql() -> str:
    """Return the isolated API-ingress schema and bounded counter contract."""
    return f"""
BEGIN;
SET LOCAL ROLE {OWNER_ROLE};
SET LOCAL search_path TO {SCHEMA_NAME}, pg_catalog;

CREATE TABLE {INGRESS_QUOTA_TABLE} (
    quota_kind TEXT NOT NULL,
    dimension TEXT NOT NULL,
    subject_digest TEXT NOT NULL,
    window_id BIGINT NOT NULL,
    window_seconds INTEGER NOT NULL,
    used_units BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (quota_kind, dimension, subject_digest, window_id),
    CONSTRAINT pq_ingress_quota_kind_check
        CHECK (quota_kind IN ('request', 'cost')),
    CONSTRAINT pq_ingress_quota_dimension_check
        CHECK (dimension IN ('ip', 'account')),
    CONSTRAINT pq_ingress_quota_subject_digest_check
        CHECK (
            subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'
        ),
    CONSTRAINT pq_ingress_quota_window_id_check
        CHECK (window_id >= 0),
    CONSTRAINT pq_ingress_quota_window_seconds_check
        CHECK (window_seconds BETWEEN 1 AND 2678400),
    CONSTRAINT pq_ingress_quota_used_units_check
        CHECK (used_units >= 0),
    CONSTRAINT pq_ingress_quota_expiry_check
        CHECK (expires_at > updated_at)
);

CREATE INDEX idx_propertyquarry_ingress_quota_expiry
    ON {INGRESS_QUOTA_TABLE}(expires_at, quota_kind, dimension);

CREATE TABLE {INGRESS_LEASE_TABLE} (
    lease_token UUID PRIMARY KEY,
    ip_subject_digest TEXT NOT NULL,
    account_subject_digest TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT pq_ingress_lease_ip_digest_check
        CHECK (
            ip_subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'
        ),
    CONSTRAINT pq_ingress_lease_account_digest_check
        CHECK (
            account_subject_digest IS NULL
            OR account_subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'
        ),
    CONSTRAINT pq_ingress_lease_time_check
        CHECK (
            heartbeat_at >= created_at
            AND expires_at > heartbeat_at
        )
);

CREATE INDEX idx_propertyquarry_ingress_lease_expiry
    ON {INGRESS_LEASE_TABLE}(expires_at, lease_token);
CREATE INDEX idx_propertyquarry_ingress_lease_ip_expiry
    ON {INGRESS_LEASE_TABLE}(ip_subject_digest, expires_at, lease_token);
CREATE INDEX idx_propertyquarry_ingress_lease_account_expiry
    ON {INGRESS_LEASE_TABLE}(account_subject_digest, expires_at, lease_token)
    WHERE account_subject_digest IS NOT NULL;

CREATE TABLE {INGRESS_CAPACITY_TABLE} (
    capacity_key TEXT PRIMARY KEY,
    row_count BIGINT NOT NULL,
    hard_limit BIGINT NOT NULL,
    contract_version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT pq_ingress_capacity_key_check
        CHECK (capacity_key IN ('quota', 'lease')),
    CONSTRAINT pq_ingress_capacity_count_check
        CHECK (row_count >= 0 AND row_count <= hard_limit),
    CONSTRAINT pq_ingress_capacity_limit_check
        CHECK (
            (capacity_key = 'quota' AND hard_limit = 1000000)
            OR
            (capacity_key = 'lease' AND hard_limit = 100000)
        ),
    CONSTRAINT pq_ingress_capacity_version_check
        CHECK (contract_version = 1)
);

INSERT INTO {INGRESS_CAPACITY_TABLE}
    (capacity_key, row_count, hard_limit, contract_version)
VALUES
    ('quota', 0, 1000000, 1),
    ('lease', 0, 100000, 1);

CREATE FUNCTION {INGRESS_CAPACITY_INSERT_FUNCTION}()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $propertyquarry_ingress_capacity_insert$
DECLARE
    inserted_count BIGINT;
BEGIN
    SELECT pg_catalog.count(*)
    INTO inserted_count
    FROM propertyquarry_ingress_inserted_rows;
    IF inserted_count = 0 THEN
        RETURN NULL;
    END IF;
    UPDATE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    SET row_count = row_count + inserted_count,
        updated_at = pg_catalog.statement_timestamp()
    WHERE capacity_key = TG_ARGV[0]
      AND row_count <= hard_limit - inserted_count;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'propertyquarry_ingress_admission_capacity_exceeded:%',
            TG_ARGV[0]
            USING ERRCODE = '54000';
    END IF;
    RETURN NULL;
END
$propertyquarry_ingress_capacity_insert$;

CREATE FUNCTION {INGRESS_CAPACITY_DELETE_FUNCTION}()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $propertyquarry_ingress_capacity_delete$
DECLARE
    deleted_count BIGINT;
BEGIN
    SELECT pg_catalog.count(*)
    INTO deleted_count
    FROM propertyquarry_ingress_deleted_rows;
    IF deleted_count = 0 THEN
        RETURN NULL;
    END IF;
    UPDATE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    SET row_count = row_count - deleted_count,
        updated_at = pg_catalog.statement_timestamp()
    WHERE capacity_key = TG_ARGV[0]
      AND row_count >= deleted_count;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'propertyquarry_ingress_admission_capacity_underflow:%',
            TG_ARGV[0]
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END
$propertyquarry_ingress_capacity_delete$;

CREATE FUNCTION {INGRESS_CAPACITY_TRUNCATE_FUNCTION}()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $propertyquarry_ingress_capacity_truncate$
BEGIN
    UPDATE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    SET row_count = 0,
        updated_at = pg_catalog.statement_timestamp()
    WHERE capacity_key = TG_ARGV[0];
    IF NOT FOUND THEN
        RAISE EXCEPTION 'propertyquarry_ingress_admission_capacity_missing:%',
            TG_ARGV[0]
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END
$propertyquarry_ingress_capacity_truncate$;

CREATE TRIGGER propertyquarry_ingress_quota_capacity_after_insert
AFTER INSERT ON {INGRESS_QUOTA_TABLE}
REFERENCING NEW TABLE AS propertyquarry_ingress_inserted_rows
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_INSERT_FUNCTION}('quota');

CREATE TRIGGER propertyquarry_ingress_quota_capacity_after_delete
AFTER DELETE ON {INGRESS_QUOTA_TABLE}
REFERENCING OLD TABLE AS propertyquarry_ingress_deleted_rows
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_DELETE_FUNCTION}('quota');

CREATE TRIGGER propertyquarry_ingress_quota_capacity_after_truncate
AFTER TRUNCATE ON {INGRESS_QUOTA_TABLE}
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_TRUNCATE_FUNCTION}('quota');

CREATE TRIGGER propertyquarry_ingress_lease_capacity_after_insert
AFTER INSERT ON {INGRESS_LEASE_TABLE}
REFERENCING NEW TABLE AS propertyquarry_ingress_inserted_rows
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_INSERT_FUNCTION}('lease');

CREATE TRIGGER propertyquarry_ingress_lease_capacity_after_delete
AFTER DELETE ON {INGRESS_LEASE_TABLE}
REFERENCING OLD TABLE AS propertyquarry_ingress_deleted_rows
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_DELETE_FUNCTION}('lease');

CREATE TRIGGER propertyquarry_ingress_lease_capacity_after_truncate
AFTER TRUNCATE ON {INGRESS_LEASE_TABLE}
FOR EACH STATEMENT
EXECUTE FUNCTION {INGRESS_CAPACITY_TRUNCATE_FUNCTION}('lease');

REVOKE ALL PRIVILEGES
    ON FUNCTION {INGRESS_CAPACITY_INSERT_FUNCTION}(),
                {INGRESS_CAPACITY_DELETE_FUNCTION}(),
                {INGRESS_CAPACITY_TRUNCATE_FUNCTION}()
    FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE {INGRESS_CAPACITY_TABLE} FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA {SCHEMA_NAME} TO {CAPACITY_OWNER_ROLE};
GRANT SELECT, UPDATE
    ON TABLE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    TO {CAPACITY_OWNER_ROLE};
ALTER FUNCTION {INGRESS_CAPACITY_INSERT_FUNCTION}()
    OWNER TO {CAPACITY_OWNER_ROLE};
ALTER FUNCTION {INGRESS_CAPACITY_DELETE_FUNCTION}()
    OWNER TO {CAPACITY_OWNER_ROLE};
ALTER FUNCTION {INGRESS_CAPACITY_TRUNCATE_FUNCTION}()
    OWNER TO {CAPACITY_OWNER_ROLE};
REVOKE CREATE ON SCHEMA {SCHEMA_NAME} FROM {CAPACITY_OWNER_ROLE};

RESET ROLE;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON TABLE {SCHEMA_NAME}.{INGRESS_QUOTA_TABLE},
             {SCHEMA_NAME}.{INGRESS_LEASE_TABLE},
             {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    FROM {RUNTIME_ROLE};
REVOKE ALL PRIVILEGES
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_quota_buckets,
             {SCHEMA_NAME}.propertyquarry_admission_leases,
             {SCHEMA_NAME}.propertyquarry_admission_capacity_state
    FROM {INGRESS_RUNTIME_ROLE};
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {RUNTIME_ROLE};
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {INGRESS_RUNTIME_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE {SCHEMA_NAME}.{INGRESS_QUOTA_TABLE},
             {SCHEMA_NAME}.{INGRESS_LEASE_TABLE}
    TO {INGRESS_RUNTIME_ROLE};
GRANT SELECT
    ON TABLE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    TO {INGRESS_RUNTIME_ROLE};
COMMIT;
"""


def _migration_sql() -> str:
    try:
        from app.product.property_search_schema import PROPERTY_SEARCH_MIGRATIONS
    except ImportError as exc:
        raise ProvisioningError("property_search_migrations_unavailable") from exc
    if len(PROPERTY_SEARCH_MIGRATIONS) < 17:
        raise ProvisioningError("admission_migrations_missing")
    migration_16 = PROPERTY_SEARCH_MIGRATIONS[15]
    migration_17 = PROPERTY_SEARCH_MIGRATIONS[16]
    if (
        migration_16.version != 16
        or migration_16.name != "distributed_request_admission_control"
        or migration_17.version != 17
        or migration_17.name != "bounded_admission_capacity_state"
    ):
        raise ProvisioningError("admission_migration_identity_drift")
    legacy_migration = f"""
BEGIN;
SET LOCAL ROLE {OWNER_ROLE};
SET LOCAL search_path TO {SCHEMA_NAME}, pg_catalog;
{migration_16.sql}
SELECT set_config(
    'propertyquarry.admission_capacity_owner_role',
    '{CAPACITY_OWNER_ROLE}',
    TRUE
);
{migration_17.sql}
RESET ROLE;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {RUNTIME_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_quota_buckets,
             {SCHEMA_NAME}.propertyquarry_admission_leases
    TO {RUNTIME_ROLE};
GRANT SELECT
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_capacity_state
    TO {RUNTIME_ROLE};
COMMIT;
"""
    return legacy_migration + _ingress_migration_sql()


def _grant_sql() -> str:
    return f"""
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON TABLE {SCHEMA_NAME}.{INGRESS_QUOTA_TABLE},
             {SCHEMA_NAME}.{INGRESS_LEASE_TABLE},
             {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    FROM {RUNTIME_ROLE};
REVOKE ALL PRIVILEGES
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_quota_buckets,
             {SCHEMA_NAME}.propertyquarry_admission_leases,
             {SCHEMA_NAME}.propertyquarry_admission_capacity_state
    FROM {INGRESS_RUNTIME_ROLE};
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {RUNTIME_ROLE};
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {INGRESS_RUNTIME_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_quota_buckets,
             {SCHEMA_NAME}.propertyquarry_admission_leases
    TO {RUNTIME_ROLE};
GRANT SELECT
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_capacity_state
    TO {RUNTIME_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE {SCHEMA_NAME}.{INGRESS_QUOTA_TABLE},
             {SCHEMA_NAME}.{INGRESS_LEASE_TABLE}
    TO {INGRESS_RUNTIME_ROLE};
GRANT SELECT
    ON TABLE {SCHEMA_NAME}.{INGRESS_CAPACITY_TABLE}
    TO {INGRESS_RUNTIME_ROLE};
"""


def _provision_database(*, container: str, credential: RuntimeCredential) -> None:
    role_state = _psql(
        container=container,
        database="template1",
        sql=_role_sql(credential.password, credential.ingress_password),
        password=credential.password,
    ).splitlines()
    state = role_state[-1].strip() if role_state else ""
    if state == "absent":
        _psql(
            container=container,
            database="template1",
            sql=(
                f"CREATE DATABASE {DATABASE_NAME} OWNER {OWNER_ROLE} "
                "TEMPLATE template0 ENCODING 'UTF8';\n"
            ),
            password=credential.password,
        )
    elif state != "present":
        raise ProvisioningError("admission_database_owner_invalid")

    schema_state = _psql(
        container=container,
        database=DATABASE_NAME,
        sql=_database_security_sql(),
        password=credential.password,
    ).splitlines()
    relation_state = schema_state[-1].strip() if schema_state else ""
    if relation_state == "0|0|0|0|0|0":
        _psql(
            container=container,
            database=DATABASE_NAME,
            sql=_migration_sql(),
            password=credential.password,
        )
    elif relation_state == "1|1|1|0|0|0":
        _psql(
            container=container,
            database=DATABASE_NAME,
            sql=_ingress_migration_sql(),
            password=credential.password,
        )
    elif relation_state == "1|1|1|1|1|1":
        _psql(
            container=container,
            database=DATABASE_NAME,
            sql=_grant_sql(),
            password=credential.password,
        )
    else:
        raise ProvisioningError("admission_database_partial_schema")


def _probe_runtime(
    *,
    image: str,
    network: str,
    env_file: Path,
    credential: RuntimeCredential,
) -> None:
    if not IMAGE_RE.fullmatch(image):
        raise ProvisioningError("runtime_image_must_be_digest_pinned")
    if not NAME_RE.fullmatch(network):
        raise ProvisioningError("docker_network_invalid")
    code = (
        "import os; "
        "from app.api.ingress_admission import PostgresIngressAdmissionStore; "
        "from app.services.admission_control import build_admission_backend; "
        f"u=os.environ[{ENV_KEY!r}]; "
        f"v=os.environ[{INGRESS_ENV_KEY!r}]; "
        "b=build_admission_backend(runtime_mode='prod',database_url=u); "
        "b.probe(); "
        "i=PostgresIngressAdmissionStore("
        "v,hmac_secret='propertyquarry-provision-probe-secret-v1',"
        "erasure_key_id='0'*64); "
        "s=i.capacity_snapshot(); "
        "assert s.contract_valid and len(s.rows)==2; "
        "print('admission-probe-ok')"
    )
    result = _run(
        (
            "/usr/bin/docker",
            "run",
            "--rm",
            "--pull=never",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop=ALL",
            "--network",
            network,
            "--env-file",
            str(env_file),
            image,
            "python",
            "-c",
            code,
        ),
        timeout_seconds=90,
        secrets_to_hide=(
            credential.password,
            credential.database_url,
            credential.ingress_password,
            credential.ingress_database_url,
        ),
    )
    if result.stdout.strip() != "admission-probe-ok":
        raise ProvisioningError("admission_runtime_probe_invalid")


def _receipt(
    *,
    image: str,
    env_file: Path,
    env_sha256: str,
    reused_credential: bool,
) -> dict[str, object]:
    return {
        "schema": "propertyquarry.admission_database.provision_receipt.v1",
        "status": "pass",
        "database": DATABASE_NAME,
        "schema_name": SCHEMA_NAME,
        "owner_role": OWNER_ROLE,
        "capacity_owner_role": CAPACITY_OWNER_ROLE,
        "runtime_role": RUNTIME_ROLE,
        "ingress_runtime_role": INGRESS_RUNTIME_ROLE,
        "runtime_image": image,
        "env_file": str(env_file),
        "env_file_sha256": env_sha256,
        "credential_reused": reused_credential,
        "least_privilege_probe": True,
        "secret_values_emitted": False,
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def provision(args: argparse.Namespace) -> dict[str, object]:
    container = str(args.database_container)
    database_host = str(args.database_host)
    network = str(args.docker_network)
    image = str(args.runtime_image)
    env_file = _validated_cli_path(args.env_file, label="admission_env")
    receipt_file = _validated_cli_path(args.receipt, label="receipt")
    if not NAME_RE.fullmatch(container):
        raise ProvisioningError("database_container_invalid")
    if not NAME_RE.fullmatch(database_host):
        raise ProvisioningError("database_host_invalid")

    reused = env_file.exists() or env_file.is_symlink()
    upgraded = False
    temporary: Path | None = None
    if reused:
        credential = _parse_env_file(env_file, database_host=database_host)
        if not credential.ingress_database_url:
            credential = _with_new_ingress_credential(
                credential,
                database_host=database_host,
            )
            temporary = _temporary_env_file(env_file, credential)
            probe_env = temporary
            upgraded = True
        else:
            probe_env = env_file
    else:
        credential = _new_credential(database_host=database_host)
        temporary = _temporary_env_file(env_file, credential)
        probe_env = temporary
    try:
        _provision_database(container=container, credential=credential)
        _probe_runtime(
            image=image,
            network=network,
            env_file=probe_env,
            credential=credential,
        )
        if temporary is not None:
            _publish_env_file(temporary, env_file, replace=upgraded)
            temporary = None
        env_sha256 = hashlib.sha256(env_file.read_bytes()).hexdigest()
        receipt = _receipt(
            image=image,
            env_file=env_file,
            env_sha256=env_sha256,
            reused_credential=reused,
        )
        _write_receipt(receipt_file, receipt)
        return receipt
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--database-container", default=DEFAULT_DATABASE_CONTAINER)
    parser.add_argument("--database-host", default=DEFAULT_DATABASE_HOST)
    parser.add_argument("--docker-network", default=DEFAULT_DOCKER_NETWORK)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        receipt = provision(build_parser().parse_args(argv))
    except ProvisioningError as exc:
        print(f"admission provisioning rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
