from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MAIL_KEYS = {
    "EMAILIT_API_KEY",
    "EA_REGISTRATION_EMAIL_FROM",
    "EA_REGISTRATION_EMAIL_NAME",
    "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
    "EA_REGISTRATION_EMAIL_NAME_FALLBACK",
    "EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
    "EA_EMAIL_DEFAULT_FROM",
    "EA_EMAIL_DEFAULT_NAME",
}
GOOGLE_KEYS = {
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
    "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
    "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
    "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
}


def _compose() -> dict[str, object]:
    loaded = yaml.safe_load(
        (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_identity_api_does_not_receive_the_broad_render_provider_bundle() -> None:
    services = _compose()["services"]
    api = services["propertyquarry-api"]
    scheduler = services["propertyquarry-scheduler"]
    render = services["propertyquarry-render-tools"]

    assert not api.get("env_file")
    assert not scheduler.get("env_file")
    assert not render.get("env_file")


def test_registration_and_google_identity_authority_is_api_only() -> None:
    services = _compose()["services"]
    api_environment = services["propertyquarry-api"]["environment"]

    assert MAIL_KEYS | GOOGLE_KEYS <= set(api_environment)
    for service_name in (
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
        "propertyquarry-migrate",
        "propertyquarry-db",
    ):
        environment = services[service_name].get("environment") or {}
        assert not (MAIL_KEYS | GOOGLE_KEYS) & set(environment)


def test_live_container_names_and_api_health_dependency_are_explicit() -> None:
    services = _compose()["services"]
    assert services["propertyquarry-api"]["container_name"].endswith(
        ":-propertyquarry-api-live}"
    )
    assert services["propertyquarry-worker"]["container_name"].endswith(
        ":-propertyquarry-worker-live}"
    )
    assert services["propertyquarry-scheduler"]["container_name"].endswith(
        ":-propertyquarry-scheduler-live}"
    )
    assert services["propertyquarry-render-tools"]["container_name"].endswith(
        ":-propertyquarry-render-live}"
    )
    assert services["propertyquarry-migrate"]["container_name"].endswith(
        ":-propertyquarry-migrate-live}"
    )
    healthcheck = services["propertyquarry-api"]["healthcheck"]
    assert healthcheck["test"][0:2] == ["CMD", "/usr/local/bin/python"]
    assert "/health/ready" in healthcheck["test"][-1]
    cloudflared = yaml.safe_load(
        (ROOT / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")
    )
    assert (
        cloudflared["services"]["propertyquarry-cloudflared"]["container_name"]
        == "propertyquarry-cloudflared-live"
    )
    assert cloudflared["services"]["propertyquarry-cloudflared"]["image"] == (
        "${PROPERTYQUARRY_CLOUDFLARED_IMAGE_SHA256:-"
        "cloudflare/cloudflared@sha256:"
        "18626b1baac4450214535cd5bc40ef44c0635244d585ebf707749c22b6f3408f}"
    )
