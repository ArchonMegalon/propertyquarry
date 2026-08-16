from __future__ import annotations

import logging
import os
import re
import urllib.parse
from html.parser import HTMLParser

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


PUBLIC_ROUTES = (
    "/",
    "/product",
    "/how-it-works",
    "/security",
    "/data-deletion",
    "/pricing",
    "/docs",
    "/integrations",
    "/guides/wohnung-kaufen-wien-checkliste",
    "/markets/vienna",
    "/register",
    "/sign-in",
)

APP_ROUTES = (
    "/app/properties",
    "/app/settings",
)

LEGACY_APP_ROUTE_REDIRECTS = {
    "/app/briefing": "/app/queue",
    "/app/inbox": "/app/queue",
    "/app/follow-ups": "/app/commitments",
    "/app/memory": "/app/people",
    "/app/contacts": "/app/evidence",
    "/app/channels": "/app/account?billing=1#delivery",
    "/app/automation": "/app/agents",
    "/app/automations": "/app/agents",
}

PROPERTY_SETTINGS_ALIAS_REDIRECTS = {
    "/app/usage": "/app/settings/usage",
    "/app/support": "/app/settings/support",
    "/app/trust": "/app/settings/trust",
    "/app/google": "/app/settings/google",
    "/app/access": "/app/settings/access",
    "/app/invitations": "/app/settings/invitations",
    "/app/outcomes": "/app/settings/outcomes",
    "/app/plan": "/app/settings/plan",
}

PROPERTY_LEGACY_APP_SURFACE_REDIRECTS = {
    "/app/today": "/app/properties",
    "/app/queue": "/app/shortlist",
    "/app/commitments": "/app/account",
    "/app/people": "/app/account",
    "/app/evidence": "/app/account",
    "/app/activity": "/app/account",
    "/app/channel-loop": "/app/account",
}


def _client(*, principal_id: str = "exec-browser-contract") -> TestClient:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = ""
    os.environ.pop("EA_ENABLE_PUBLIC_SIDE_SURFACES", None)
    os.environ.pop("EA_ENABLE_PUBLIC_RESULTS", None)
    os.environ.pop("EA_ENABLE_PUBLIC_TOURS", None)
    os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
    os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    client.headers.update({"X-EA-Principal-ID": principal_id})
    return client


def _assert_no_drift(text: str) -> None:
    lower = text.lower()
    assert "chummer" not in lower
    assert "gm_creator_ops" not in lower
    assert "principal id" not in lower
    assert "operator access ·" not in lower


def _internal_links(html: str) -> list[str]:
    refs = sorted(set(re.findall(r'href="([^"]+)"', html)))
    return [ref for ref in refs if ref.startswith("/") and not ref.startswith("//")]


def _visible_text(html: str) -> str:
    without_script = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_script)).strip().lower()


class _StartTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _start_tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _StartTagParser()
    parser.feed(html)
    return parser.tags


def _has_class(attrs: dict[str, str | None], class_name: str) -> bool:
    return class_name in str(attrs.get("class") or "").split()


def _assert_internal_links_resolve(client: TestClient, *, source_path: str, html: str) -> None:
    for href in _internal_links(html):
        if href.startswith("/app/actions/") or href.startswith("/sign-out"):
            continue
        request_href = href.split("#", 1)[0] or "/"
        linked = client.get(request_href, headers={"host": "propertyquarry.com", "accept": "text/html"}, follow_redirects=False)
        assert linked.status_code in {200, 303, 307}, f"{source_path} links to {href} -> {linked.status_code}"


def test_public_surface_routes_render_and_keep_product_language() -> None:
    from ea.app.api.routes.landing_content import LANDING_FAQS, SIGN_IN_NOTES

    client = _client()
    anonymous_client = _client(principal_id="")
    for path in PUBLIC_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path
        _assert_no_drift(response.text)
        start_tags = _start_tags(response.text)
        assert sum(tag == "main" for tag, _attrs in start_tags) == 1, path
        nav_labels = {
            attrs.get("aria-label")
            for tag, attrs in start_tags
            if tag == "nav"
        }
        assert {"Primary navigation", "Mobile navigation", "Legal navigation"} <= nav_labels, path

    how_it_works = client.get("/how-it-works")
    security = client.get("/security")
    assert "Search. Compare. Decide." in how_it_works.text
    assert "Clear requirements. Calmer search." in security.text
    assert "Must-haves decide what belongs" in security.text
    assert "You choose what is shared" in security.text
    assert "Search. Compare. Decide." not in security.text

    landing = anonymous_client.get("/", headers={"host": "propertyquarry.com", "accept": "text/html"})
    assert "Search once. See the right homes. Decide faster." in landing.text
    assert "matching homes" in landing.text
    assert "ranked homes" not in landing.text.lower()
    assert "Open search" in landing.text
    assert re.search(
        r'<a class="btn(?: primary)?" href="/sign-in\?signing_in=1"[^>]*data-analytics-event="home_open_search"',
        landing.text,
    )
    assert re.search(
        r'<a class="btn(?: ghost)?" href="/register"[^>]*data-analytics-event="home_email_setup"',
        landing.text,
    )
    assert landing.text.index(">Open search</a>") < landing.text.index(">Email sign-in</a>")
    assert "Must-haves stay clear" in landing.text
    assert "Preferences shape fit" in landing.text
    assert "Details stay together" in landing.text
    assert "Research is attached" not in landing.text
    assert "Hard filters stay hard" not in landing.text
    assert "Preferences score" not in landing.text
    assert "sample-memo" not in landing.text
    assert (
        "from account settings" not in landing.text.lower()
        and "from preferences" not in landing.text.lower()
        and "from account, with connections inside it" in landing.text.lower()
    )

    directory = client.get("/directory", follow_redirects=False)
    assert directory.status_code == 307
    assert directory.headers["location"] == "/"

    directory_profile = client.get("/directory/profile/sample", follow_redirects=False)
    assert directory_profile.status_code == 307
    assert directory_profile.headers["location"] == "/"

    pricing = client.get("/pricing")
    assert "<h1>Choose the search depth you need.</h1>" in pricing.text
    assert "Listing sites / search" in pricing.text
    assert "3D tour scope" in pricing.text
    assert "No charge" in pricing.text
    assert ">Free</div>" not in pricing.text
    anonymous_pricing = anonymous_client.get(
        "/pricing",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )
    assert 'href="/register?return_to=%2Fapp%2Fsearch"' in anonymous_pricing.text
    assert 'href="/sign-in?signing_in=1&amp;return_to=%2Fapp%2Fsearch"' in anonymous_pricing.text
    assert "Choose by sources, shortlist size, and research depth." not in pricing.text
    assert "Upgrade when the current lane is the bottleneck." not in pricing.text
    assert "Typical office path" not in pricing.text
    assert "Checkout pending" not in pricing.text
    assert "Billing account" not in pricing.text
    assert "Manage billing from your account." not in pricing.text
    assert "Your account is already active." in pricing.text
    assert re.search(r'<span class="active" aria-current="page">Pricing</span>', pricing.text)
    assert re.search(r'<a href="/pricing"[^>]*>Pricing</a>', pricing.text) is None

    sign_in = client.get("/sign-in", headers={"host": "propertyquarry.com"})
    assert sign_in.status_code == 200
    assert "Trusted device" not in sign_in.text
    assert "Private hardware sign-in lane for approved devices." not in sign_in.text
    assert ">Restricted<" not in sign_in.text
    assert "verified rollout" not in sign_in.text.lower()
    assert ">Invite only<" not in sign_in.text
    assert "Join waitlist" not in sign_in.text

    cookies = client.get("/cookies")
    refunds = client.get("/refunds")
    assert "from account settings" not in f"{cookies.text} {refunds.text}".lower()
    assert "from account, with connections inside it where appropriate" in f"{cookies.text} {refunds.text}".lower()

    security = client.get("/how-it-works")
    assert "Search. Compare. Decide." in security.text
    assert "Describe the home" in security.text
    assert "Compare matching homes" in security.text
    assert "Research before deciding" in security.text
    assert "/how-it-works/score" in security.text
    assert "Describe the home once. Compare matching homes across selected listing sites." in security.text
    assert "Private until you share" in security.text
    assert "Start a search" in security.text
    assert '<ol class="trust-grid" aria-label="How PropertyQuarry works">' in security.text
    assert security.text.count('class="trust-step" aria-hidden="true"') == 3
    assert "<h2>Describe the home</h2>" in security.text
    anonymous_security = anonymous_client.get(
        "/how-it-works",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )
    assert 'href="/register?return_to=%2Fapp%2Fsearch"' in anonymous_security.text
    assert 'href="/sign-in?signing_in=1&amp;return_to=%2Fapp%2Fsearch"' in anonymous_security.text
    assert "Strict rules. Smart ranking." not in security.text
    assert "Score guide" not in security.text
    assert "Hard filters decide eligibility. Optional preferences tune the score." not in security.text
    assert "Private by default." not in security.text
    assert "Automatic digests" not in security.text
    assert "Morning memo schedule" not in security.text
    assert "Security, privacy, and visual quality are reviewed before public changes go live." not in security.text
    assert "Release checks and security review" not in security.text
    assert "Searches, decisions, notes, and property pages stay signed in." in security.text
    assert "EA Postgres" not in security.text
    assert "source of truth" not in security.text.lower()

    deletion = client.get("/data-deletion")
    assert "Request deletion of your PropertyQuarry data." in deletion.text
    assert "property@propertyquarry.com" in deletion.text
    assert "Data deletion request" in deletion.text
    assert "those providers' own account settings" not in deletion.text.lower() and "from preferences" not in deletion.text.lower()

    sign_in = client.get("/sign-in")
    assert "Use a secure email link if your address already has access." in sign_in.text
    assert "First sign-in creates the account automatically." not in sign_in.text
    assert "Identity only" not in sign_in.text
    assert "Choose the narrowest sign-in path" not in sign_in.text
    assert (
        "from account settings" not in next(row["answer"] for row in LANDING_FAQS if row["question"] == "Can I start alone and add others later?").lower()
        and "from preferences" not in " ".join(SIGN_IN_NOTES).lower()
        and "account, with connections inside it" in next(row["answer"] for row in LANDING_FAQS if row["question"] == "Can I start alone and add others later?").lower()
        and "account, with connections inside it" in " ".join(SIGN_IN_NOTES).lower()
    )

    disclaimers = client.get("/disclaimers")
    assert "verified live provider embed" not in disclaimers.text.lower()
    assert "provider verification" not in disclaimers.text.lower()
    assert "check before deciding" in disclaimers.text.lower()

    imprint = anonymous_client.get(
        "/imprint",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )
    assert "Operator details incomplete" in imprint.text
    assert "legal operator name, service address" in imprint.text.lower()
    assert "Is this legal notice complete?" in imprint.text
    assert "PropertyQuarry is responsible for this public product surface" not in imprint.text

    anonymous_support = anonymous_client.get(
        "/support",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )
    assert 'href="/sign-in?signing_in=1&amp;return_to=%2Fapp%2Fsupport"' in anonymous_support.text
    assert "Sign in to attach account context" in anonymous_support.text
    assert "Email support" in anonymous_support.text
    assert "mailto:property@propertyquarry.com" in anonymous_support.text
    signed_in_support = client.get("/support")
    assert 'href="/app/support"' in signed_in_support.text
    assert "Open account support" in signed_in_support.text
    assert "Email support" in signed_in_support.text
    assert "mailto:property@propertyquarry.com" in signed_in_support.text
    signed_in_support_alias = client.get("/app/support", follow_redirects=False)
    assert signed_in_support_alias.status_code == 307
    assert signed_in_support_alias.headers["location"] == "/app/settings/support"
    account_support = client.get("/app/settings/support")
    assert account_support.status_code == 200
    support_tags = _start_tags(account_support.text)
    assert any(tag == "div" and _has_class(attrs, "object-panel-stack") for tag, attrs in support_tags)
    assert not any(tag == "aside" and _has_class(attrs, "object-panel-stack") for tag, attrs in support_tags)
    assert not any(
        _has_class(attrs, "pq-appbar-breadcrumbs") and attrs.get("aria-label")
        for _tag, attrs in support_tags
    )

    for href in _internal_links(landing.text):
        assert not href.startswith("/tours")
        assert not href.startswith("/results")
        resolved = client.get(href, follow_redirects=False)
        assert resolved.status_code in {200, 303, 307}, href

    signed_in_landing = client.get("/", headers={"host": "propertyquarry.com", "accept": "text/html"})
    assert (
        '<a class="btn primary" href="/app/search" data-analytics-event="home_open_search"'
        in signed_in_landing.text
    )
    assert (
        '<a class="btn" href="/app/properties" data-analytics-event="home_open_results"'
        in signed_in_landing.text
    )

    product = anonymous_client.get("/product", headers={"host": "propertyquarry.com", "accept": "text/html"})
    assert re.search(
        r'<a class="btn(?: primary)?" href="/sign-in\?signing_in=1"[^>]*data-analytics-event="home_open_search"[^>]*>Open search</a>',
        product.text,
    )
    assert re.search(
        r'<a class="btn(?: ghost)?" href="/register"[^>]*data-analytics-event="home_email_setup"[^>]*>Email sign-in</a>',
        product.text,
    )
    assert product.text.index(">Open search</a>") < product.text.index(">Email sign-in</a>")


def test_propertyquarry_public_templates_do_not_keep_memo_anchors() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    marketing = open(os.path.join(root, "ea/app/templates/marketing_home.html"), encoding="utf-8").read()

    assert "sample-memo" not in marketing
    assert 'href="#sample-shortlist"' in marketing
    assert 'id="sample-shortlist"' in marketing


def test_pricing_keeps_payfunnels_checkout_closed_while_billing_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYFUNNELS_WEBHOOK_SECRET", "pf-secret")
    monkeypatch.setenv("PAYFUNNELS_PLUS_CHECKOUT_URL", "https://checkout.payfunnels.example/plus")
    monkeypatch.setenv("PAYFUNNELS_AGENT_CHECKOUT_URL", "https://checkout.payfunnels.example/agent")
    client = _client()

    pricing = client.get("/pricing")

    assert pricing.status_code == 200
    assert "Opens here when ready." in pricing.text
    assert "PayFunnels" not in pricing.text
    assert "payfunnels/order" not in pricing.text.lower()
    assert "data-pricing-provider" not in pricing.text
    assert "Checkout uses PayFunnels" not in pricing.text
    assert "Checkout pending" not in pricing.text
    assert "Start checkout" not in pricing.text
    assert "Secure checkout." not in pricing.text


def test_propertyquarry_exposes_privacy_safe_pwa_shell() -> None:
    client = _client()

    public_page = client.get("/")
    app_page = client.get("/app/search")
    manifest = client.get("/manifest.webmanifest")
    service_worker = client.get("/service-worker.js")
    favicon = client.get("/favicon.ico")
    icon_192 = client.get("/pwa-icon-192.png")
    icon_512 = client.get("/pwa-icon-512.png")

    assert public_page.status_code == 200
    assert app_page.status_code == 200
    assert '<link rel="manifest" href="/manifest.webmanifest">' in public_page.text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in app_page.text
    assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in public_page.text
    assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in app_page.text
    assert '<meta name="application-name" content="PropertyQuarry">' in public_page.text
    assert '<meta name="application-name" content="PropertyQuarry">' in app_page.text
    assert '<link rel="apple-touch-icon" href="/pwa-icon-192.png">' in public_page.text
    assert '<link rel="apple-touch-icon" href="/pwa-icon-192.png">' in app_page.text
    assert "navigator.serviceWorker.register('/service-worker.js', { scope: '/' })" in public_page.text
    assert "navigator.serviceWorker.register('/service-worker.js', { scope: '/' })" in app_page.text

    assert manifest.status_code == 200
    payload = manifest.json()
    assert payload["name"] == "PropertyQuarry"
    assert payload["lang"] == "en"
    assert payload["dir"] == "ltr"
    assert payload["id"] == "/app/search"
    assert payload["start_url"] == "/app/search"
    assert payload["display"] == "standalone"
    assert payload["display_override"] == ["standalone", "minimal-ui", "browser"]
    assert payload["scope"] == "/"
    assert payload["launch_handler"]["client_mode"] == "navigate-existing"
    assert payload["prefer_related_applications"] is False
    icons = {(row["src"], row.get("sizes"), row["type"], row.get("purpose")) for row in payload["icons"]}
    assert ("/pwa-icon.svg", "any", "image/svg+xml", "any maskable") in icons
    assert ("/pwa-icon-192.png", "192x192", "image/png", "any maskable") in icons
    assert ("/pwa-icon-512.png", "512x512", "image/png", "any maskable") in icons
    shortcuts = {row["url"]: row for row in payload["shortcuts"]}
    assert shortcuts["/app/search"]["name"] == "Search"
    assert shortcuts["/app/properties"]["name"] == "Results"
    assert shortcuts["/app/shortlist"]["name"] == "Shortlist"
    assert shortcuts["/app/agents"]["name"] == "Saved Searches"

    assert service_worker.status_code == 200
    assert service_worker.headers["cache-control"] == "no-store"
    assert service_worker.headers["x-content-type-options"] == "nosniff"
    assert service_worker.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "caches.open" not in service_worker.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert 'aria-label="PropertyQuarry"' in favicon.text
    assert "cache.put" not in service_worker.text
    assert "fetch(event.request)" not in service_worker.text
    assert icon_192.status_code == 200
    assert icon_192.headers["content-type"] == "image/png"
    assert icon_192.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert icon_512.status_code == 200
    assert icon_512.headers["content-type"] == "image/png"
    assert icon_512.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_propertyquarry_public_host_blocks_raw_runtime_api_docs() -> None:
    client = _client(principal_id="exec-property-openapi-public")
    for path in ("/openapi.json", "/api/docs", "/api/redoc"):
        response = client.get(path, headers={"host": "propertyquarry.com"}, follow_redirects=False)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "propertyquarry_api_schema_not_public"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive, nosnippet"

    internal_schema = client.get("/openapi.json", follow_redirects=False)
    assert internal_schema.status_code == 200
    assert internal_schema.headers["content-type"].startswith("application/json")


def test_propertyquarry_public_docs_do_not_link_raw_openapi_schema() -> None:
    client = _client(principal_id="exec-property-docs-no-openapi")
    for path in ("/", "/product", "/docs"):
        response = client.get(path, headers={"host": "propertyquarry.com"})
        assert response.status_code == 200, path
        assert "/openapi.json" not in response.text
        assert "API schema" not in response.text


def test_experimental_routes_are_unavailable_in_product_mode_by_default() -> None:
    client = _client()
    for path in ("/tours/example-tour", "/results/example-result"):
        response = client.get(path)
        assert response.status_code == 404, path


def test_app_surface_routes_render_without_product_drift() -> None:
    principal_id = "exec-app-contract"
    client = _client(principal_id=principal_id)
    for path in APP_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path
        _assert_no_drift(response.text)
        assert principal_id not in response.text

    search = client.get("/app/search", follow_redirects=False)
    assert search.status_code == 200
    assert str(search.url).endswith("/app/search")
    assert len(search.history) == 0

    properties = client.get("/app/properties", follow_redirects=False)
    assert properties.status_code == 307
    assert str(properties.url).endswith("/app/properties")
    assert len(properties.history) == 0
    assert properties.headers["location"] == "/app/search"
    properties_followed = client.get("/app/properties")
    assert str(properties_followed.url).endswith("/app/search")
    assert "Search flow" in properties_followed.text
    assert "Recent searches" in properties_followed.text

    settings = client.get("/app/settings")
    assert str(settings.url).endswith("/app/account")
    assert "Search defaults" in settings.text
    assert "Notifications" in settings.text
    assert "Export account data" in settings.text
    assert "Clear search history" in settings.text
    assert "Log out" in settings.text


def test_propertyquarry_management_settings_use_property_language() -> None:
    client = _client(principal_id="exec-property-settings-language")
    banned_terms = (
        "office loop",
        "memo",
        "commitment",
        "handoff",
        "draft",
        "operator load",
        "queue items",
        "operator seats",
        "principal seats",
    )
    paths = (
        "/app/settings/plan",
        "/app/settings/usage",
        "/app/settings/support",
        "/app/settings/trust",
        "/app/settings/google",
        "/app/settings/access",
        "/app/settings/invitations",
        "/app/settings/outcomes",
    )
    for path in paths:
        response = client.get(path, headers={"host": "propertyquarry.com", "accept": "text/html"})
        assert response.status_code == 200, path
        text = _visible_text(response.text)
        for term in banned_terms:
            assert term not in text, f"{path} leaked {term!r}"
        _assert_internal_links_resolve(client, source_path=path, html=response.text)

    usage = client.get("/app/settings/usage", headers={"host": "propertyquarry.com", "accept": "text/html"})
    assert "Matches" in usage.text
    assert "Lists used" in usage.text
    assert "Ranked homes" not in usage.text
    assert "Sources used" not in usage.text
    assert "Source checks" not in usage.text
    assert "Search health" in usage.text

    trust = client.get("/app/settings/trust", headers={"host": "propertyquarry.com", "accept": "text/html"})
    assert 'href="/downloads"' not in trust.text
    assert 'href="/app/api/property/account/export?download=1"' in trust.text


def test_propertyquarry_settings_detail_aliases_redirect_to_property_pages() -> None:
    client = _client(principal_id="exec-property-settings-aliases")
    for source, target in PROPERTY_SETTINGS_ALIAS_REDIRECTS.items():
        response = client.get(source, headers={"host": "propertyquarry.com", "accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 307, source
        assert response.headers["location"] == target
        page = client.get(target, headers={"host": "propertyquarry.com", "accept": "text/html"})
        assert page.status_code == 200, target
        text = _visible_text(page.text)
        assert "memo items" not in text
        assert "commitments" not in text
        assert "handoffs" not in text


def test_propertyquarry_legacy_app_surfaces_redirect_to_property_surfaces() -> None:
    client = _client(principal_id="exec-property-legacy-surfaces")
    for source, target in PROPERTY_LEGACY_APP_SURFACE_REDIRECTS.items():
        response = client.get(source, headers={"host": "propertyquarry.com", "accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 307, source
        assert response.headers["location"] == target
        page = client.get(target, headers={"host": "propertyquarry.com", "accept": "text/html"}, follow_redirects=True)
        assert page.status_code == 200, target
        text = _visible_text(page.text)
        assert "current office loop" not in text
        assert "memo items" not in text
        assert "commitment ledger" not in text
        assert "handoffs" not in text


def test_propertyquarry_core_surface_internal_links_resolve() -> None:
    client = _client(principal_id="exec-property-core-link-contract")
    paths = (
        "/",
        "/product",
        "/pricing",
        "/security",
        "/support",
        "/privacy",
        "/terms",
        "/imprint",
        "/cookies",
        "/docs",
        "/integrations",
        "/guides/wohnung-kaufen-wien-checkliste",
        "/markets/vienna",
        "/register",
        "/sign-in",
        "/app/search",
        "/app/properties",
        "/app/shortlist",
        "/app/agents",
        "/app/account",
        "/app/billing",
    )
    for path in paths:
        response = client.get(path, headers={"host": "propertyquarry.com", "accept": "text/html"}, follow_redirects=True)
        if path == "/app/billing":
            assert response.status_code == 200, path
            assert "Account" in response.text
            assert "Billing portal unavailable" not in response.text
            continue
        assert response.status_code == 200, path
        _assert_internal_links_resolve(client, source_path=path, html=response.text)


def test_propertyquarry_support_records_and_reopens_traceable_request() -> None:
    client = _client(principal_id="exec-property-support-request")
    headers = {"host": "propertyquarry.com", "accept": "text/html"}

    page = client.get("/app/settings/support", headers=headers)
    assert page.status_code == 200
    assert 'action="/app/actions/support/request"' in page.text
    assert 'name="summary"' in page.text
    assert 'minlength="5"' in page.text
    assert 'name="details"' in page.text
    assert 'maxlength="2000"' in page.text
    assert "Do not include passwords, payment-card details, identity documents, or private access links." in page.text

    submitted = client.post(
        "/app/actions/support/request",
        headers=headers,
        data={
            "return_to": "/app/settings/support",
            "category": "search_results",
            "summary": "One listing does not open",
            "details": "The property card stays on the same page after I select Open property.",
            "context_reference": "listing-42",
        },
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    parsed = urllib.parse.urlparse(submitted.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/app/settings/support"
    assert query["support_status"] == ["recorded"]
    request_id = query["support_request_id"][0]
    assert re.fullmatch(r"support_[0-9a-f]{12}", request_id)
    container = client.app.state.container
    receipt_rows = [
        row
        for row in container.channel_runtime.list_recent_observations(
            principal_id="exec-property-support-request",
            limit=50,
        )
        if str(row.source_id or "") == request_id
    ]
    assert len(receipt_rows) == 1
    assert receipt_rows[0].channel == "support"
    assert receipt_rows[0].payload["details"] == "The property card stays on the same page after I select Open property."
    assert container.orchestrator.list_human_tasks(
        principal_id="exec-property-support-request",
        role_required="support",
        limit=10,
    ) == []

    receipt = client.get(submitted.headers["location"], headers=headers)
    assert receipt.status_code == 200
    assert "Support reference saved" in receipt.text
    assert f"it has not sent a message. Email support and include reference {request_id}." in receipt.text
    assert "Recent support references" in receipt.text
    assert "One listing does not open" in receipt.text
    assert f"Reference {request_id}" in receipt.text
    assert f"mailto:property@propertyquarry.com?subject=PropertyQuarry%20support%20{request_id}" in receipt.text
    assert "Human review is pending" not in receipt.text

    for index in range(210):
        container.channel_runtime.ingest_observation(
            principal_id="exec-property-support-request",
            channel="product",
            event_type="support_history_displacement_probe",
            payload={"index": index},
            source_id=f"support-history-noise-{index}",
        )
    after_unrelated_activity = client.get(submitted.headers["location"], headers=headers)
    assert "One listing does not open" in after_unrelated_activity.text
    assert f"Reference {request_id}" in after_unrelated_activity.text

    other_account = client.get(
        "/app/settings/support",
        headers={**headers, "X-EA-Principal-ID": "exec-property-support-other-account"},
    )
    assert other_account.status_code == 200
    assert "One listing does not open" not in other_account.text
    assert request_id not in other_account.text


def test_propertyquarry_support_rejects_invalid_and_external_return_target() -> None:
    client = _client(principal_id="exec-property-support-invalid")
    headers = {"host": "propertyquarry.com", "accept": "text/html"}

    rejected = client.post(
        "/app/actions/support/request",
        headers=headers,
        data={
            "return_to": "https://example.invalid/escape",
            "category": "unknown",
            "summary": "No",
            "details": "Too short",
        },
        follow_redirects=False,
    )

    assert rejected.status_code == 303
    parsed = urllib.parse.urlparse(rejected.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/app/settings/support"
    assert parsed.netloc == ""
    assert query["support_error"] == ["support_request_category_invalid"]
    followup = client.get(rejected.headers["location"], headers=headers)
    assert "Reference not saved" in followup.text
    assert "Choose one of the available support categories." in followup.text


def test_propertyquarry_support_does_not_trust_forged_receipt_query() -> None:
    client = _client(principal_id="exec-property-support-forged-receipt")
    response = client.get(
        "/app/settings/support?support_status=recorded&support_request_id=support_deadbeefdead",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Support reference saved" not in response.text
    assert "Reference support_deadbeefdead" not in response.text


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    (
        ("summary", "s" * 121, "support_request_summary_too_long"),
        ("details", "d" * 2001, "support_request_details_too_long"),
        ("context_reference", "r" * 241, "support_request_reference_too_long"),
    ),
)
def test_propertyquarry_support_rejects_overlong_fields_without_truncation(
    field: str,
    value: str,
    expected_error: str,
) -> None:
    client = _client(principal_id=f"exec-property-support-overlong-{field}")
    payload = {
        "return_to": "/app/settings/support",
        "category": "other",
        "summary": "A valid summary",
        "details": "A valid description of the support issue.",
        "context_reference": "",
    }
    payload[field] = value

    response = client.post(
        "/app/actions/support/request",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
        data=payload,
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert query["support_error"] == [expected_error]


def test_propertyquarry_support_stream_caps_body_without_content_length() -> None:
    client = _client(principal_id="exec-property-support-stream-cap")

    def oversized_chunks():
        yield b"return_to=%2Fapp%2Fsettings%2Fsupport&category=other&summary=Valid+summary&details="
        yield b"x" * 33_000

    response = client.post(
        "/app/actions/support/request",
        headers={
            "host": "propertyquarry.com",
            "accept": "text/html",
            "content-type": "application/x-www-form-urlencoded",
        },
        content=oversized_chunks(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert query["support_error"] == ["support_request_too_large"]


def test_propertyquarry_support_accepts_valid_unicode_within_character_limit() -> None:
    client = _client(principal_id="exec-property-support-unicode")
    details = "ä" * 1500

    response = client.post(
        "/app/actions/support/request",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
        data={
            "return_to": "/app/settings/support",
            "category": "other",
            "summary": "Unicode details remain valid",
            "details": details,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert query["support_status"] == ["recorded"]
    request_id = query["support_request_id"][0]
    receipt = next(
        row
        for row in client.app.state.container.channel_runtime.list_recent_observations_matching(
            principal_id="exec-property-support-unicode",
            channel="support",
            event_types=("support_request_created",),
            limit=10,
        )
        if row.source_id == request_id
    )
    assert receipt.payload["details"] == details


def test_propertyquarry_support_post_requires_authenticated_account() -> None:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = "test-token"
    os.environ["EA_RUNTIME_MODE"] = "dev"
    from app.api.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/app/actions/support/request",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
        data={
            "category": "other",
            "summary": "A valid summary",
            "details": "A valid description of the support issue.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
    assert client.app.state.container.channel_runtime.list_recent_observations(limit=20) == []


def test_register_success_surface_uses_account_cta_not_settings_alias() -> None:
    client = _client(principal_id="exec-register-account-cta")

    response = client.get("/register", headers={"host": "propertyquarry.com", "accept": "text/html"})

    assert response.status_code == 200
    assert 'href="/app/account">Account</a>' in response.text
    assert "Account settings" not in response.text
    assert 'href="/app/settings"' not in response.text


def test_legacy_app_aliases_redirect_to_canonical_routes() -> None:
    client = _client()
    for path, target in LEGACY_APP_ROUTE_REDIRECTS.items():
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307, path
        assert response.headers["location"] == target

    redirected = client.get("/app/inbox?focus=board", follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "/app/queue?focus=board"


def test_unauthenticated_browser_app_navigation_redirects_to_sign_in() -> None:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = "test-token"
    os.environ.pop("EA_ENABLE_PUBLIC_SIDE_SURFACES", None)
    os.environ.pop("EA_ENABLE_PUBLIC_RESULTS", None)
    os.environ.pop("EA_ENABLE_PUBLIC_TOURS", None)
    os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
    os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    response = client.get("/app/properties", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?return_to=%2Fapp%2Fproperties"

    deep_link = client.get(
        "/app/properties?run_id=5139bf4532e64edb95534684bf8b620a",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert deep_link.status_code == 303
    assert deep_link.headers["location"] == (
        "/sign-in?return_to=%2Fapp%2Fproperties%3Frun_id%3D5139bf4532e64edb95534684bf8b620a"
    )


def test_unauthenticated_api_calls_still_return_json_auth_error() -> None:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = "test-token"
    os.environ.pop("EA_ENABLE_PUBLIC_SIDE_SURFACES", None)
    os.environ.pop("EA_ENABLE_PUBLIC_RESULTS", None)
    os.environ.pop("EA_ENABLE_PUBLIC_TOURS", None)
    os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
    os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    response = client.get("/app/api/brief", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


def test_anonymous_public_home_does_not_emit_false_auth_failure_warning(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "test-token")
    monkeypatch.delenv("EA_ALLOW_LOOPBACK_NO_AUTH", raising=False)
    monkeypatch.delenv("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", raising=False)
    monkeypatch.delenv("EA_OPERATOR_PRINCIPAL_IDS", raising=False)
    from app.api.app import create_app

    client = TestClient(create_app())
    caplog.set_level(logging.WARNING, logger="app.api.dependencies")

    response = client.get(
        "/",
        headers={"host": "propertyquarry.com", "accept": "text/html"},
    )

    assert response.status_code == 200
    assert "PropertyQuarry" in response.text
    assert not any("ea_auth_failure" in record.getMessage() for record in caplog.records)

    caplog.clear()
    invalid = client.get(
        "/",
        headers={
            "host": "propertyquarry.com",
            "accept": "text/html",
            "authorization": "Bearer invalid-token",
        },
    )

    assert invalid.status_code == 200
    assert any(
        "ea_auth_failure detail=auth_required" in record.getMessage()
        for record in caplog.records
    )
