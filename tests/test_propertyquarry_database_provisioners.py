from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from types import ModuleType
from urllib.parse import quote
from uuid import uuid4

import pytest

from app.kernel_schema import (
    LATEST_KERNEL_SCHEMA_VERSION,
    load_kernel_migrations,
    required_kernel_relations,
)


ROOT = Path(__file__).resolve().parents[1]
PASSWORD_A = "A" * 48
PASSWORD_B = "B" * 48


def _load_script(module_name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


admission = _load_script(
    "test_propertyquarry_admission_provisioner",
    "scripts/provision_propertyquarry_admission_database.py",
)
runtime = _load_script(
    "test_propertyquarry_runtime_database_provisioner",
    "scripts/provision_propertyquarry_runtime_database.py",
)


def _write_mode_0600(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)


def _runtime_values(*, migrator_url: str | None = None) -> dict[str, str]:
    values = {
        key: (
            runtime._migrator_url(password=PASSWORD_A)
            if role == runtime.MIGRATOR_ROLE
            else runtime._runtime_url(role=role, password=PASSWORD_A)
        )
        for key, role in runtime.ROLE_KEYS.items()
    }
    if migrator_url is not None:
        values["PROPERTYQUARRY_MIGRATION_DATABASE_URL"] = migrator_url
    admission_url = (
        "postgresql://propertyquarry_admission_runtime:"
        f"{PASSWORD_B}@propertyquarry-db:5432/propertyquarry_admission"
    )
    values[runtime.ADMISSION_KEY] = admission_url
    values[runtime.RENDER_KEY] = admission_url
    values[runtime.ERASURE_KEY] = "E" * 48
    return values


def test_admission_env_is_published_atomically_with_strict_metadata(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "runtime" / "admission.env"
    credential = admission.RuntimeCredential(
        password=PASSWORD_A,
        database_url=admission._database_url(
            password=PASSWORD_A,
            host=admission.DEFAULT_DATABASE_HOST,
        ),
    )

    temporary = admission._temporary_env_file(destination, credential)
    assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
    assert not destination.exists()

    admission._publish_env_file(temporary, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_nlink == 1
    assert (
        admission._parse_env_file(
            destination,
            database_host=admission.DEFAULT_DATABASE_HOST,
        )
        == credential
    )


def test_admission_env_rejects_symlink_without_reading_its_target(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.env"
    _write_mode_0600(victim, "do-not-parse\n")
    linked = tmp_path / "admission.env"
    linked.symlink_to(victim)

    with pytest.raises(
        admission.ProvisioningError,
        match="admission_env_must_be_regular",
    ):
        admission._parse_env_file(
            linked,
            database_host=admission.DEFAULT_DATABASE_HOST,
        )

    assert victim.read_text(encoding="utf-8") == "do-not-parse\n"


def test_admission_probe_is_digest_pinned_and_keeps_secret_out_of_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "admission.env"
    credential = admission.RuntimeCredential(
        password=PASSWORD_A,
        database_url=admission._database_url(
            password=PASSWORD_A,
            host=admission.DEFAULT_DATABASE_HOST,
        ),
    )
    observed: dict[str, object] = {}

    def fake_run(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "admission-probe-ok\n", "")

    monkeypatch.setattr(admission, "_run", fake_run)
    image = f"ghcr.io/example/property@sha256:{'a' * 64}"

    admission._probe_runtime(
        image=image,
        network="property_default",
        env_file=env_file,
        credential=credential,
    )

    argv = tuple(observed["argv"])
    assert ("--pull=never", "--read-only", "--cap-drop=ALL") == (
        argv[argv.index("--pull=never")],
        argv[argv.index("--read-only")],
        argv[argv.index("--cap-drop=ALL")],
    )
    assert str(env_file.resolve()) in argv
    assert credential.password not in " ".join(argv)
    assert credential.database_url not in " ".join(argv)
    assert observed["kwargs"]["secrets_to_hide"] == (
        credential.password,
        credential.database_url,
    )

    with pytest.raises(
        admission.ProvisioningError,
        match="runtime_image_must_be_digest_pinned",
    ):
        admission._probe_runtime(
            image="ghcr.io/example/property:latest",
            network="property_default",
            env_file=env_file,
            credential=credential,
        )


def test_admission_database_creation_runs_only_the_required_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    results = iter(("absent", "", "0|0|0", ""))

    def fake_psql(*, database: str, sql: str, **_kwargs: object) -> str:
        calls.append((database, sql))
        return next(results)

    monkeypatch.setattr(admission, "_psql", fake_psql)
    monkeypatch.setattr(admission, "_migration_sql", lambda: "MIGRATIONS_16_AND_17")
    credential = admission.RuntimeCredential(PASSWORD_A, "unused-in-this-test")

    admission._provision_database(
        container="propertyquarry-db-live", credential=credential
    )

    assert [database for database, _sql in calls] == [
        "template1",
        "template1",
        admission.DATABASE_NAME,
        admission.DATABASE_NAME,
    ]
    assert f"CREATE DATABASE {admission.DATABASE_NAME}" in calls[1][1]
    assert calls[3][1] == "MIGRATIONS_16_AND_17"


def test_admission_role_sql_rejects_excess_authority_memberships() -> None:
    role_sql = admission._role_sql(PASSWORD_A)

    assert "unsafe admission owner memberships" in role_sql
    assert "unsafe admission capacity owner memberships" in role_sql
    assert f"granted_role.rolname <> '{admission.CAPACITY_OWNER_ROLE}'" in role_sql
    assert role_sql.index("unsafe admission owner memberships") < role_sql.index(
        f"GRANT {admission.CAPACITY_OWNER_ROLE} TO {admission.OWNER_ROLE}"
    )


@pytest.mark.parametrize(
    ("field", "error"),
    (
        ("env_file", "admission_env_path_symlink"),
        ("receipt", "receipt_path_symlink"),
    ),
)
def test_admission_public_entry_rejects_symlink_paths_before_provisioning(
    field: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("untouched\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(victim)
    args = argparse.Namespace(
        database_container=admission.DEFAULT_DATABASE_CONTAINER,
        database_host=admission.DEFAULT_DATABASE_HOST,
        docker_network=admission.DEFAULT_DOCKER_NETWORK,
        runtime_image=f"ghcr.io/example/property@sha256:{'a' * 64}",
        env_file=tmp_path / "admission.env",
        receipt=tmp_path / "receipt.json",
    )
    setattr(args, field, linked)
    monkeypatch.setattr(
        admission,
        "_provision_database",
        lambda **_kwargs: pytest.fail("provisioning must not start"),
    )

    with pytest.raises(admission.ProvisioningError, match=error):
        admission.provision(args)

    assert victim.read_text(encoding="utf-8") == "untouched\n"


def test_command_failures_redact_raw_and_url_encoded_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sensitive/value"
    encoded = quote(secret, safe="")

    def failed_run(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["docker"],
            19,
            "",
            f"database rejected {secret} and {encoded}",
        )

    monkeypatch.setattr(admission.subprocess, "run", failed_run)
    with pytest.raises(admission.ProvisioningError) as admission_error:
        admission._run(("/usr/bin/docker", "version"), secrets_to_hide=(secret,))
    assert secret not in str(admission_error.value)
    assert encoded not in str(admission_error.value)
    assert "***" in str(admission_error.value)

    monkeypatch.setattr(runtime.subprocess, "run", failed_run)
    with pytest.raises(runtime.RuntimeDatabaseError) as runtime_error:
        runtime._run(("/usr/bin/docker", "version"), stdin="", hidden=(secret,))
    assert secret not in str(runtime_error.value)
    assert encoded not in str(runtime_error.value)
    assert "***" in str(runtime_error.value)


def test_migrator_url_uses_libpq_safe_percent_encoding() -> None:
    canonical = runtime._migrator_url(password=PASSWORD_A)

    assert "+" not in canonical
    assert "options=-c%20role%3Dpropertyquarry_owner%20-c%20search_path" in canonical
    assert (
        runtime._parse_database_url(
            canonical,
            expected_role=runtime.MIGRATOR_ROLE,
            migrator=True,
        )
        == PASSWORD_A
    )


def test_existing_legacy_migrator_url_is_repaired_in_memory(
    tmp_path: Path,
) -> None:
    canonical = runtime._migrator_url(password=PASSWORD_A)
    legacy = canonical.replace("%20", "+")
    values = _runtime_values(migrator_url=legacy)
    admission_env = tmp_path / "admission.env"
    runtime_env = tmp_path / "runtime.env"
    _write_mode_0600(
        admission_env,
        f"{runtime.ADMISSION_KEY}={values[runtime.ADMISSION_KEY]}\n",
    )
    _write_mode_0600(
        runtime_env,
        "".join(f"{key}={values[key]}\n" for key in sorted(values)),
    )

    loaded, reused, repaired = runtime._load_or_create_values(
        env_file=runtime_env,
        admission_env_file=admission_env,
    )

    assert reused is True
    assert repaired is True
    assert loaded["PROPERTYQUARRY_MIGRATION_DATABASE_URL"] == canonical


def test_admission_url_is_canonical_dedicated_and_role_bound() -> None:
    canonical = runtime._admission_url(password=PASSWORD_B)

    assert runtime._parse_admission_url(canonical) == PASSWORD_B
    assert canonical.endswith(f"/{runtime.ADMISSION_DATABASE}")
    for invalid in (
        canonical.replace(runtime.ADMISSION_RUNTIME_ROLE, runtime.API_ROLE),
        canonical.replace(runtime.ADMISSION_DATABASE, runtime.TARGET_DATABASE),
        canonical.replace(runtime.DATABASE_HOST, "other-database"),
        canonical + "?sslmode=disable",
        canonical + "#fragment",
    ):
        with pytest.raises(
            runtime.RuntimeDatabaseError, match="admission_database_url"
        ):
            runtime._parse_admission_url(invalid)


def test_runtime_env_writer_is_mode_0600_and_rejects_symlink_parent(
    tmp_path: Path,
) -> None:
    values = _runtime_values()
    destination = tmp_path / "runtime" / "roles.env"

    runtime._write_env(destination, values)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_nlink == 1
    assert runtime._read_regular_env(destination) == values

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(
        runtime.RuntimeDatabaseError, match="runtime_env_parent_invalid"
    ):
        runtime._write_env(linked_parent / "roles.env", values)
    assert not (real_parent / "roles.env").exists()


@pytest.mark.parametrize(
    ("field", "error"),
    (
        ("env_file", "runtime_env_path_symlink"),
        ("admission_env_file", "admission_env_path_symlink"),
        ("receipt", "receipt_path_symlink"),
    ),
)
def test_runtime_public_entry_rejects_symlink_paths_before_loading_credentials(
    field: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("untouched\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(victim)
    args = argparse.Namespace(
        operation="prepare",
        database_container=runtime.DEFAULT_DATABASE_CONTAINER,
        env_file=tmp_path / "runtime.env",
        admission_env_file=tmp_path / "admission.env",
        receipt=tmp_path / "receipt.json",
    )
    setattr(args, field, linked)
    monkeypatch.setattr(
        runtime,
        "_load_or_create_values",
        lambda **_kwargs: pytest.fail("credentials must not be loaded"),
    )

    with pytest.raises(runtime.RuntimeDatabaseError, match=error):
        runtime.execute(args)

    assert victim.read_text(encoding="utf-8") == "untouched\n"


def test_runtime_sql_contract_fences_runtime_roles_and_owned_objects() -> None:
    passwords = {
        role: chr(65 + index) * 48
        for index, role in enumerate(runtime.ROLE_KEYS.values())
    }

    roles_sql = runtime._prepare_roles_sql(passwords)
    assert f"GRANT {runtime.OWNER_ROLE} TO {runtime.MIGRATOR_ROLE}" in roles_sql
    assert (
        f"GRANT {runtime.ADMISSION_CAPACITY_OWNER_ROLE} TO {runtime.OWNER_ROLE}"
        in roles_sql
    )
    assert "unsafe propertyquarry runtime role" in roles_sql
    assert "pg_auth_members" in roles_sql
    assert "unsafe propertyquarry admission runtime role" in roles_sql
    assert (
        f"granted_role.rolname <> '{runtime.ADMISSION_CAPACITY_OWNER_ROLE}'"
        in roles_sql
    )
    assert f"granted_role.rolname <> '{runtime.OWNER_ROLE}'" in roles_sql
    assert roles_sql.lstrip().startswith("BEGIN;")
    assert roles_sql.rstrip().endswith("COMMIT;")

    configure_sql = runtime._configure_sql()
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in configure_sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" not in configure_sql
    assert "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES" not in configure_sql
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" in configure_sql
    assert (
        "GRANT SELECT, INSERT, DELETE ON TABLE public.property_search_work_jobs "
        f"TO {runtime.API_ROLE}" in configure_sql
    )
    assert (
        "GRANT SELECT, UPDATE, DELETE ON TABLE public.property_search_work_jobs "
        f"TO {runtime.WORKER_ROLE}" in configure_sql
    )
    assert (
        "GRANT SELECT, INSERT ON TABLE public.property_content_job_events "
        f"TO {runtime.API_ROLE}" in configure_sql
    )
    for role in runtime.RUNTIME_ROLES:
        assert (
            "GRANT SELECT ON TABLE public.ea_kernel_schema_migrations "
            f"TO {role}" in configure_sql
        )
    grant_statements = [
        line.strip()
        for line in configure_sql.splitlines()
        if line.strip().startswith("EXECUTE 'GRANT")
    ]
    for protected in (
        *runtime.MIGRATION_TABLES,
        runtime.ACTIVATION_SENTINEL_TABLE,
        *runtime.ADMISSION_CAPACITY_TABLES,
    ):
        mutating = [
            statement
            for statement in grant_statements
            if protected in statement
            and any(
                privilege in statement for privilege in ("INSERT", "UPDATE", "DELETE")
            )
        ]
        assert mutating == []
    for function_name in runtime.ADMISSION_CAPACITY_FUNCTIONS:
        assert function_name in configure_sql
    assert (
        f"owner_role.rolname <> '{runtime.ADMISSION_CAPACITY_OWNER_ROLE}'"
        in configure_sql
    )
    assert "OR NOT routine_row.prosecdef" in configure_sql
    assert "ALTER DEFAULT PRIVILEGES" in configure_sql
    assert "dependency.deptype IN ('a', 'i')" in configure_sql
    assert "runtime role owns a public relation" in configure_sql
    assert configure_sql.lstrip().startswith("BEGIN;")
    assert configure_sql.rstrip().endswith("COMMIT;")


def test_runtime_role_sql_repairs_legacy_owner_membership_and_inheritance() -> None:
    passwords = {
        role: chr(65 + index) * 48
        for index, role in enumerate(runtime.ROLE_KEYS.values())
    }

    roles_sql = runtime._prepare_roles_sql(passwords)
    guard_offset = roles_sql.index("unsafe propertyquarry runtime role")
    for role in runtime.RUNTIME_ROLES:
        normalize = (
            f"ALTER ROLE {role} WITH LOGIN NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;"
        )
        revoke_owner = f"REVOKE {runtime.OWNER_ROLE} FROM {role};"

        assert roles_sql.count(normalize) == 1
        assert roles_sql.count(revoke_owner) == 1
        assert roles_sql.index(normalize) < roles_sql.index(revoke_owner)
        assert roles_sql.index(revoke_owner) < guard_offset

    assert "OR role_row.rolinherit OR role_row.rolsuper" in roles_sql
    assert (
        "SELECT 1 FROM pg_catalog.pg_auth_members AS membership\n"
        "                WHERE membership.member = role_row.oid"
    ) in roles_sql


def test_v0_37_schema_relations_are_bound_to_the_runtime_acl_contract() -> None:
    migration_name = "20260305_v0_37_runtime_repository_contract.sql"
    migration = (ROOT / "ea" / "schema" / migration_name).read_text(encoding="utf-8")
    created_tables = {
        match.group(1)
        for match in re.finditer(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(?:public\.)?([A-Za-z_][A-Za-z0-9_$]*)",
            migration,
            flags=re.IGNORECASE,
        )
    }
    created_sequences = {
        match.group(1)
        for match in re.finditer(
            r"\bCREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(?:public\.)?([A-Za-z_][A-Za-z0-9_$]*)",
            migration,
            flags=re.IGNORECASE,
        )
    }

    assert created_tables == set(runtime.V0_37_RUNTIME_REPOSITORY_TABLES)
    assert created_sequences == set()

    expected_acl: dict[tuple[str, str], set[str]] = {}
    for role, grant_groups in (
        (runtime.API_ROLE, runtime.V0_37_API_TABLE_GRANTS),
        (runtime.WORKER_ROLE, runtime.V0_37_WORKER_TABLE_GRANTS),
    ):
        for privileges, tables in grant_groups:
            for table in tables:
                key = (role, table)
                assert key not in expected_acl
                expected_acl[key] = set(privileges.split(", "))
    assert {
        table for role, table in expected_acl if role == runtime.API_ROLE
    } == created_tables

    configured_acl: dict[tuple[str, str], set[str]] = {}
    for match in re.finditer(
        r"GRANT (?P<privileges>[A-Z, ]+) ON TABLE public\."
        r"(?P<table>[A-Za-z_][A-Za-z0-9_$]*) TO "
        r"(?P<role>[A-Za-z_][A-Za-z0-9_$]*)",
        runtime._configure_sql(),
    ):
        table = match.group("table")
        if table not in created_tables:
            continue
        key = (match.group("role"), table)
        configured_acl.setdefault(key, set()).update(
            match.group("privileges").split(", ")
        )

    assert configured_acl == expected_acl
    assert not any(role == runtime.SCHEDULER_ROLE for role, _table in configured_acl)

    bootstrap = (ROOT / "scripts" / "db_bootstrap.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    assert bootstrap.count(f'  "ea/schema/{migration_name}"') == 1
    assert (
        'command: ["/usr/local/bin/python", "-m", '
        '"app.product.propertyquarry_schema", "migrate"]'
    ) in compose


def test_kernel_relations_are_exhaustively_bound_to_the_runtime_acl_contract() -> None:
    migrated_relations = set(required_kernel_relations(load_kernel_migrations()))
    runtime_relations = set(runtime.KERNEL_RUNTIME_REPOSITORY_TABLES)
    no_grant_relations = set(runtime.KERNEL_NO_RUNTIME_GRANT_TABLES)
    protected_relations = {"ea_kernel_schema_migrations"}

    assert len(runtime_relations) == len(runtime.KERNEL_RUNTIME_REPOSITORY_TABLES)
    assert len(no_grant_relations) == len(runtime.KERNEL_NO_RUNTIME_GRANT_TABLES)
    assert runtime_relations.isdisjoint(no_grant_relations)
    assert runtime_relations.isdisjoint(protected_relations)
    assert no_grant_relations.isdisjoint(protected_relations)
    assert runtime_relations | no_grant_relations | protected_relations == (
        migrated_relations
    )

    expected_acl: dict[tuple[str, str], set[str]] = {}
    for role, grant_groups in runtime.KERNEL_ROLE_TABLE_GRANTS.items():
        for privileges, tables in grant_groups:
            for table in tables:
                assert table in runtime_relations
                key = (role, table)
                assert key not in expected_acl
                expected_acl[key] = set(privileges.split(", "))
    for role in runtime.RUNTIME_ROLES:
        expected_acl[(role, "ea_kernel_schema_migrations")] = {"SELECT"}

    assert {
        table
        for role, table in expected_acl
        if role == runtime.API_ROLE and table in runtime_relations
    } == runtime_relations
    assert {
        table
        for role, table in expected_acl
        if role == runtime.WORKER_ROLE and table in runtime_relations
    } == {
        "evidence_objects",
        "execution_events",
        "execution_sessions",
        "human_tasks",
        "observation_events",
        "onboarding_states",
        "operator_profiles",
        "person_profiles",
        "preference_decision_assessments",
        "preference_evidence_events",
        "preference_nodes",
        "property_agent_question_tasks",
        "property_decision_ledger",
        "property_documents",
        "property_evidence_claims",
        "provider_bindings",
        "tool_registry",
    }
    assert {
        table
        for role, table in expected_acl
        if role == runtime.SCHEDULER_ROLE and table in runtime_relations
    } == {
        "execution_events",
        "execution_sessions",
        "human_tasks",
        "observation_events",
        "operator_profiles",
        "provider_bindings",
        "tool_registry",
    }
    assert expected_acl[(runtime.WORKER_ROLE, "operator_profiles")] == {"SELECT"}
    assert expected_acl[(runtime.WORKER_ROLE, "tool_registry")] == {
        "SELECT",
        "INSERT",
        "UPDATE",
    }
    assert expected_acl[(runtime.SCHEDULER_ROLE, "tool_registry")] == {
        "SELECT",
        "INSERT",
        "UPDATE",
    }

    configured_acl: dict[tuple[str, str], set[str]] = {}
    for match in re.finditer(
        r"GRANT (?P<privileges>[A-Z, ]+) ON TABLE public\."
        r"(?P<table>[A-Za-z_][A-Za-z0-9_$]*) TO "
        r"(?P<role>[A-Za-z_][A-Za-z0-9_$]*)",
        runtime._configure_sql(),
    ):
        table = match.group("table")
        if table not in migrated_relations:
            continue
        key = (match.group("role"), table)
        configured_acl.setdefault(key, set()).update(
            match.group("privileges").split(", ")
        )

    assert configured_acl == expected_acl
    assert not any(
        table in no_grant_relations
        for _role, table in configured_acl
    )
    assert {
        (role, table, frozenset(privileges))
        for (role, table), privileges in configured_acl.items()
        if table in protected_relations
    } == {
        (role, "ea_kernel_schema_migrations", frozenset({"SELECT"}))
        for role in runtime.RUNTIME_ROLES
    }


def test_activation_sentinel_is_transactional_owned_and_database_oid_bound() -> None:
    install_sql = runtime._sentinel_sql(expected_oid=4242, install=True)
    verify_sql = runtime._sentinel_sql(expected_oid=4242, install=False)
    rename_sql = runtime._rename_sql(
        source=runtime.LEGACY_DATABASE,
        target=runtime.TARGET_DATABASE,
        expected_oid=4242,
    )

    assert install_sql.lstrip().startswith("BEGIN;")
    assert install_sql.rstrip().endswith("SELECT 'activation-sentinel-ok';")
    assert (
        f"CREATE TABLE IF NOT EXISTS public.{runtime.ACTIVATION_SENTINEL_TABLE}"
        in install_sql
    )
    assert (
        f"ALTER TABLE public.{runtime.ACTIVATION_SENTINEL_TABLE} "
        f"OWNER TO {runtime.OWNER_ROLE}" in install_sql
    )
    assert runtime.ACTIVATION_SENTINEL_DIGEST in install_sql
    assert "database_oid = 4242::OID" in install_sql
    assert "legacy database is not a PropertyQuarry database" in install_sql
    assert "CREATE TABLE" not in verify_sql
    assert "oid = 4242::OID" in rename_sql
    assert "database rename source identity mismatch" in rename_sql


def test_runtime_receipt_reports_acl_and_rename_recovery_contract(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    _write_mode_0600(env_file, "bounded=test\n")

    receipt = runtime._receipt(
        operation="finalize",
        env_file=env_file,
        reused=True,
    )

    assert receipt["runtime_acl_profile"] == "propertyquarry-role-specific-v3"
    assert receipt["activation_sentinel_table"] == runtime.ACTIVATION_SENTINEL_TABLE
    assert (
        receipt["activation_sentinel_contract_sha256"]
        == runtime.ACTIVATION_SENTINEL_DIGEST
    )
    assert receipt["rename_recovery"] == "database-oid-bound-resumable-v1"


def test_database_state_parser_is_closed_and_requires_a_primary_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_psql", lambda **_kwargs: "101|202")
    assert runtime._database_oids(container="database", hidden=()) == (101, 202)

    for output in ("", "|", "not-an-oid|202", "101|202|303", "0|202"):
        monkeypatch.setattr(runtime, "_psql", lambda **_kwargs: output)
        with pytest.raises(
            runtime.RuntimeDatabaseError,
            match="database_state_invalid|propertyquarry_database_missing",
        ):
            runtime._database_oids(container="database", hidden=())


def _execute_with_database_state(
    *,
    operation: str,
    database_oids: tuple[int | None, int | None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, str]], list[tuple[str, int, bool]]]:
    values = _runtime_values()
    psql_calls: list[tuple[str, str]] = []
    sentinel_calls: list[tuple[str, int, bool]] = []
    receipts: list[dict[str, object]] = []

    monkeypatch.setattr(
        runtime,
        "_load_or_create_values",
        lambda **_kwargs: (values, True, False),
    )
    monkeypatch.setattr(runtime, "_database_oids", lambda **_kwargs: database_oids)
    monkeypatch.setattr(
        runtime,
        "_check_sentinel",
        lambda *, database, database_oid, install, **_kwargs: sentinel_calls.append(
            (database, database_oid, install)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_psql",
        lambda *, database, sql, **_kwargs: psql_calls.append((database, sql)) or "",
    )
    monkeypatch.setattr(
        runtime,
        "_receipt",
        lambda **kwargs: {"status": "pass", "operation": kwargs["operation"]},
    )
    monkeypatch.setattr(
        runtime,
        "_write_receipt",
        lambda _path, payload: receipts.append(dict(payload)),
    )
    args = argparse.Namespace(
        operation=operation,
        database_container=runtime.DEFAULT_DATABASE_CONTAINER,
        env_file=tmp_path / "runtime.env",
        admission_env_file=tmp_path / "admission.env",
        receipt=tmp_path / "receipt.json",
    )

    result = runtime.execute(args)

    assert result == {"status": "pass", "operation": operation}
    assert receipts == [result]
    return psql_calls, sentinel_calls


def test_prepare_installs_sentinel_on_existing_target_without_renaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psql_calls, sentinel_calls = _execute_with_database_state(
        operation="prepare",
        database_oids=(606, 707),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert [database for database, _sql in psql_calls] == ["template1"]
    assert sentinel_calls == [(runtime.TARGET_DATABASE, 707, True)]


@pytest.mark.parametrize("operation", ("activate", "rename-forward"))
def test_activation_is_sentinel_bound_and_oid_bound_before_legacy_rename(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psql_calls, sentinel_calls = _execute_with_database_state(
        operation=operation,
        database_oids=(707, None),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert [database for database, _sql in psql_calls] == [
        "template1",
        "template1",
        runtime.TARGET_DATABASE,
    ]
    assert sentinel_calls == [
        (runtime.LEGACY_DATABASE, 707, False),
        (runtime.TARGET_DATABASE, 707, False),
    ]
    assert "oid = 707::OID" in psql_calls[1][1]


def test_activation_resumes_configuration_when_rename_already_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psql_calls, sentinel_calls = _execute_with_database_state(
        operation="activate",
        database_oids=(None, 808),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert [database for database, _sql in psql_calls] == [
        "template1",
        runtime.TARGET_DATABASE,
    ]
    assert sentinel_calls == [(runtime.TARGET_DATABASE, 808, False)]


def test_finalize_and_rename_back_require_the_bound_target_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalize_calls, finalize_sentinels = _execute_with_database_state(
        operation="finalize",
        database_oids=(None, 909),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert [database for database, _sql in finalize_calls] == [
        "template1",
        runtime.TARGET_DATABASE,
    ]
    assert finalize_sentinels == [(runtime.TARGET_DATABASE, 909, False)]

    rename_calls, rename_sentinels = _execute_with_database_state(
        operation="rename-back",
        database_oids=(None, 909),
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert [database for database, _sql in rename_calls] == [
        "template1",
        "template1",
    ]
    assert rename_sentinels == [
        (runtime.TARGET_DATABASE, 909, False),
        (runtime.LEGACY_DATABASE, 909, False),
    ]


def test_runtime_database_provisioner_on_disposable_postgres(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("PROPERTYQUARRY_RUN_PROVISIONER_POSTGRES_INTEGRATION") != "1":
        pytest.skip("disposable provisioner PostgreSQL integration is opt-in")

    image = (
        "postgres:16-alpine@sha256:"
        "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
    )
    container = f"propertyquarry-provisioner-it-{uuid4().hex[:16]}"
    postgres_password = "integration-postgres-password"
    start_environment = dict(os.environ)
    start_environment["POSTGRES_PASSWORD"] = postgres_password
    subprocess.run(
        (
            "/usr/bin/docker",
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=268435456",
            "--shm-size=64m",
            "--env",
            "POSTGRES_PASSWORD",
            image,
        ),
        check=True,
        capture_output=True,
        text=True,
        env=start_environment,
    )
    try:
        consecutive_ready = 0
        for _attempt in range(200):
            ready = subprocess.run(
                (
                    "/usr/bin/docker",
                    "exec",
                    container,
                    "psql",
                    "--no-psqlrc",
                    "--username=postgres",
                    "--dbname=postgres",
                    "--tuples-only",
                    "--command=SELECT 1",
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0:
                consecutive_ready += 1
                if consecutive_ready >= 5:
                    break
            else:
                consecutive_ready = 0
            time.sleep(0.2)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")

        published_port = subprocess.run(
            ("/usr/bin/docker", "port", container, "5432/tcp"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        port_match = re.fullmatch(r"127\.0\.0\.1:(?P<port>[0-9]+)", published_port)
        assert port_match is not None
        database_port = int(port_match.group("port"))

        admission._psql(
            container=container,
            database="template1",
            password=PASSWORD_B,
            sql=admission._role_sql(PASSWORD_B),
        )
        runtime._psql(
            container=container,
            database="template1",
            hidden=(),
            sql=f"""
CREATE ROLE propertyquarry_admission_excess_owner NOLOGIN;
GRANT propertyquarry_admission_excess_owner TO {admission.OWNER_ROLE};
""",
        )
        with pytest.raises(
            admission.ProvisioningError,
            match="unsafe admission owner memberships",
        ):
            admission._psql(
                container=container,
                database="template1",
                password=PASSWORD_B,
                sql=admission._role_sql(PASSWORD_B),
            )
        runtime._psql(
            container=container,
            database="template1",
            hidden=(),
            sql=f"""
REVOKE propertyquarry_admission_excess_owner FROM {admission.OWNER_ROLE};
DROP ROLE propertyquarry_admission_excess_owner;
CREATE ROLE propertyquarry_admission_excess_capacity NOLOGIN;
GRANT propertyquarry_admission_excess_capacity
    TO {admission.CAPACITY_OWNER_ROLE};
""",
        )
        with pytest.raises(
            admission.ProvisioningError,
            match="unsafe admission capacity owner memberships",
        ):
            admission._psql(
                container=container,
                database="template1",
                password=PASSWORD_B,
                sql=admission._role_sql(PASSWORD_B),
            )
        runtime._psql(
            container=container,
            database="template1",
            hidden=(),
            sql=f"""
REVOKE propertyquarry_admission_excess_capacity
    FROM {admission.CAPACITY_OWNER_ROLE};
DROP ROLE propertyquarry_admission_excess_capacity;
CREATE DATABASE {runtime.ADMISSION_DATABASE}
    OWNER postgres TEMPLATE template0 ENCODING 'UTF8';
""",
        )
        runtime._psql(
            container=container,
            database=runtime.LEGACY_DATABASE,
            hidden=(),
            sql="CREATE TABLE public.property_search_runs (run_id TEXT PRIMARY KEY);",
        )

        admission_env = tmp_path / "admission.env"
        runtime_env = tmp_path / "runtime.env"
        receipt = tmp_path / "receipt.json"
        _write_mode_0600(
            admission_env,
            f"{runtime.ADMISSION_KEY}={runtime._admission_url(password=PASSWORD_B)}\n",
        )

        def execute(operation: str) -> dict[str, object]:
            return runtime.execute(
                argparse.Namespace(
                    operation=operation,
                    database_container=container,
                    env_file=runtime_env,
                    admission_env_file=admission_env,
                    receipt=receipt,
                )
            )

        assert execute("prepare")["status"] == "pass"
        assert execute("activate")["status"] == "pass"

        runtime._psql(
            container=container,
            database=runtime.TARGET_DATABASE,
            hidden=(),
            sql="DROP TABLE public.property_search_runs;",
        )
        prepared_values = runtime._read_regular_env(runtime_env)
        principal_id = "propertyquarry-provisioner-principal"
        runtime._psql(
            container=container,
            database=runtime.TARGET_DATABASE,
            hidden=(),
            sql=f"""
BEGIN;
SET LOCAL ROLE {runtime.OWNER_ROLE};
CREATE TABLE public.operator_profiles (
    operator_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    roles_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    skill_tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    trust_tier TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO public.operator_profiles (
    operator_id,
    principal_id,
    display_name
) VALUES (
    'propertyquarry-provisioner-operator',
    '{principal_id}',
    'Legacy ACL Probe Operator'
);
COMMIT;
""",
        )
        migrator_url = prepared_values["PROPERTYQUARRY_MIGRATION_DATABASE_URL"].replace(
            f"@{runtime.DATABASE_HOST}:5432/",
            f"@127.0.0.1:{database_port}/",
            1,
        )
        from app.product.propertyquarry_schema import migrate_propertyquarry_schema

        migration_result = migrate_propertyquarry_schema(
            migrator_url,
            applied_by="provisioner-integration",
        )
        assert (
            migration_result["kernel"]["current_version"]
            == LATEST_KERNEL_SCHEMA_VERSION
        )
        assert migration_result["property_search"]["current_version"] > 0
        repeated_migration_result = migrate_propertyquarry_schema(
            migrator_url,
            applied_by="provisioner-integration-repeat",
        )
        assert repeated_migration_result["kernel"]["applied_versions"] == []
        assert repeated_migration_result["property_search"]["applied_versions"] == []

        runtime._psql(
            container=container,
            database=runtime.TARGET_DATABASE,
            hidden=(),
            sql=f"""
BEGIN;
ALTER FUNCTION public.{runtime.ADMISSION_CAPACITY_FUNCTIONS[0]}()
    OWNER TO {runtime.ADMISSION_CAPACITY_OWNER_ROLE};
ALTER FUNCTION public.{runtime.ADMISSION_CAPACITY_FUNCTIONS[1]}()
    OWNER TO {runtime.ADMISSION_CAPACITY_OWNER_ROLE};
ALTER FUNCTION public.{runtime.ADMISSION_CAPACITY_FUNCTIONS[2]}()
    OWNER TO {runtime.ADMISSION_CAPACITY_OWNER_ROLE};
COMMIT;
""",
        )

        assert execute("finalize")["status"] == "pass"
        authority = runtime._psql(
            container=container,
            database=runtime.TARGET_DATABASE,
            hidden=(),
            sql=f"""
SELECT pg_catalog.concat_ws('|',
    (SELECT pg_catalog.count(*) = 3
     FROM pg_catalog.pg_proc AS routine
     JOIN pg_catalog.pg_namespace AS namespace_row
       ON namespace_row.oid = routine.pronamespace
     JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = routine.proowner
     WHERE namespace_row.nspname = 'public'
       AND routine.proname IN (
           '{runtime.ADMISSION_CAPACITY_FUNCTIONS[0]}',
           '{runtime.ADMISSION_CAPACITY_FUNCTIONS[1]}',
           '{runtime.ADMISSION_CAPACITY_FUNCTIONS[2]}'
       )
       AND routine.prosecdef
       AND owner_role.rolname = '{runtime.ADMISSION_CAPACITY_OWNER_ROLE}'),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}',
        'public.ea_kernel_schema_migrations',
        'SELECT'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}',
        'public.propertyquarry_schema_migrations',
        'UPDATE'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}',
        'public.propertyquarry_admission_capacity_state',
        'SELECT'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}',
        'public.{runtime.ACTIVATION_SENTINEL_TABLE}',
        'SELECT'
    ),
    pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}', 'public.property_content_job_events', 'INSERT'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}', 'public.property_content_job_events', 'UPDATE'
    ),
    pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}', 'public.property_search_work_jobs', 'INSERT'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.API_ROLE}', 'public.property_search_work_jobs', 'UPDATE'
    ),
    pg_catalog.has_table_privilege(
        '{runtime.WORKER_ROLE}', 'public.property_search_work_jobs', 'UPDATE'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.WORKER_ROLE}', 'public.property_search_work_jobs', 'INSERT'
    ),
    pg_catalog.has_table_privilege(
        '{runtime.SCHEDULER_ROLE}', 'public.property_search_work_jobs', 'SELECT'
    ),
    NOT pg_catalog.has_table_privilege(
        '{runtime.SCHEDULER_ROLE}', 'public.property_search_work_jobs', 'UPDATE'
    ),
    NOT pg_catalog.has_function_privilege(
        '{runtime.API_ROLE}',
        'public.{runtime.ADMISSION_CAPACITY_FUNCTIONS[0]}()',
        'EXECUTE'
    ),
    (SELECT NOT role_row.rolsuper
     FROM pg_catalog.pg_roles AS role_row
     WHERE role_row.rolname = '{runtime.ADMISSION_RUNTIME_ROLE}'),
    (SELECT pg_catalog.array_agg(
                granted_role.rolname::TEXT ORDER BY granted_role.rolname
            )
        = ARRAY['{runtime.ADMISSION_CAPACITY_OWNER_ROLE}']::TEXT[]
     FROM pg_catalog.pg_auth_members AS membership
     JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
     JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
     WHERE member_role.rolname = '{runtime.OWNER_ROLE}'),
    (SELECT pg_catalog.array_agg(
                granted_role.rolname::TEXT ORDER BY granted_role.rolname
            )
        = ARRAY['{runtime.OWNER_ROLE}']::TEXT[]
     FROM pg_catalog.pg_auth_members AS membership
     JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
     JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
     WHERE member_role.rolname = '{runtime.MIGRATOR_ROLE}'),
    NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE member_role.rolname IN (
            '{runtime.API_ROLE}',
            '{runtime.WORKER_ROLE}',
            '{runtime.SCHEDULER_ROLE}'
        )
    )
);
""",
        )
        assert authority.splitlines()[-1] == "|".join(("t",) * 18)

        kernel_relations = tuple(
            sorted(required_kernel_relations(load_kernel_migrations()))
        )
        relation_values = ",\n".join(f"('{table}')" for table in kernel_relations)
        role_values = ",\n".join(f"('{role}')" for role in runtime.RUNTIME_ROLES)
        live_acl = runtime._psql(
            container=container,
            database=runtime.TARGET_DATABASE,
            hidden=(),
            sql=f"""
SELECT runtime_role.role_name || '|' || relation_name.table_name || '|' ||
       pg_catalog.concat_ws(',',
           CASE WHEN pg_catalog.has_table_privilege(
               runtime_role.role_name,
               pg_catalog.format('public.%I', relation_name.table_name),
               'SELECT'
           ) THEN 'SELECT' END,
           CASE WHEN pg_catalog.has_table_privilege(
               runtime_role.role_name,
               pg_catalog.format('public.%I', relation_name.table_name),
               'INSERT'
           ) THEN 'INSERT' END,
           CASE WHEN pg_catalog.has_table_privilege(
               runtime_role.role_name,
               pg_catalog.format('public.%I', relation_name.table_name),
               'UPDATE'
           ) THEN 'UPDATE' END,
           CASE WHEN pg_catalog.has_table_privilege(
               runtime_role.role_name,
               pg_catalog.format('public.%I', relation_name.table_name),
               'DELETE'
           ) THEN 'DELETE' END
       )
FROM (VALUES {role_values}) AS runtime_role(role_name)
CROSS JOIN (VALUES {relation_values}) AS relation_name(table_name)
ORDER BY runtime_role.role_name, relation_name.table_name;
""",
        )
        expected_acl = {
            (role, table): set()
            for role in runtime.RUNTIME_ROLES
            for table in kernel_relations
        }
        for role, grant_groups in runtime.KERNEL_ROLE_TABLE_GRANTS.items():
            for privileges, tables in grant_groups:
                for table in tables:
                    expected_acl[(role, table)] = set(privileges.split(", "))
        observed_acl: dict[tuple[str, str], set[str]] = {}
        for row in live_acl.splitlines():
            role, table, privileges = row.split("|", 2)
            observed_acl[(role, table)] = set(filter(None, privileges.split(",")))
        assert observed_acl == expected_acl

        api_url = prepared_values["PROPERTYQUARRY_API_DATABASE_URL"].replace(
            f"@{runtime.DATABASE_HOST}:5432/",
            f"@127.0.0.1:{database_port}/",
            1,
        )
        api_token = "propertyquarry-provisioner-api-probe"
        for key, value in {
            "DATABASE_URL": api_url,
            "EA_RUNTIME_MODE": "prod",
            "EA_ROLE": "api",
            "EA_STORAGE_BACKEND": "postgres",
            "EA_API_TOKEN": api_token,
            "EA_SIGNING_SECRET": "S" * 64,
            "EA_PROVIDER_SECRET_KEY": "P" * 64,
            "EA_DEFAULT_PRINCIPAL_ID": principal_id,
            "EA_ALLOW_LOOPBACK_NO_AUTH": "0",
            "EA_ALLOW_NON_PROPERTYQUARRY_EMAIL_SENDER": "1",
            runtime.ERASURE_KEY: prepared_values[runtime.ERASURE_KEY],
        }.items():
            monkeypatch.setenv(key, value)
        for key in (
            "EA_CF_ACCESS_TEAM_DOMAIN",
            "EA_CF_ACCESS_AUD",
            "EA_CF_ACCESS_CERTS_URL",
        ):
            monkeypatch.delenv(key, raising=False)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.dependencies import RequestContext, get_request_context
        from app.api.routes.human import router as human_router
        from app.api.routes.onboarding import router as onboarding_router
        from app.api.routes.providers import router as providers_router
        from app.container import build_container
        from app.domain.models import Artifact
        from app.settings import get_settings

        app_container = build_container(settings=get_settings())
        assert app_container.runtime_profile.mode == "prod"
        assert app_container.runtime_profile.storage_backend == "postgres"
        adopted_operator = app_container.orchestrator.fetch_operator_profile(
            "propertyquarry-provisioner-operator",
            principal_id=principal_id,
        )
        assert adopted_operator is not None
        assert adopted_operator.display_name == "Legacy ACL Probe Operator"

        request_app = FastAPI()
        request_app.state.container = app_container
        request_app.dependency_overrides[get_request_context] = lambda: RequestContext(
            principal_id=principal_id,
            authenticated=True,
            auth_source="cloudflare_access",
        )
        request_app.include_router(onboarding_router)
        request_app.include_router(providers_router)
        request_app.include_router(human_router)
        request_headers = {"x-ea-api-token": api_token}
        with TestClient(request_app) as client:
            onboarding_response = client.post(
                "/v1/onboarding/start",
                headers=request_headers,
                json={
                    "workspace_name": "ACL probe",
                    "workspace_mode": "personal",
                    "region": "AT",
                    "language": "en",
                    "timezone": "Europe/Vienna",
                    "selected_channels": [],
                },
            )
            assert onboarding_response.status_code == 200, onboarding_response.text

            provider_response = client.post(
                "/v1/providers/bindings",
                headers=request_headers,
                json={
                    "provider_key": "openai",
                    "status": "enabled",
                    "priority": 10,
                },
            )
            assert provider_response.status_code == 200, provider_response.text
            binding_id = str(provider_response.json()["binding_id"])
            update_response = client.post(
                f"/v1/providers/bindings/{binding_id}/status",
                headers=request_headers,
                json={"status": "disabled"},
            )
            assert update_response.status_code == 200, update_response.text

            human_response = client.post(
                "/v1/human/tasks",
                headers=request_headers,
                json={
                    "session_id": "acl-probe-missing-session",
                    "task_type": "property_fact_repair",
                    "role_required": "property_researcher",
                    "brief": "Resolve a missing supermarket distance.",
                },
            )
            assert human_response.status_code == 200, human_response.text
            human_payload = human_response.json()
            assert human_payload["principal_id"] == principal_id
            assert human_payload["session_id"] != "acl-probe-missing-session"

        app_container.preference_profiles.ensure_profile(
            principal_id=principal_id,
            display_name="ACL Probe",
        )
        preference = app_container.preference_profiles.upsert_preference_node(
            principal_id=principal_id,
            domain="property",
            category="amenities",
            key="supermarket_distance",
            value_json={"max_minutes": 10},
            strength="high",
            confidence=1.0,
        )
        assert preference["principal_id"] == principal_id

        app_container.task_contracts.upsert_contract(
            task_key="acl_probe",
            deliverable_type="property_fact",
            default_risk_class="low",
            default_approval_class="none",
        )
        app_container.tool_runtime.upsert_tool(
            tool_name="acl_probe_tool",
            version="v1",
        )
        artifact = Artifact(
            artifact_id="acl-probe-artifact",
            kind="property_fact",
            content="Nearest supermarket: 7 minutes walking.",
            execution_session_id=str(human_payload["session_id"]),
            principal_id=principal_id,
            structured_output_json={
                "format": "evidence_pack",
                "claims": ["Nearest supermarket is seven minutes away."],
                "evidence_refs": ["acl-probe:supermarket-distance"],
                "confidence": 0.95,
            },
        )
        app_container.orchestrator._artifacts.save(artifact)
        persisted_artifact = app_container.orchestrator.fetch_artifact(
            artifact.artifact_id
        )
        assert persisted_artifact is not None
        assert persisted_artifact.artifact_id == artifact.artifact_id
        assert persisted_artifact.content == artifact.content
        assert persisted_artifact.principal_id == principal_id
        evidence = app_container.evidence_runtime.record_artifact(artifact)
        assert evidence is not None
        assert evidence.artifact_id == artifact.artifact_id

        operator_id = "propertyquarry-provisioner-operator"
        operator_profile = app_container.orchestrator.upsert_operator_profile(
            principal_id=principal_id,
            operator_id=operator_id,
            display_name="ACL Probe Operator",
            roles=("property_researcher",),
        )
        assert operator_profile.operator_id == operator_id
        assert operator_profile.display_name == "ACL Probe Operator"

        def build_role_container(*, role: str, env_key: str):  # type: ignore[no-untyped-def]
            role_url = prepared_values[env_key].replace(
                f"@{runtime.DATABASE_HOST}:5432/",
                f"@127.0.0.1:{database_port}/",
                1,
            )
            monkeypatch.setenv("EA_ROLE", role)
            monkeypatch.setenv("DATABASE_URL", role_url)
            return build_container(settings=get_settings())

        worker_container = build_role_container(
            role="worker",
            env_key="PROPERTYQUARRY_WORKER_DATABASE_URL",
        )
        assert worker_container.provider_registry.list_persisted_binding_records(
            principal_id=principal_id
        )
        worker_task = worker_container.orchestrator.create_human_task(
            session_id="acl-probe-worker-missing-session",
            principal_id=principal_id,
            task_type="property_fact_repair",
            role_required="property_researcher",
            brief="Worker repair handoff.",
        )
        assert worker_task.session_id != "acl-probe-worker-missing-session"

        scheduler_container = build_role_container(
            role="scheduler",
            env_key="PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
        )
        assert scheduler_container.provider_registry.list_persisted_binding_records(
            principal_id=principal_id
        )
        scheduler_task = scheduler_container.orchestrator.create_human_task(
            session_id="acl-probe-scheduler-missing-session",
            principal_id=principal_id,
            task_type="property_tour_repair",
            role_required="property_researcher",
            brief="Scheduler tour repair handoff.",
        )
        assigned_task = scheduler_container.orchestrator.assign_human_task(
            scheduler_task.human_task_id,
            principal_id=principal_id,
            operator_id=operator_id,
            assignment_source="scheduler",
        )
        assert assigned_task.assigned_operator_id == operator_id
        returned_task = scheduler_container.orchestrator.return_human_task(
            scheduler_task.human_task_id,
            principal_id=principal_id,
            operator_id=operator_id,
            resolution="resolved",
            returned_payload_json={"status": "verified"},
        )
        assert returned_task.status == "returned"

        deleted_binding = (
            app_container.provider_registry.delete_persisted_binding_record(
                binding_id=binding_id,
                principal_id=principal_id,
            )
        )
        assert deleted_binding is not None

        # A crash after the non-transactional rename is recoverable: activate
        # observes the target OID/sentinel and only reapplies transactional ACLs.
        assert execute("activate")["status"] == "pass"
        assert execute("rename-back")["status"] == "pass"
        assert execute("rename-back")["status"] == "pass"
    finally:
        subprocess.run(
            ("/usr/bin/docker", "rm", "--force", container),
            check=False,
            capture_output=True,
            text=True,
        )


def test_compose_database_contract_names_database_in_bootstrap_and_healthcheck() -> (
    None
):
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  propertyquarry-db:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        compose,
    )
    assert match is not None
    database_service = match.group("body")

    assert database_service.count('POSTGRES_DB: "propertyquarry"') == 1
    assert (
        database_service.count(
            'test: ["CMD-SHELL", "pg_isready -U postgres -d propertyquarry"]'
        )
        == 1
    )
    assert 'test: ["CMD-SHELL", "pg_isready -U postgres"]' not in database_service


def test_release_asset_inventory_contains_database_provisioning_contract() -> None:
    verifier = (ROOT / "scripts/verify_release_assets.sh").read_text(encoding="utf-8")

    for path in (
        "scripts/provision_propertyquarry_admission_database.py",
        "scripts/provision_propertyquarry_auth_env.py",
        "scripts/provision_propertyquarry_runtime_database.py",
        "tests/test_propertyquarry_auth_env_provisioner.py",
        "tests/test_propertyquarry_database_provisioners.py",
    ):
        assert verifier.count(f'  "{path}"') == 1
