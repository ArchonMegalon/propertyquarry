from __future__ import annotations

import os
from pathlib import Path

import yaml

from scripts import provision_propertyquarry_runtime_database as runtime_database
from scripts import smoke_property_postgres_isolated as isolated_harness


ROOT = Path(__file__).resolve().parents[1]


def test_isolated_dependency_snapshot_matches_the_candidate_lock_and_ci_pins() -> None:
    def _locked(relative: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in (ROOT / relative).read_text(encoding="utf-8").splitlines():
            name, separator, version = line.partition("==")
            if separator:
                result[name.lower()] = version
        return result

    production_locked = _locked("ea/requirements.lock")
    ci_locked = _locked("ea/requirements.ci.lock")
    assert production_locked.keys().isdisjoint(ci_locked)
    locked = {**production_locked, **ci_locked}

    for name, version in isolated_harness.DEPENDENCY_PROFILE:
        normalized = name.lower()
        assert version == locked[normalized]
    assert locked["packaging"] == "26.2"
    assert locked["pygments"] == "2.20.0"
    assert locked["pytest"] == "9.0.3"
    assert locked["httpcore"] == "1.0.9"
    assert locked["httpx"] == "0.28.1"
    assert locked["iniconfig"] == "2.3.0"
    assert locked["pluggy"] == "1.6.0"
    for ci_only in (
        "attrs",
        "httpcore",
        "httpx",
        "iniconfig",
        "jsonschema",
        "jsonschema-specifications",
        "packaging",
        "pluggy",
        "pygments",
        "pytest",
        "referencing",
        "rpds-py",
    ):
        assert ci_locked[ci_only] == locked[ci_only]
        assert ci_only not in production_locked
    assert {
        name: locked[name]
        for name in (
            "attrs",
            "jsonschema",
            "jsonschema-specifications",
            "referencing",
            "rpds-py",
            "typing-extensions",
        )
    } == {
        "attrs": "26.1.0",
        "jsonschema": "4.25.1",
        "jsonschema-specifications": "2025.9.1",
        "referencing": "0.37.0",
        "rpds-py": "2026.6.3",
        "typing-extensions": "4.15.0",
    }
    assert ("packaging", locked["packaging"]) in isolated_harness.DEPENDENCY_PROFILE
    assert ("Pygments", locked["pygments"]) in isolated_harness.DEPENDENCY_PROFILE


def test_isolated_runtime_executes_the_authenticated_overlay_before_the_venv(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "candidate"
    (repo_root / "ea").mkdir(parents=True)
    temp_root = tmp_path / "run"
    temp_root.mkdir()
    overlay_base = tmp_path / "overlay"
    overlay_site = (
        overlay_base
        / "lib"
        / f"python{isolated_harness.sys.version_info.major}."
        f"{isolated_harness.sys.version_info.minor}"
        / "site-packages"
    )
    overlay_site.mkdir(parents=True)

    environment = isolated_harness._runtime_environment(
        repo_root=repo_root,
        temp_root=temp_root,
        database_url="postgresql://postgres:secret@127.0.0.1:15432/postgres",
        admission_database_url=(
            "postgresql://propertyquarry_api_admission:secret@127.0.0.1:15432/postgres"
        ),
        api_token="api-token",
        signing_secret="signing-secret",
        erasure_secret="erasure-secret",
        chromium_headless_shell="/safe/chromium-headless-shell",
        dependency_overlay_base=overlay_base,
    )

    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(repo_root / "ea"),
        str(overlay_site),
    ]
    assert environment["PYTHONUSERBASE"] == str(overlay_base)


def test_disposable_capacity_owner_sql_is_exact_idempotent_and_fail_closed() -> None:
    source = (
        ROOT / "scripts" / "propertyquarry_disposable_capacity_owner.sql"
    ).read_text(encoding="utf-8")

    assert source.count("CREATE ROLE propertyquarry_admission_capacity_owner") == 1
    for posture in (
        "NOLOGIN",
        "NOINHERIT",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert posture in source
    assert "IF NOT FOUND THEN" in source
    assert "pg_catalog.pg_auth_members" in source
    assert source.count("membership.member = role.oid") == 2
    assert source.count("membership.roleid = role.oid") == 2
    assert "ERRCODE = '42501'" in source
    assert "ALTER ROLE" not in source
    assert "DROP ROLE" not in source


def test_disposable_runtime_roles_sql_is_exact_idempotent_and_fail_closed() -> None:
    source = (
        ROOT / "scripts" / "propertyquarry_disposable_runtime_roles.sql"
    ).read_text(encoding="utf-8")

    expected_roles = (
        "propertyquarry_api",
        "propertyquarry_scheduler",
        "propertyquarry_worker",
    )
    assert set(expected_roles) == set(runtime_database.RUNTIME_ROLES)
    for role_name in expected_roles:
        assert source.count(f"'{role_name}'") == 2
    for posture in (
        "NOLOGIN",
        "NOINHERIT",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert posture in source
    assert "IF NOT FOUND THEN" in source
    assert "pg_catalog.pg_auth_members" in source
    assert source.count("membership.member = role.oid") == 2
    assert source.count("membership.roleid = role.oid") == 2
    assert "ERRCODE = '42501'" in source
    assert "ALTER ROLE" not in source
    assert "DROP ROLE" not in source


def test_every_legacy_disposable_migration_lane_runs_the_same_role_bootstrap() -> None:
    for relative in (
        "scripts/smoke_postgres.sh",
        "scripts/test_postgres_contracts.sh",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert source.count("propertyquarry_disposable_capacity_owner.sql") == 1
        assert source.count('f|f|f|f|f|f|f|0') == 1
        assert "--no-psqlrc --quiet --tuples-only --no-align" in source


def test_postgres_smoke_provisions_runtime_roles_before_full_schema_migration() -> None:
    source = (ROOT / "scripts" / "smoke_postgres.sh").read_text(encoding="utf-8")

    role_bootstrap = "propertyquarry_disposable_runtime_roles.sql"
    full_migration = "python -m app.product.propertyquarry_schema migrate"
    assert source.count(role_bootstrap) == 1
    assert source.count(full_migration) == 1
    assert source.index(role_bootstrap) < source.index(full_migration)
    assert "python -m app.product.property_search_schema migrate" not in source


def test_postgres_browser_ci_uses_only_the_isolated_host_capped_harness() -> None:
    workflow = (ROOT / ".github" / "workflows" / "smoke-runtime.yml").read_text(
        encoding="utf-8"
    )
    job = workflow.split("  propertyquarry-postgres-browser-e2e:\n", 1)[1].split(
        "\n  propertyquarry-continuous-ux:", 1
    )[0]

    assert "scripts/smoke_property_postgres_isolated.py" in job
    assert "scripts/smoke_property_postgres.sh" not in job
    assert "--systemd-run /usr/bin/systemd-run" in job
    assert "--docker-binary /usr/bin/docker" in job
    assert "--chromium-headless-shell" in job
    assert "--venv" in job
    assert "docker pull \"${postgres_image}\"" in job
    assert "pip install --ignore-installed" in job
    assert "pip install --user --ignore-installed" in job
    assert job.count("-c ea/requirements.ci.lock") == 2
    assert 'dependency_userbase="$(mktemp -d "${RUNNER_TEMP}/propertyquarry-postgres-browser-userbase.XXXXXXXX")"' in job
    assert 'chmod 0700 "${dependency_userbase}"' in job
    assert 'PYTHONUSERBASE="${dependency_userbase}" \\' in job
    assert "python -m venv --system-site-packages" not in job
    assert 'python -m venv "${venv}"' in job
    assert "printf '%s=%s\\n' PYTHONUSERBASE \"${dependency_userbase}\"" in job
    assert "pytest==9.0.3" in job
    assert "pytest==9.0.2" not in job
    assert 'sudo systemctl start "user@${runner_uid}.service"' in job
    assert 'test -S "${user_runtime_dir}/bus"' in job
    assert "DBUS_SESSION_BUS_ADDRESS" in job
    assert "propertyquarry-postgres-browser-preflight-" in job
    for host_limit in (
        "--property=MemoryMax=1073741824",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=128",
        "--property=CPUQuota=100%",
        "--property=RuntimeMaxSec=1200s",
    ):
        assert host_limit in job
    assert "--with-deps chromium" in job


def test_smoke_runtime_api_uses_the_fully_constrained_jsonschema_closure() -> None:
    workflow = (ROOT / ".github" / "workflows" / "smoke-runtime.yml").read_text(
        encoding="utf-8"
    )
    job = workflow.split("  smoke-runtime-api:\n", 1)[1].split(
        "\n  smoke-runtime-postgres:", 1
    )[0]

    assert "jsonschema==4.25.1" in job
    assert "--constraint ea/requirements.lock" in job or "-c ea/requirements.lock" in job
    assert "--constraint ea/requirements.ci.lock" in job or "-c ea/requirements.ci.lock" in job
    assert "jsonschema>=" not in job
    assert "jsonschema~=" not in job


def test_every_ordinary_ci_test_install_uses_the_separate_ci_constraints_lock() -> None:
    workflow = (ROOT / ".github" / "workflows" / "smoke-runtime.yml").read_text(
        encoding="utf-8"
    )
    jobs = yaml.safe_load(workflow)["jobs"]
    for job_name in (
        "smoke-runtime-api",
        "propertyquarry-browser-contracts",
        "product-browser-e2e",
        "propertyquarry-postgres-browser-e2e",
        "propertyquarry-continuous-ux",
        "propertyquarry-accessibility-contracts",
        "propertyquarry-failure-state-contracts",
        "propertyquarry-activation-contracts",
        "postgres-runtime-contracts",
    ):
        run_source = "\n".join(
            str(step.get("run") or "") for step in jobs[job_name].get("steps", [])
        )
        assert "-c ea/requirements.lock" in run_source
        assert "-c ea/requirements.ci.lock" in run_source

    production_image_lock = (ROOT / "ea" / "requirements.lock").read_text(
        encoding="utf-8"
    )
    assert "pytest==" not in production_image_lock
    assert "jsonschema==" not in production_image_lock
    assert "httpx==" not in production_image_lock
