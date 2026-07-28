from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.property.yml"
THREEDVISTA_SECRET_KEYS = (
    "THREEDVISTA_LOGIN_EMAIL",
    "THREEDVISTA_LOGIN_PASSWORD",
    "THREEDVISTA_LICENSE_EMAIL",
)


def test_long_lived_property_services_do_not_inherit_3dvista_login_secrets() -> None:
    payload = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = dict(payload.get("services") or {})

    for service_name in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
    ):
        environment = dict(services[service_name].get("environment") or {})
        assert {
            key: environment.get(key)
            for key in THREEDVISTA_SECRET_KEYS
        } == {key: "" for key in THREEDVISTA_SECRET_KEYS}


def test_reconstruction_render_bridge_does_not_inherit_blanket_secret_files() -> None:
    payload = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = dict(payload.get("services") or {})
    render_service = dict(services["propertyquarry-render-tools"])
    environment = dict(render_service.get("environment") or {})

    assert "env_file" not in render_service
    assert environment["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"]
    assert environment["DATABASE_URL"] == ""
    assert environment["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] == ""
