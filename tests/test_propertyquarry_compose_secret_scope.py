from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.property.yml"
THREEDVISTA_SECRET_KEYS = (
    "THREEDVISTA_LOGIN_EMAIL",
    "THREEDVISTA_LOGIN_PASSWORD",
    "THREEDVISTA_LICENSE_EMAIL",
)
LONG_LIVED_SERVICES = (
    "propertyquarry-api",
    "propertyquarry-worker",
    "propertyquarry-scheduler",
    "propertyquarry-render-tools",
)
SERVICE_DSN_INPUTS = {
    "propertyquarry-api": "PROPERTYQUARRY_API_DATABASE_URL",
    "propertyquarry-worker": "PROPERTYQUARRY_WORKER_DATABASE_URL",
    "propertyquarry-scheduler": "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
    "propertyquarry-render-tools": "PROPERTYQUARRY_RENDER_DATABASE_URL",
    "propertyquarry-migrate": "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
}
DATABASE_SECRET_INPUTS = frozenset(SERVICE_DSN_INPUTS.values()) | {
    "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
    "PROPERTYQUARRY_API_INGRESS_DATABASE_URL",
}


def _compose_payload() -> dict[str, object]:
    payload = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert type(payload) is dict
    return payload


def test_long_lived_property_services_do_not_inherit_3dvista_login_secrets() -> None:
    payload = _compose_payload()
    services = dict(payload.get("services") or {})

    for service_name in LONG_LIVED_SERVICES:
        environment = dict(services[service_name].get("environment") or {})
        assert {
            key: environment.get(key)
            for key in THREEDVISTA_SECRET_KEYS
        } == {key: "" for key in THREEDVISTA_SECRET_KEYS}


def test_compose_maps_each_database_secret_to_only_its_service_lane() -> None:
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    payload = _compose_payload()
    services = dict(payload.get("services") or {})

    assert "${DATABASE_URL" not in source
    assert "postgresql://postgres:" not in source
    for service_name, dsn_input in SERVICE_DSN_INPUTS.items():
        environment = dict(services[service_name].get("environment") or {})
        database_expression = str(environment.get("DATABASE_URL") or "")
        assert database_expression.startswith(f"${{{dsn_input}:?")
        for other_input in SERVICE_DSN_INPUTS.values():
            if other_input != dsn_input:
                assert other_input not in database_expression

    api_environment = dict(
        services["propertyquarry-api"].get("environment") or {}
    )
    assert api_environment["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"].startswith(
        "${PROPERTYQUARRY_API_ADMISSION_DATABASE_URL:?"
    )
    assert api_environment["PROPERTYQUARRY_API_INGRESS_DATABASE_URL"].startswith(
        "${PROPERTYQUARRY_API_INGRESS_DATABASE_URL:?"
    )
    assert api_environment["PROPERTYQUARRY_ADMISSION_BACKEND"] == "postgres"
    assert services["propertyquarry-migrate"]["restart"] == "no"


def test_long_lived_services_do_not_load_the_broad_dotenv_or_migration_dsn() -> None:
    payload = _compose_payload()
    services = dict(payload.get("services") or {})

    for service_name in LONG_LIVED_SERVICES:
        env_file = list(services[service_name].get("env_file") or [])
        paths = {
            str(item.get("path") or "") if isinstance(item, dict) else str(item)
            for item in env_file
        }
        assert ".env" not in paths
        environment_text = yaml.safe_dump(
            dict(services[service_name].get("environment") or {}),
            sort_keys=True,
        )
        environment = dict(services[service_name].get("environment") or {})
        if service_name in {"propertyquarry-api", "propertyquarry-scheduler"}:
            assert environment["PROPERTYQUARRY_MIGRATION_DATABASE_URL"] == ""
            assert environment["POSTGRES_PASSWORD"] == ""
            for dsn_input in DATABASE_SECRET_INPUTS:
                if (
                    service_name == "propertyquarry-api"
                    and dsn_input
                    in {
                        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
                        "PROPERTYQUARRY_API_INGRESS_DATABASE_URL",
                    }
                ):
                    continue
                assert environment.get(dsn_input) == ""
        else:
            assert "PROPERTYQUARRY_MIGRATION_DATABASE_URL" not in environment_text
            assert "POSTGRES_PASSWORD" not in environment_text


def test_docker_compose_config_resolves_explicit_nonsecret_placeholders(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI is unavailable")

    empty_env_file = tmp_path / "empty-compose.env"
    empty_env_file.write_text("", encoding="utf-8")
    onemin_key_manifest = tmp_path / "onemin-api-keys.json"
    onemin_key_manifest.write_text("{}\n", encoding="utf-8")
    role_urls = {
        "PROPERTYQUARRY_API_DATABASE_URL": (
            "postgresql://pq_api:review-only@db/property"
        ),
            "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": (
                "postgresql://pq_api_admission:review-only@db/property"
            ),
            "PROPERTYQUARRY_API_INGRESS_DATABASE_URL": (
                "postgresql://pq_api_ingress:review-only@db/property"
            ),
        "PROPERTYQUARRY_WORKER_DATABASE_URL": (
            "postgresql://pq_worker:review-only@db/property"
        ),
        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL": (
            "postgresql://pq_scheduler:review-only@db/property"
        ),
        "PROPERTYQUARRY_RENDER_DATABASE_URL": (
            "postgresql://pq_render_admission:review-only@db/property"
        ),
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL": (
            "postgresql://pq_migration:review-only@db/property"
        ),
    }
    identity_placeholders = {
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID": (
            "review-only-google-client-id-placeholder"
        ),
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET": (
            "review-only-google-client-secret-placeholder"
        ),
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET": (
            "review-only-google-state-secret-placeholder"
        ),
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET": (
            "review-only-identity-session-secret-placeholder"
        ),
    }
    command_env = {
        **os.environ,
        **role_urls,
        **identity_placeholders,
        "DATABASE_URL": "postgresql://generic-forbidden:review-only@db/property",
        "POSTGRES_PASSWORD": "review-only-bootstrap-placeholder",
        "EA_SIGNING_SECRET": "review-only-signing-placeholder",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN": (
            "review-only-bridge-placeholder"
        ),
        "ONEMIN_DIRECT_API_KEYS_JSON_FILE": str(onemin_key_manifest),
    }
    completed = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(empty_env_file),
            "-f",
            str(COMPOSE_PATH),
            "config",
        ],
        cwd=COMPOSE_PATH.parent,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    rendered = yaml.safe_load(completed.stdout)
    assert type(rendered) is dict
    services = dict(rendered.get("services") or {})
    for service_name, dsn_input in SERVICE_DSN_INPUTS.items():
        environment = dict(services[service_name].get("environment") or {})
        assert environment["DATABASE_URL"] == role_urls[dsn_input]

    for service_name in LONG_LIVED_SERVICES:
        environment = dict(services[service_name].get("environment") or {})
        assert environment.get("DATABASE_URL") != role_urls[
            "PROPERTYQUARRY_MIGRATION_DATABASE_URL"
        ]
        assert environment.get("POSTGRES_PASSWORD", "") == ""
        assert environment.get("PROPERTYQUARRY_MIGRATION_DATABASE_URL", "") == ""
    assert services["propertyquarry-api"]["environment"][
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
    ] == role_urls["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
    api_environment = dict(services["propertyquarry-api"]["environment"])
    assert {
        key: api_environment.get(key)
        for key in identity_placeholders
    } == identity_placeholders
    assert api_environment["PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET"] not in {
        command_env["EA_SIGNING_SECRET"],
        api_environment["PROPERTYQUARRY_IDENTITY_SESSION_SECRET"],
    }
