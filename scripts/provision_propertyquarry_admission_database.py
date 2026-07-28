#!/usr/bin/env python3
"""Provision PropertyQuarry's least-privilege PostgreSQL admission authority.

The API deliberately refuses production startup when request admission shares
the application database credential.  This host-side provisioner creates the
closed database/role/schema contract exercised by ``admission_control.py`` and
writes only a dedicated, mode-0600 env file.  Secrets are passed to PostgreSQL
over stdin and are never printed or placed in process arguments.
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
ENV_KEY = "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
DEFAULT_DATABASE_CONTAINER = "propertyquarry-db-live"
DEFAULT_DATABASE_HOST = "propertyquarry-db"
DEFAULT_DOCKER_NETWORK = "property_default"
DEFAULT_ENV_FILE = Path("state/runtime/propertyquarry_admission.env")
IMAGE_RE = re.compile(
    r"^(?:[a-z0-9./_-]+@)?sha256:[0-9a-f]{64}$"
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{48,128}$")


class ProvisioningError(RuntimeError):
    """The admission authority could not be provisioned safely."""


@dataclass(frozen=True)
class RuntimeCredential:
    password: str
    database_url: str


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
    if len(assignments) != 1 or not assignments[0].startswith(f"{ENV_KEY}="):
        raise ProvisioningError("admission_env_fields_invalid")
    value = assignments[0].split("=", 1)[1]
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
    return RuntimeCredential(password=password, database_url=value)


def _new_credential(*, database_host: str) -> RuntimeCredential:
    password = secrets.token_urlsafe(48)
    if not PASSWORD_RE.fullmatch(password):  # pragma: no cover - token_urlsafe contract
        raise ProvisioningError("generated_runtime_password_invalid")
    return RuntimeCredential(
        password=password,
        database_url=_database_url(password=password, host=database_host),
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
        payload = f"{ENV_KEY}={credential.database_url}\n".encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return temporary


def _publish_env_file(temporary: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ProvisioningError("admission_env_destination_exists")
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _role_sql(password: str) -> str:
    password_literal = _sql_literal(password)
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
END
$propertyquarry_roles$;
GRANT {CAPACITY_OWNER_ROLE} TO {OWNER_ROLE};
ALTER ROLE {RUNTIME_ROLE} PASSWORD {password_literal};
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
GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {RUNTIME_ROLE};
ALTER ROLE {RUNTIME_ROLE} IN DATABASE {DATABASE_NAME}
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
    CASE WHEN to_regclass('{SCHEMA_NAME}.propertyquarry_admission_capacity_state') IS NULL THEN '0' ELSE '1' END
);
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
    return f"""
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


def _grant_sql() -> str:
    return f"""
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;
GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {RUNTIME_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_quota_buckets,
             {SCHEMA_NAME}.propertyquarry_admission_leases
    TO {RUNTIME_ROLE};
GRANT SELECT
    ON TABLE {SCHEMA_NAME}.propertyquarry_admission_capacity_state
    TO {RUNTIME_ROLE};
"""


def _provision_database(*, container: str, credential: RuntimeCredential) -> None:
    role_state = _psql(
        container=container,
        database="template1",
        sql=_role_sql(credential.password),
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
    if relation_state == "0|0|0":
        _psql(
            container=container,
            database=DATABASE_NAME,
            sql=_migration_sql(),
            password=credential.password,
        )
    elif relation_state == "1|1|1":
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
        "from app.services.admission_control import build_admission_backend; "
        f"u=os.environ[{ENV_KEY!r}]; "
        "b=build_admission_backend(runtime_mode='prod',database_url=u); "
        "b.probe(); print('admission-probe-ok')"
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
        secrets_to_hide=(credential.password, credential.database_url),
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
    temporary: Path | None = None
    if reused:
        credential = _parse_env_file(env_file, database_host=database_host)
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
            _publish_env_file(temporary, env_file)
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
