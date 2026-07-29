from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from app.api.propertyquarry_localization import (
    PROPERTYQUARRY_MAX_LOCALIZED_HTML_BYTES,
    PROPERTYQUARRY_PSEUDO_LOCALE,
    PROPERTYQUARRY_PUBLIC_LOCALES,
    PROPERTYQUARRY_PUBLIC_ORIGIN,
    PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES,
    PropertyQuarryLocalizationMiddleware,
    localize_propertyquarry_html,
    normalize_propertyquarry_locale,
    propertyquarry_locale_cookie_header,
    propertyquarry_route_is_translated,
    propertyquarry_required_route_translation_status,
    propertyquarry_translation,
    propertyquarry_translation_coverage,
    resolve_propertyquarry_locale,
)
from tests.product_test_helpers import build_property_client, start_workspace


ROOT = Path(__file__).resolve().parents[1]


def _scope(
    *,
    scheme: str = "http",
    query_string: bytes = b"lang=de-DE",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": "/app/search",
        "raw_path": b"/app/search",
        "query_string": query_string,
        "headers": headers or [(b"host", b"propertyquarry.com")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


def _run_middleware(
    response_messages: list[Message],
    *,
    scope: Scope | None = None,
    max_html_bytes: int = PROPERTYQUARRY_MAX_LOCALIZED_HTML_BYTES,
) -> list[Message]:
    emitted: list[Message] = []

    async def upstream(_scope: Scope, _receive, send) -> None:  # type: ignore[no-untyped-def]
        for message in response_messages:
            await send(dict(message))

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        emitted.append(message)

    middleware = PropertyQuarryLocalizationMiddleware(upstream, max_html_bytes=max_html_bytes)
    asyncio.run(middleware(scope or _scope(), receive, send))
    return emitted


def _response_header_values(messages: list[Message], name: str) -> list[str]:
    encoded_name = name.casefold().encode("ascii")
    start = next(message for message in messages if message["type"] == "http.response.start")
    return [
        value.decode("latin-1")
        for key, value in start.get("headers", [])
        if key.lower() == encoded_name
    ]


def _propertyquarry_document(body: str = "<p>Search</p>") -> bytes:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta name="application-name" content="PropertyQuarry">'
        "<title>PropertyQuarry Search</title></head>"
        f'<body><a class="skip" href="#main">Skip to content</a><main id="main">{body}</main></body></html>'
    ).encode("utf-8")


def test_locale_resolution_is_allowlisted_and_uses_explicit_cookie_then_language_priority() -> None:
    explicit = resolve_propertyquarry_locale(
        query_string=b"lang=de_AT",
        headers=[
            (b"cookie", b"pq_locale=es-CR"),
            (b"accept-language", b"de-DE;q=0.9, es-CR;q=0.8"),
        ],
    )
    assert explicit.locale == "de-AT"
    assert explicit.source == "query"
    assert explicit.query_locale_valid is True

    invalid_query = resolve_propertyquarry_locale(
        query_string=b"lang=%0d%0aSet-Cookie%3Aunsafe%3D1",
        headers=[(b"cookie", b"pq_locale=es-CR")],
    )
    assert invalid_query.locale == "es-CR"
    assert invalid_query.source == "cookie"
    assert invalid_query.query_locale_rejected is True

    accepted = resolve_propertyquarry_locale(
        headers=[(b"accept-language", b"de-DE;q=0.4, es-CR;q=0.9, de-AT;q=0.7")],
    )
    assert accepted.locale == "es-CR"
    assert accepted.source == "accept-language"
    assert normalize_propertyquarry_locale("qps-ploc") is None
    assert normalize_propertyquarry_locale("qps-ploc", allow_pseudo=True) == PROPERTYQUARRY_PSEUDO_LOCALE


def test_accept_language_rejects_invalid_or_overprecise_q_values_instead_of_clamping() -> None:
    resolved = resolve_propertyquarry_locale(
        headers=[
            (
                b"accept-language",
                b"de-DE;q=2, de-AT;q=NaN, en;q=.9, es-CR;q=0.600",
            )
        ],
    )
    assert resolved.locale == "es-CR"

    duplicate = resolve_propertyquarry_locale(
        headers=[(b"accept-language", b"de-DE;q=0.9;q=0.8, es-CR;q=0.1234")],
    )
    assert duplicate.locale == "en"
    assert duplicate.source == "default"

    valid_boundary = resolve_propertyquarry_locale(
        headers=[(b"accept-language", b"de-AT;q=1.000, es-CR;q=0.999")],
    )
    assert valid_boundary.locale == "de-AT"


def test_locale_cookie_is_host_only_http_only_lax_and_secure_on_https() -> None:
    secure_cookie = propertyquarry_locale_cookie_header("de-DE", secure=True)
    assert secure_cookie.startswith("pq_locale=de-DE;")
    assert "Path=/" in secure_cookie
    assert "Max-Age=15552000" in secure_cookie
    assert "HttpOnly" in secure_cookie
    assert "SameSite=Lax" in secure_cookie
    assert "Secure" in secure_cookie
    assert "Domain=" not in secure_cookie

    local_cookie = propertyquarry_locale_cookie_header("es-CR", secure=False)
    assert "Secure" not in local_cookie


def test_terminal_result_shell_localizes_dynamic_counts_facts_and_actions() -> None:
    source = _propertyquarry_document(
        """
        <a>Saved pages</a><a>Edit search</a><h2>At a glance</h2>
        <h2>Next step</h2><h2>Premium next step</h2><strong>Best so far</strong>
        <span>40 matching homes</span><span>2 rooms | 80.0 m2</span>
        <span>Fit 56</span><span>Monthly total EUR 690</span>
        <p>It is meaningfully cheaper.</p><p>Tour not available yet.</p>
        <a>Open listing</a><section aria-label="Selected property">
        <span>Media preview not available.</span><span>Best part</span>
        <span>Keep in mind</span><span>Price level</span><span>Not available yet.</span>
        <button>Adjust search</button><button>Viewing requested</button>
        <button>Documents requested</button><button>Offer candidate</button>
        <button>Archived</button><h2>Premium market report</h2>
        <a>Open checkout</a>
        <button aria-label="Review Helle Gartenwohnung">Review</button>
        <button aria-label="Open area map for Helle Gartenwohnung">Map</button>
        <span>Missing 360 pending, address, heating</span>
        <small>EUR 49 | Use only if Helle Gartenwohnung needs wider market context before deciding.</small>
        </section>
        """
    ).decode("utf-8")

    german = localize_propertyquarry_html(
        source,
        locale="de-AT",
        path="/app/properties",
        query_string=b"",
    )
    for expected in (
        "Gespeicherte Seiten",
        "Suche bearbeiten",
        "Auf einen Blick",
        "Nächster Schritt",
        "Premium-Nächster Schritt",
        "Bisher am besten",
        "40 passende Immobilien",
        "2 Zimmer | 80.0 m2",
        "Eignung 56",
        "Monatlich gesamt EUR 690",
        "Deutlich günstiger.",
        "Noch keine Tour verfügbar.",
        "Angebot öffnen",
        'aria-label="Ausgewählte Immobilie"',
        "Medienvorschau noch nicht verfügbar.",
        "Größter Vorteil",
        "Zu beachten",
        "Preisniveau",
        "Noch nicht verfügbar.",
        "Suche anpassen",
        "Besichtigung angefragt",
        "Unterlagen angefragt",
        "Kaufangebot erwägen",
        "Archiviert",
        "Premium-Marktbericht",
        "Zur Kasse",
        'aria-label="Prüfen: Helle Gartenwohnung"',
        'aria-label="Gebietskarte öffnen: Helle Gartenwohnung"',
        "Fehlend: 360°-Ansicht ausstehend, Adresse, Heizung",
        "EUR 49 | Nur verwenden, wenn Helle Gartenwohnung vor der Entscheidung einen breiteren Marktkontext benötigt.",
    ):
        assert expected in german

    spanish = localize_propertyquarry_html(
        source,
        locale="es-CR",
        path="/app/properties",
        query_string=b"",
    )
    for expected in (
        "Páginas guardadas",
        "Editar búsqueda",
        "De un vistazo",
        "Siguiente paso",
        "Siguiente paso premium",
        "La mejor hasta ahora",
        "40 propiedades adecuadas",
        "2 habitaciones | 80.0 m2",
        "Compatibilidad 56",
        "Total mensual EUR 690",
        "Es considerablemente más económica.",
        "El recorrido aún no está disponible.",
        "Abrir anuncio",
        'aria-label="Propiedad seleccionada"',
        "La vista previa multimedia aún no está disponible.",
        "Punto fuerte",
        "Tenga en cuenta",
        "Nivel de precio",
        "Aún no disponible.",
        "Ajustar búsqueda",
        "Visita solicitada",
        "Documentos solicitados",
        "Candidata para oferta",
        "Archivada",
        "Informe de mercado premium",
        "Ir al pago",
        'aria-label="Revisar: Helle Gartenwohnung"',
        'aria-label="Abrir mapa de la zona: Helle Gartenwohnung"',
        "Falta: vista 360 pendiente, dirección, calefacción",
        "EUR 49 | Úselo solo si Helle Gartenwohnung necesita un contexto de mercado más amplio antes de decidir.",
    ):
        assert expected in spanish


def test_all_public_catalogs_cover_the_global_route_shell_without_review_claim() -> None:
    for locale in PROPERTYQUARRY_PUBLIC_LOCALES:
        coverage = propertyquarry_translation_coverage(locale)
        assert coverage["missing_critical_messages"] == []
        assert coverage["critical_translated_messages"] == coverage["critical_source_messages"]
        assert coverage["coverage_scope"] == "global_required_route_shell"
        assert coverage["required_customer_route_count"] == 39
        assert coverage["localized_route_shell_count"] == 32
        assert coverage["blocked_required_routes"] == [
            "/cookies",
            "/disclaimers",
            "/imprint",
            "/privacy",
            "/refunds",
            "/subprocessors",
            "/terms",
        ]
        assert coverage["localized_indexable_route_count"] == 8
        assert coverage["english_fallback_scopes"] == [
            "unreviewed_legal_source",
            "provider_specific",
            "customer_or_listing_content",
        ]
        assert coverage["professional_review"] is False
        assert coverage["native_launch_ready"] is False


def test_global_experience_route_inventory_is_exact_and_fail_closed_for_legal_copy() -> None:
    experience_contract = json.loads(
        (ROOT / "config/monitoring/propertyquarry_global_experience.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert tuple(experience_contract["required_customer_routes"]) == (
        PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES
    )
    assert len(PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES) == 39
    assert len(set(PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES)) == 39
    legal_routes = {
        "/privacy",
        "/terms",
        "/cookies",
        "/subprocessors",
        "/refunds",
        "/disclaimers",
        "/imprint",
    }
    for route_template in PROPERTYQUARRY_REQUIRED_CUSTOMER_ROUTE_TEMPLATES:
        route = (
            route_template.replace("{candidate_ref}", "candidate-ref")
            .replace("{run_id}", "run-id")
            .replace("{slug}", "tour-slug")
        )
        status = propertyquarry_required_route_translation_status(route)
        if route_template in legal_routes:
            assert status == "blocked_unreviewed_legal_source"
            assert propertyquarry_route_is_translated(route) is False
        else:
            assert status == "localized_route_shell_pending_native_review"
            assert propertyquarry_route_is_translated(route) is True


def test_indexable_localized_routes_emit_self_canonical_and_reciprocal_hreflang() -> None:
    source = """<!doctype html><html lang="en"><head>
    <meta name="application-name" content="PropertyQuarry">
    <title>PropertyQuarry Pricing</title>
    <meta name="description" content="English description">
    <link rel="canonical" href="https://propertyquarry.com/pricing">
    <meta property="og:title" content="PropertyQuarry Pricing">
    <meta property="og:description" content="English description">
    <meta property="og:url" content="https://propertyquarry.com/pricing">
    <meta name="twitter:title" content="PropertyQuarry Pricing">
    <meta name="twitter:description" content="English description">
    </head><body><a href="/docs">Research</a></body></html>"""

    for locale in PROPERTYQUARRY_PUBLIC_LOCALES:
        localized = localize_propertyquarry_html(
            source,
            locale=locale,
            path="/pricing",
            query_string=f"utm_source=ignored&lang={locale}".encode(),
        )
        canonical = f"{PROPERTYQUARRY_PUBLIC_ORIGIN}/pricing"
        if locale != "en":
            canonical += f"?lang={locale}"
        assert localized.count('rel="canonical"') == 1
        assert f'rel="canonical" href="{canonical}"' in localized
        assert localized.count('rel="alternate"') == 5
        for target_locale in PROPERTYQUARRY_PUBLIC_LOCALES:
            target = f"{PROPERTYQUARRY_PUBLIC_ORIGIN}/pricing"
            if target_locale != "en":
                target += f"?lang={target_locale}"
            assert (
                f'hreflang="{target_locale}" href="{target}"'
                in localized
            )
        assert (
            'hreflang="x-default" '
            f'href="{PROPERTYQUARRY_PUBLIC_ORIGIN}/pricing"'
        ) in localized
        assert "utm_source" not in localized.split("</head>", 1)[0]
        if locale != "en":
            assert "English description" not in localized
            assert f'href="/docs?lang={locale}"' in localized


def test_html_localization_preserves_scripts_external_urls_and_english_fallback_copy() -> None:
    source = """<!doctype html>
    <html lang="en"><head><title>PropertyQuarry Search</title>
    <script>window.shellLabel = "Search";</script></head><body>
    <a href="/app/shortlist?run_id=run-7#results" aria-label="Search">Search</a>
    <form action="/app/properties"><button title="Launch search">Launch search</button></form>
    <a href="https://provider.example/legal?lang=en&amp;version=2">Provider legal terms</a>
    <p>Provider legal terms and listing disclosures remain in English.</p>
    </body></html>"""

    localized = localize_propertyquarry_html(
        source,
        locale="de-AT",
        path="/app/search",
        query_string=b"lang=de-AT&run_id=run-7&access_token=do-not-reflect",
    )

    assert '<html lang="de-AT">' in localized
    assert "PropertyQuarry Suche" in localized
    assert '>Suche</a>' in localized
    assert 'href="/app/shortlist?run_id=run-7&amp;lang=de-AT#results"' in localized
    assert 'action="/app/properties?lang=de-AT"' in localized
    assert 'aria-label="Suche"' in localized
    assert 'title="Suche starten"' in localized
    assert 'window.shellLabel = "Search";' in localized
    assert 'href="https://provider.example/legal?lang=en&amp;version=2"' in localized
    assert "Provider legal terms and listing disclosures remain in English." in localized
    assert "do-not-reflect" not in localized
    assert (
        'data-pq-english-fallback="unreviewed-legal provider-specific '
        'customer-or-listing-content"'
    ) in localized
    assert 'data-pq-professional-review="false"' in localized
    assert 'data-pq-localization-placement="floating"' in localized
    assert "nicht professionell geprüft" in localized
    assert 'rel="alternate"' not in localized
    for hreflang in PROPERTYQUARRY_PUBLIC_LOCALES:
        assert f'hreflang="{hreflang}"' in localized
    assert 'hreflang="x-default"' not in localized

    english = localize_propertyquarry_html(
        source,
        locale="en",
        path="/app/search",
        preserve_locale_in_urls=False,
    )
    assert 'href="https://provider.example/legal?lang=en&amp;version=2"' in english
    assert "&amp;amp;" not in english


def test_localization_uses_the_template_slot_without_adding_document_flow() -> None:
    source = (
        '<!doctype html><html lang="en"><head><title>PropertyQuarry Search</title></head>'
        '<body><header><span data-pq-localization-slot></span></header><main>Search</main></body></html>'
    )

    localized = localize_propertyquarry_html(
        source,
        locale="de-DE",
        path="/app/properties",
        query_string=b"lang=de-DE",
    )

    assert '<span data-pq-localization-slot></span>' not in localized
    assert localized.count("data-pq-localization-status") == 1
    assert 'data-pq-localization-placement="integrated"' in localized
    assert localized.index("data-pq-localization-status") < localized.index("</header>")
    assert '<details class="pq-locale-disclosure">' in localized
    assert 'aria-current="true"' in localized


def test_localization_protects_verbatim_blocks_requires_app_boundary_and_keeps_skip_link_first() -> None:
    protected_blocks = (
        "<script>Search</script>"
        "<style>Search</style>"
        "<pre>Search</pre>"
        "<code>Search</code>"
        "<textarea>Search</textarea>"
        "<template><p>Search</p></template>"
        "<svg><text>Search</text></svg>"
    )
    source = (
        '<html lang="en"><head><title>PropertyQuarry Search</title></head><body>'
        '<a class="skip" href="#main">Skip to content</a>'
        f"{protected_blocks}"
        '<main id="main"><p>Search</p>'
        '<a href="/application/settings">Search</a>'
        '<a href="/app">Search</a></main></body></html>'
    )

    localized = localize_propertyquarry_html(
        source,
        locale="de-DE",
        path="/app/search",
        query_string=b"lang=de-DE",
    )

    for protected in (
        "<script>Search</script>",
        "<style>Search</style>",
        "<pre>Search</pre>",
        "<code>Search</code>",
        "<textarea>Search</textarea>",
        "<template><p>Search</p></template>",
        "<svg><text>Search</text></svg>",
    ):
        assert protected in localized
    assert "<p>Suche</p>" in localized
    assert 'href="/application/settings"' in localized
    assert 'href="/application/settings?lang=de-DE"' not in localized
    assert 'href="/app?lang=de-DE"' in localized
    assert localized.index('class="skip"') < localized.index("data-pq-localization-status")
    assert localized.index("data-pq-localization-status") < localized.lower().index("</body>")
    assert 'rel="alternate"' not in localized


def test_legal_source_routes_remain_unmodified_until_independent_review() -> None:
    source = '<!doctype html><html lang="en"><head><title>Privacy</title></head><body><a href="/app/search">Search</a></body></html>'
    assert propertyquarry_route_is_translated("/app/search") is True
    assert propertyquarry_route_is_translated("/app/research/candidate-7") is True
    assert propertyquarry_route_is_translated("/app/account") is True
    assert propertyquarry_route_is_translated("/privacy") is False
    assert (
        propertyquarry_required_route_translation_status("/privacy")
        == "blocked_unreviewed_legal_source"
    )
    assert (
        localize_propertyquarry_html(
            source,
            locale="de-DE",
            path="/privacy",
            query_string=b"lang=de-DE",
        )
        == source
    )


def test_error_html_localizes_known_recovery_copy_without_advertising_seo_alternates() -> None:
    app = FastAPI()
    app.add_middleware(PropertyQuarryLocalizationMiddleware)

    @app.get("/app/research/{candidate_ref}")
    def unavailable_research(candidate_ref: str) -> HTMLResponse:
        return HTMLResponse(
            '<html lang="en"><head>'
            '<meta name="application-name" content="PropertyQuarry">'
            "<title>PropertyQuarry Research</title></head>"
            f"<body>Unavailable: {candidate_ref}. <button>Try again</button></body></html>",
            status_code=503,
        )

    response = TestClient(app, base_url="https://propertyquarry.com").get(
        "/app/research/candidate-7?lang=de-DE"
    )
    assert response.status_code == 503
    assert response.headers["content-language"] == "de-DE"
    assert response.headers["x-propertyquarry-translation-status"].startswith(
        "global-route-shell;"
    )
    assert "PropertyQuarry Recherche" in response.text
    assert "Erneut versuchen" in response.text
    assert 'rel="alternate"' not in response.text
    assert "pq_locale=de-DE" in response.headers["set-cookie"]


def test_cookie_secure_attribute_uses_asgi_scheme_not_spoofable_forwarded_proto() -> None:
    response_messages: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        },
        {
            "type": "http.response.body",
            "body": _propertyquarry_document(),
            "more_body": False,
        },
    ]
    spoofed_https = _run_middleware(
        response_messages,
        scope=_scope(
            scheme="http",
            headers=[
                (b"host", b"propertyquarry.com"),
                (b"x-forwarded-proto", b"https"),
                (b"forwarded", b"for=203.0.113.8;proto=https"),
            ],
        ),
    )
    insecure_cookie = _response_header_values(spoofed_https, "set-cookie")[0]
    assert "pq_locale=de-DE" in insecure_cookie
    assert "Secure" not in insecure_cookie

    spoofed_http = _run_middleware(
        response_messages,
        scope=_scope(
            scheme="https",
            headers=[
                (b"host", b"propertyquarry.com"),
                (b"x-forwarded-proto", b"http"),
                (b"forwarded", b"for=203.0.113.8;proto=http"),
            ],
        ),
    )
    secure_cookie = _response_header_values(spoofed_http, "set-cookie")[0]
    assert "Secure" in secure_cookie


def test_cross_brand_html_is_byte_for_byte_untouched_without_locale_headers_or_cookie() -> None:
    response_messages: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"etag", b'"ea-shell"'),
                (b"x-brand", b"ea"),
            ],
        },
        {
            "type": "http.response.body",
            "body": (
                b'<!doctype html><html lang="en"><head>'
                b'<meta name="application-name" content="EA">'
                b"<script>window.example='<meta name=\"application-name\" content=\"PropertyQuarry\">';</script>"
                b"<title>EA Search</title></head>"
                b"<body><p>Sea"
            ),
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": b"rch</p></body></html>",
            "more_body": False,
        },
    ]
    emitted = _run_middleware(
        response_messages,
        scope=_scope(
            scheme="https",
            headers=[
                (b"host", b"propertyquarry.com"),
                (b"x-forwarded-host", b"propertyquarry.com"),
                (b"x-forwarded-proto", b"https"),
            ],
        ),
    )

    assert emitted == response_messages
    assert _response_header_values(emitted, "set-cookie") == []
    assert _response_header_values(emitted, "content-language") == []
    assert _response_header_values(emitted, "x-propertyquarry-translation-status") == []


def test_non_html_encoded_and_redirect_responses_stream_without_buffering_or_mutation() -> None:
    scenarios: list[list[Message]] = [
        [
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            },
            {"type": "http.response.body", "body": b'{"part":', "more_body": True},
            {"type": "http.response.body", "body": b'"two"}', "more_body": False},
        ],
        [
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-encoding", b"gzip"),
                ],
            },
            {"type": "http.response.body", "body": b"\x1f\x8bencoded", "more_body": False},
        ],
        [
            {
                "type": "http.response.start",
                "status": 307,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"location", b"/app/search"),
                ],
            },
            {"type": "http.response.body", "body": b"", "more_body": False},
        ],
    ]

    async def exercise(messages: list[Message]) -> tuple[list[Message], list[int]]:
        delivered: list[Message] = []
        checkpoints: list[int] = []

        async def upstream(_scope: Scope, _receive, send) -> None:  # type: ignore[no-untyped-def]
            for message in messages:
                await send(dict(message))
                checkpoints.append(len(delivered))

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def downstream(message: Message) -> None:
            delivered.append(message)

        middleware = PropertyQuarryLocalizationMiddleware(upstream)
        await middleware(_scope(), receive, downstream)
        return delivered, checkpoints

    for messages in scenarios:
        delivered, checkpoints = asyncio.run(exercise(messages))
        assert delivered == messages
        assert checkpoints == list(range(1, len(messages) + 1))


def test_oversize_malformed_and_non_utf8_html_fail_open_without_reordering_or_500() -> None:
    property_document = _propertyquarry_document("<p>Search</p>")
    oversized_messages: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        },
        {
            "type": "http.response.body",
            "body": property_document[:80],
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": property_document[80:],
            "more_body": False,
        },
    ]
    assert _run_middleware(oversized_messages, max_html_bytes=64) == oversized_messages

    declared_oversized: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", b"999"),
            ],
        },
        {"type": "http.response.body", "body": property_document, "more_body": False},
    ]
    assert _run_middleware(declared_oversized, max_html_bytes=64) == declared_oversized

    malformed_messages: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html")],
        },
        {
            "type": "http.response.body",
            "body": property_document[:-14] + b"\xff</body></html>",
            "more_body": False,
        },
    ]
    assert _run_middleware(malformed_messages) == malformed_messages

    latin1_messages: list[Message] = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=iso-8859-1")],
        },
        {
            "type": "http.response.body",
            "body": b"<html><body>Espa\xf1ol</body></html>",
            "more_body": False,
        },
    ]
    assert _run_middleware(latin1_messages) == latin1_messages


def test_research_shell_critical_actions_are_translated_with_dynamic_copy_left_untouched() -> None:
    source = """<!doctype html><html lang="en"><head><title>PropertyQuarry Research</title></head><body>
    <nav><a href="/app/shortlist">Back to shortlist</a></nav>
    <main><h1>Property research</h1><h2>Research packet</h2>
    <button>Open property</button><section><h2>Evidence</h2><h2>Decision</h2>
    <button>Yes</button><button>No</button><button>Maybe</button><button>Request documents</button></section>
    <p>Immo Provider contractual disclosure 2026-07 remains in English.</p></main></body></html>"""
    expectations = {
        "de-AT": ("Immobilienrecherche", "Recherchepaket", "Zurück zur Merkliste", "Unterlagen anfordern"),
        "de-DE": ("Immobilienrecherche", "Recherchepaket", "Zurück zur Merkliste", "Unterlagen anfordern"),
        "es-CR": ("Investigación de la propiedad", "Paquete de investigación", "Volver a favoritos", "Solicitar documentos"),
    }
    for locale, translated_copy in expectations.items():
        localized = localize_propertyquarry_html(
            source,
            locale=locale,
            path="/app/research/candidate-7",
            query_string=f"run_id=run-7&lang={locale}".encode(),
        )
        for expected in translated_copy:
            assert expected in localized
        assert "Immo Provider contractual disclosure 2026-07 remains in English." in localized
        assert f'href="/app/shortlist?lang={locale}"' in localized
        assert 'rel="alternate"' not in localized


def test_pseudo_locale_expands_visible_copy_but_is_not_publicly_selectable() -> None:
    source_text = "Search brief"
    pseudo_text = propertyquarry_translation(source_text, locale=PROPERTYQUARRY_PSEUDO_LOCALE)
    assert pseudo_text.startswith("[!! ")
    assert len(pseudo_text) >= int(len(source_text) * 1.5)
    assert PROPERTYQUARRY_PSEUDO_LOCALE not in PROPERTYQUARRY_PUBLIC_LOCALES

    pseudo_html = localize_propertyquarry_html(
        '<html lang="en"><head><title>PropertyQuarry Search</title></head><body><h1>Search brief</h1><button>Save changes</button></body></html>',
        locale=PROPERTYQUARRY_PSEUDO_LOCALE,
        path="/app/search",
    )
    assert f'<html lang="{PROPERTYQUARRY_PSEUDO_LOCALE}">' in pseudo_html
    assert pseudo_html.count("[!! ") >= 3
    assert f'hreflang="{PROPERTYQUARRY_PSEUDO_LOCALE}"' not in pseudo_html


def test_search_shortlist_cookie_selector_and_fail_closed_redirect_contracts() -> None:
    client = build_property_client(principal_id="pq-localization-routes")
    start_workspace(client, mode="personal", workspace_name="Localization Office")

    german = client.get(
        "/app/search?lang=de-AT",
        headers={"host": "attacker.example"},
        follow_redirects=False,
    )
    assert german.status_code == 200
    assert german.headers["content-language"] == "de-AT"
    assert german.headers["content-encoding"] == "gzip"
    assert german.headers["x-propertyquarry-translation-status"] == (
        "global-route-shell; english-fallback-unreviewed-legal-provider-customer-content; "
        "independent-native-review-required"
    )
    assert "Cookie" in german.headers["vary"]
    assert "Accept-Language" in german.headers["vary"]
    cookie = german.headers["set-cookie"]
    assert "pq_locale=de-AT" in cookie
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
    assert re.search(r'<html\b[^>]*\blang="de-AT"', german.text)
    assert "PropertyQuarry Suche" in german.text
    assert ">Merkliste<" in german.text
    assert 'href="/app/shortlist?lang=de-AT"' in german.text
    assert 'data-pq-localization-status' in german.text
    assert 'rel="alternate"' not in german.text
    assert 'hreflang="de-AT"' in german.text
    assert "attacker.example" not in german.text
    assert german.text.index("pqx-skip-link") < german.text.index("data-pq-localization-status")
    assert 'href="/static/propertyquarry-localization.css"' in german.text

    spanish_selection = client.get("/app/shortlist?lang=es-CR", follow_redirects=False)
    assert spanish_selection.status_code == 200
    assert spanish_selection.headers["content-language"] == "es-CR"
    assert "Favoritos de PropertyQuarry" in spanish_selection.text
    assert ">Buscar<" in spanish_selection.text
    assert "no ha sido revisada profesionalmente" in spanish_selection.text

    persisted = client.get("/app/shortlist", follow_redirects=False)
    assert persisted.status_code == 200
    assert persisted.headers["content-language"] == "es-CR"
    assert "Favoritos de PropertyQuarry" in persisted.text
    assert 'href="/app/properties"' in persisted.text
    assert 'href="/app/properties?lang=es-CR"' not in persisted.text
    assert "set-cookie" not in persisted.headers

    redirect = client.get("/app/properties?lang=de-DE", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/app/search?lang=de-DE"
    assert "content-language" not in redirect.headers
    assert "set-cookie" not in redirect.headers

    invalid = client.get(
        "/app/search?lang=%0d%0aX-Unsafe%3Ayes",
        headers={"cookie": ""},
        follow_redirects=False,
    )
    assert invalid.status_code == 200
    assert invalid.headers["content-language"] == "en"
    assert "set-cookie" not in invalid.headers
    assert "X-Unsafe" not in invalid.text

    css = client.get("/static/propertyquarry-localization.css")
    assert css.status_code == 200
    assert "flex-wrap: wrap" in css.text
    assert "overflow-wrap: anywhere" in css.text
    assert re.search(r"min-block-size:\s*2\.75rem", css.text)
