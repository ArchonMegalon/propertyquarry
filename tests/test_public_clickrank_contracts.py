from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from app.services.public_clickrank import (
    clickrank_head_snippet,
    clickrank_route_allowed,
    clickrank_site_id_for_hostname,
    request_hostname,
)
from app.services.public_analytics_consent import ANALYTICS_CONSENT_CSRF_COOKIE


_PROPERTYQUARRY_CLICKRANK_SITE_ID = "33ff8f39-6213-4903-99d7-81048b5b3e1f"
_PROPERTYQUARRY_RYBBIT_SITE_ID = "rybbit-property-site"


def _client(*, principal_id: str = "exec-clickrank-contract", clickrank_enabled: bool = True) -> TestClient:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = ""
    if clickrank_enabled:
        os.environ["EA_ENABLE_CLICKRANK"] = "1"
        os.environ["CLICKRANK_AI_PROPERTYQUARRY_SITE_ID"] = _PROPERTYQUARRY_CLICKRANK_SITE_ID
    else:
        os.environ.pop("EA_ENABLE_CLICKRANK", None)
        os.environ.pop("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", None)
    os.environ.pop("EA_ENABLE_PUBLIC_SIDE_SURFACES", None)
    os.environ.pop("EA_ENABLE_PUBLIC_RESULTS", None)
    os.environ.pop("EA_ENABLE_PUBLIC_TOURS", None)
    os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
    os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    client.headers.update({"X-EA-Principal-ID": principal_id})
    return client


def _grant_public_analytics(client: TestClient, *, host: str) -> None:
    initial = client.get("/", headers={"host": host})
    csrf_token = str(client.cookies.get(ANALYTICS_CONSENT_CSRF_COOKIE) or "")
    assert initial.status_code == 200
    assert csrf_token
    response = client.post(
        "/privacy/analytics-consent",
        data={"csrf_token": csrf_token, "decision": "granted", "return_to": "/"},
        headers={"host": host, "origin": f"http://{host}"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_request_hostname_prefers_forwarded_host_over_host_and_url_host() -> None:
    request = SimpleNamespace(
        headers={
            "x-forwarded-host": "propertyquarry.com",
            "host": "internal-ea-host:443",
        },
        url=SimpleNamespace(hostname="internal-ea-host"),
    )

    assert request_hostname(request) == "propertyquarry.com"


def test_request_hostname_uses_public_base_host_for_proxied_opaque_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    request = SimpleNamespace(
        headers={
            "host": "opaque-origin.internal",
            "x-forwarded-for": "198.51.100.42",
        },
        url=SimpleNamespace(hostname="opaque-origin.internal"),
    )

    assert request_hostname(request) == "propertyquarry.com"


def test_request_hostname_keeps_unknown_public_host_for_proxied_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    request = SimpleNamespace(
        headers={
            "host": "propertyquarry.com",
            "cf-ray": "ray-id",
        },
        url=SimpleNamespace(hostname="propertyquarry.com"),
    )

    assert request_hostname(request) == "propertyquarry.com"


def test_clickrank_site_id_for_hostname_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", "configured-site-id")
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", raising=False)

    assert clickrank_site_id_for_hostname("propertyquarry.com") == "configured-site-id"


def test_clickrank_site_id_for_hostname_rejects_invalid_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", 'bad/site"id')

    assert clickrank_site_id_for_hostname("propertyquarry.com") == ""
    assert clickrank_head_snippet("propertyquarry.com", "/", consent_granted=True) == ""


def test_clickrank_site_id_for_hostname_falls_back_to_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_CLICKRANK_SITE_ID)
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")

    assert clickrank_site_id_for_hostname("internal-ea-host") == _PROPERTYQUARRY_CLICKRANK_SITE_ID


def test_clickrank_site_id_for_hostname_does_not_fallback_for_unknown_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_CLICKRANK_SITE_ID)
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")

    assert clickrank_site_id_for_hostname("example.com") == ""


def test_clickrank_head_snippet_returns_empty_for_unknown_host_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.delenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", raising=False)

    assert clickrank_head_snippet("example.com", "/") == ""


def test_clickrank_route_allowlist_blocks_private_and_generated_routes() -> None:
    assert clickrank_route_allowed("/") is True
    assert clickrank_route_allowed("/guides/wohnung-kaufen-wien-checkliste") is True
    assert clickrank_route_allowed("/app/properties") is False
    assert clickrank_route_allowed("/results/demo") is False
    assert clickrank_route_allowed("/tours/demo") is False
    assert clickrank_route_allowed("/memorials/manfred") is False
    assert clickrank_route_allowed("") is False


def test_public_landing_includes_clickrank_snippet_for_propertyquarry_host() -> None:
    client = _client()
    _grant_public_analytics(client, host="propertyquarry.com")

    response = client.get("/", headers={"host": "propertyquarry.com"})

    assert response.status_code == 200
    assert f"https://js.clickrank.ai/seo/{_PROPERTYQUARRY_CLICKRANK_SITE_ID}/script?" in response.text


def test_public_landing_includes_clickrank_snippet_for_www_propertyquarry_host() -> None:
    client = _client()
    _grant_public_analytics(client, host="www.propertyquarry.com")

    response = client.get("/", headers={"host": "www.propertyquarry.com"})

    assert response.status_code == 200
    assert f"https://js.clickrank.ai/seo/{_PROPERTYQUARRY_CLICKRANK_SITE_ID}/script?" in response.text


def test_public_landing_never_emits_propertyquarry_analytics_for_myexternalbrain_host() -> None:
    client = _client()

    response = client.get("/", headers={"host": "myexternalbrain.com"})

    assert response.status_code == 200
    assert "clickrank.ai" not in response.text
    assert "rybbit.io" not in response.text


def test_public_landing_omits_clickrank_snippet_for_localhost() -> None:
    client = _client()

    response = client.get("/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "clickrank.ai" not in response.text


def test_public_landing_omits_clickrank_snippet_for_unknown_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_CLICKRANK_SITE_ID)
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    client = _client()

    response = client.get("/", headers={"host": "example.com"})

    assert response.status_code == 200
    assert "clickrank.ai" not in response.text


def test_public_landing_omits_clickrank_snippet_when_not_explicitly_enabled() -> None:
    client = _client(clickrank_enabled=False, principal_id="exec-clickrank-disabled")

    response = client.get("/", headers={"host": "propertyquarry.com"})

    assert response.status_code == 200
    assert "clickrank.ai" not in response.text


def test_clickrank_head_snippet_can_include_rybbit_for_propertyquarry_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_RYBBIT", "1")
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_RENDER_IN_CLICKRANK", "1")
    monkeypatch.setenv("RYBBIT_IO_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_RYBBIT_SITE_ID)
    monkeypatch.delenv("EA_ENABLE_CLICKRANK", raising=False)

    snippet = clickrank_head_snippet("propertyquarry.com", "/", consent_granted=True)

    assert 'https://app.rybbit.io/api/script.js' in snippet
    assert f'data-site-id="{_PROPERTYQUARRY_RYBBIT_SITE_ID}"' in snippet
    assert "clickrank.ai" not in snippet


def test_clickrank_head_snippet_can_include_optional_rybbit_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_RYBBIT", "1")
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_RENDER_IN_CLICKRANK", "1")
    monkeypatch.setenv("RYBBIT_IO_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_RYBBIT_SITE_ID)
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_TAG", "propertyquarry-public")
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_DEBOUNCE", "750")
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_SKIP_PATTERNS", '["/health","/ready"]')
    monkeypatch.setenv("EA_PUBLIC_RYBBIT_MASK_PATTERNS", '["token","session"]')

    snippet = clickrank_head_snippet("propertyquarry.com", "/", consent_granted=True)

    assert 'data-tag="propertyquarry-public"' in snippet
    assert 'data-debounce="750"' in snippet
    assert 'data-skip-patterns="[&quot;/health&quot;,&quot;/ready&quot;]"' in snippet
    assert 'data-mask-patterns="[&quot;token&quot;,&quot;session&quot;]"' in snippet


def test_public_landing_can_include_clickrank_and_rybbit_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", _PROPERTYQUARRY_CLICKRANK_SITE_ID)
    monkeypatch.setenv("EA_ENABLE_RYBBIT", "1")
    monkeypatch.setenv("RYBBIT_IO_PROPERTYQUARRY_SITE_ID", "rybbit-property-site")
    monkeypatch.delenv("EA_PUBLIC_RYBBIT_RENDER_IN_CLICKRANK", raising=False)
    client = _client()
    _grant_public_analytics(client, host="propertyquarry.com")

    response = client.get("/", headers={"host": "propertyquarry.com"})

    assert response.status_code == 200
    assert f"https://js.clickrank.ai/seo/{_PROPERTYQUARRY_CLICKRANK_SITE_ID}/script?" in response.text
    assert response.text.count('https://app.rybbit.io/api/script.js') == 1
    assert 'data-site-id="rybbit-property-site"' in response.text
