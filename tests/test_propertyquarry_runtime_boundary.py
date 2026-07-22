from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.app import create_app


def _configure_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_ALLOW_LOOPBACK_NO_AUTH", "1")
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_HOSTS", "propertyquarry.com")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_MEMORIALS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.delenv("EA_API_TOKEN", raising=False)
    monkeypatch.delenv("EA_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EA_CF_ACCESS_AUD", raising=False)


def _route_paths(app) -> set[str]:  # type: ignore[no-untyped-def]
    return {str(getattr(route, "path", "")) for route in app.routes}


def test_propertyquarry_runtime_profile_mounts_only_property_product_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_PROFILE", "propertyquarry")

    app = create_app()
    paths = _route_paths(app)

    assert {
        "/static",
        "/health",
        "/health/ready",
        "/version",
        "/sign-in",
        "/sign-in/email-link",
        "/sign-in/google",
        "/google/callback",
        "/v1/register/start",
        "/app/search",
        "/app/properties",
        "/app/shortlist",
        "/app/research/{candidate_ref}",
        "/app/api/signals/property/search/run",
        "/app/api/signals/property/search/run/{run_id}/candidates/{candidate_ref}/fact-enrichment",
        "/app/api/signals/willhaben/property-tour",
        "/app/api/property/governed-spatial/tours/{slug}/status",
        "/tours/{slug}",
    }.issubset(paths)

    assert {
        "/app/{section}",
        "/app/api/brief",
        "/app/api/queue",
        "/app/people",
        "/admin",
        "/admin/{section}",
        "/api/v1/images/generations",
        "/demo/brief",
        "/memorials/{slug}",
        "/openapi.json",
        "/results/{slug}",
        "/setup",
        "/v1/memory/items",
        "/v1/providers/bindings",
        "/v1/rewrite/artifact",
    }.isdisjoint(paths)

    client = TestClient(app, base_url="https://propertyquarry.com")
    assert client.get("/sign-in").status_code == 200
    assert client.get("/app/queue").status_code == 404
    assert client.get("/app/api/brief").status_code == 404
    assert client.get("/v1/memory/items").status_code == 404
    assert client.get("/admin").status_code == 404


def test_generic_runtime_keeps_existing_routes_when_profile_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.delenv("PROPERTYQUARRY_RUNTIME_PROFILE", raising=False)

    paths = _route_paths(create_app())

    assert {
        "/app/{section}",
        "/app/api/brief",
        "/app/people",
        "/admin",
        "/api/v1/images/generations",
        "/memorials/{slug}",
        "/openapi.json",
        "/results/{slug}",
        "/v1/memory/items",
        "/v1/providers/bindings",
        "/v1/rewrite/artifact",
    }.issubset(paths)


def test_propertyquarry_runtime_profile_rejects_unknown_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_PROFILE", "propertyquarry-plus-generic")

    with pytest.raises(RuntimeError, match="must be unset or 'propertyquarry'"):
        create_app()


def test_propertyquarry_compose_enables_profile_for_api_only() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.property.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = dict(compose["services"])

    assert (
        services["propertyquarry-api"]["environment"]["PROPERTYQUARRY_RUNTIME_PROFILE"]
        == "propertyquarry"
    )
    for service_name in (
        "propertyquarry-migrate",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
    ):
        assert (
            "PROPERTYQUARRY_RUNTIME_PROFILE"
            not in services[service_name]["environment"]
        )
