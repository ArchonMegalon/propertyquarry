from __future__ import annotations

import re

from starlette.requests import Request

from app.api.routes import landing as landing_routes
from tests.product_test_helpers import build_property_client, start_workspace


def test_propertyquarry_public_route_shell_is_localized_without_native_review_claim() -> None:
    client = build_property_client(principal_id="pq-ui-locale-contract")
    client.headers.pop("X-EA-Principal-ID", None)

    response = client.get(
        "/?lang=de-AT",
        headers={
            "host": "propertyquarry.com",
            "accept-language": "de-AT,de;q=0.9,en;q=0.8",
        },
    )

    assert response.status_code == 200
    assert re.search(r'<html\b[^>]*\blang="de-AT"', response.text)
    assert response.headers["content-language"] == "de-AT"
    assert response.headers["x-propertyquarry-translation-status"].endswith(
        "independent-native-review-required"
    )
    assert 'hreflang="x-default"' in response.text
    assert 'data-pq-professional-review="false"' in response.text


def test_propertyquarry_route_shell_preserves_explicit_fallback_boundaries() -> None:
    client = build_property_client(principal_id="pq-ui-locale-console")
    start_workspace(client, mode="personal", workspace_name="Locale contract")

    response = client.get(
        "/app/search?lang=es-CR",
        headers={"host": "propertyquarry.com", "accept-language": "es-CR,es;q=0.9"},
    )

    assert response.status_code == 200
    html_tag = re.search(r"<html\b[^>]*>", response.text, re.IGNORECASE)
    assert html_tag is not None
    assert re.search(r'\blang="es-CR"', html_tag.group(0))
    assert re.search(r'\bdir="ltr"', html_tag.group(0))
    assert response.headers["content-language"] == "es-CR"
    assert response.headers["x-propertyquarry-translation-status"] == (
        "global-route-shell; english-fallback-unreviewed-legal-provider-customer-content; "
        "independent-native-review-required"
    )
    assert (
        'data-pq-english-fallback="unreviewed-legal provider-specific '
        'customer-or-listing-content"'
    ) in response.text
    assert 'data-pq-professional-review="false"' in response.text
    assert "Los textos legales, de proveedores y aún no traducidos permanecen en inglés." in response.text


def test_propertyquarry_anonymous_home_uses_supported_browser_language_coherently() -> None:
    client = build_property_client(principal_id="pq-ui-locale-default")
    client.headers.pop("X-EA-Principal-ID", None)

    response = client.get(
        "/",
        headers={
            "host": "propertyquarry.com",
            "accept-language": "de-DE,de;q=0.9,en;q=0.8",
        },
    )

    assert response.status_code == 200
    assert re.search(r'<html\b[^>]*\blang="de-DE"', response.text)
    assert response.headers["content-language"] == "de-DE"
    assert ">Zum Inhalt springen</a>" in response.text
    assert "<span>Übereinstimmung</span>" in response.text
    assert ">Immobilie öffnen</a>" in response.text
    assert ">Skip to content</a>" not in response.text
    assert "<span>Match</span>" not in response.text
    assert ">Open property</a>" not in response.text


def test_propertyquarry_locale_resolver_uses_only_governed_complete_ui_locales() -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"ui_locale=de",
            "headers": [
                (b"host", b"propertyquarry.com"),
                (b"accept-language", b"de,en;q=0.8"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("propertyquarry.com", 443),
        }
    )

    context = landing_routes._propertyquarry_ui_locale_context(request)

    assert context == {
        "ui_locale": "en",
        "ui_locale_requested": "de",
        "ui_locale_fallback": True,
        "supported_ui_locales": ["en"],
        "document_language": "en",
        "document_direction": "ltr",
    }
