from __future__ import annotations

from contextlib import nullcontext
import os
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, Playwright

from app.api.propertyquarry_localization import PROPERTYQUARRY_PSEUDO_LOCALE, localize_propertyquarry_html
from scripts.propertyquarry_playwright_runtime import (
    normalize_playwright_engine,
    playwright_engine_launch_browser,
)
from tests.product_test_helpers import build_property_client, start_workspace


def _browser_safe_document(source: str, *, localization_css: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", source, flags=re.IGNORECASE | re.DOTALL)
    without_links = re.sub(r"<link\b[^>]*>", "", without_scripts, flags=re.IGNORECASE)
    return re.sub(
        r"</head\s*>",
        f"<style>{localization_css}</style></head>",
        without_links,
        count=1,
        flags=re.IGNORECASE,
    )


def _locale_panel_metrics(page: Page) -> dict[str, float]:
    metrics = page.locator("[data-pq-localization-status]").evaluate(
        """(panel) => {
          const rect = panel.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            width: rect.width,
            scrollWidth: panel.scrollWidth,
            clientWidth: panel.clientWidth,
            viewportWidth: window.innerWidth,
          };
        }"""
    )
    assert isinstance(metrics, dict)
    return {str(key): float(value) for key, value in metrics.items()}


def test_localized_route_and_pseudo_expansion_are_mobile_safe_in_real_browser(
    playwright: Playwright,
) -> None:
    client = build_property_client(principal_id="pq-localization-browser")
    start_workspace(client, mode="personal", workspace_name="Localization Browser Office")
    route = client.get("/app/search?lang=de-DE")
    assert route.status_code == 200
    css = client.get("/static/propertyquarry-localization.css")
    assert css.status_code == 200
    localized_document = _browser_safe_document(route.text, localization_css=css.text)

    engine = normalize_playwright_engine(os.getenv("PROPERTYQUARRY_CORE_BROWSER_ENGINE", "chromium"))
    with nullcontext(playwright):
        browser = playwright_engine_launch_browser(playwright, engine=engine)
        try:
            context = browser.new_context(viewport={"width": 320, "height": 800})
            page = context.new_page()
            page.set_content(localized_document, wait_until="domcontentloaded")
            assert page.locator("html").get_attribute("lang") == "de-DE"
            assert page.get_by_role("heading", name="Suchprofil").first.is_visible()
            assert page.locator("[data-pq-localization-status]").is_visible()
            assert page.locator("[data-pq-localization-status]").get_attribute(
                "data-pq-localization-placement"
            ) == "integrated"
            page.locator("[data-pq-localization-status] summary").click()
            assert page.locator('[data-pq-locale-selector] a[aria-current="true"]').inner_text() == "Deutsch (Deutschland)"
            assert "lang=de-DE" in str(page.get_by_role("link", name="Merkliste").first.get_attribute("href"))
            metrics = _locale_panel_metrics(page)
            assert metrics["left"] >= -1
            assert metrics["right"] <= metrics["viewportWidth"] + 1
            assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1

            pseudo_source = """<!doctype html><html lang="en"><head><title>PropertyQuarry Search</title></head>
            <body><main><h1>Search brief</h1><button>Save changes</button><a href="/app/shortlist">Shortlist</a></main></body></html>"""
            pseudo_document = localize_propertyquarry_html(
                pseudo_source,
                locale=PROPERTYQUARRY_PSEUDO_LOCALE,
                path="/app/search",
            )
            pseudo_document = _browser_safe_document(pseudo_document, localization_css=css.text)
            page.set_content(pseudo_document, wait_until="domcontentloaded")
            assert page.locator("html").get_attribute("lang") == PROPERTYQUARRY_PSEUDO_LOCALE
            assert "[!!" in page.locator("body").inner_text()
            pseudo_panel = page.locator("[data-pq-localization-status]")
            assert pseudo_panel.get_attribute("data-pq-localization-placement") == "floating"
            flow_metrics = pseudo_panel.evaluate(
                """panel => {
                  const rect = panel.getBoundingClientRect();
                  const previous = panel.previousElementSibling;
                  const previousRect = previous ? previous.getBoundingClientRect() : null;
                  return {
                    position: getComputedStyle(panel).position,
                    previousTag: previous ? previous.tagName : '',
                    panelTop: rect.top,
                    previousBottom: previousRect ? previousRect.bottom : null,
                  };
                }"""
            )
            assert flow_metrics["position"] == "relative"
            assert flow_metrics["previousTag"] == "MAIN"
            assert flow_metrics["panelTop"] >= flow_metrics["previousBottom"] - 1
            pseudo_panel.locator("summary").click()
            pseudo_menu = pseudo_panel.locator("[data-pq-locale-selector]")
            assert pseudo_menu.is_visible()
            expanded_metrics = pseudo_panel.evaluate(
                """panel => {
                  const panelRect = panel.getBoundingClientRect();
                  const menuRect = panel.querySelector('[data-pq-locale-selector]').getBoundingClientRect();
                  return {
                    panelTop: panelRect.top,
                    panelBottom: panelRect.bottom,
                    menuTop: menuRect.top,
                    menuBottom: menuRect.bottom,
                  };
                }"""
            )
            assert expanded_metrics["menuTop"] >= expanded_metrics["panelTop"] - 1
            assert expanded_metrics["menuBottom"] <= expanded_metrics["panelBottom"] + 1
            pseudo_metrics = _locale_panel_metrics(page)
            assert pseudo_metrics["right"] <= pseudo_metrics["viewportWidth"] + 1
            assert pseudo_metrics["scrollWidth"] <= pseudo_metrics["clientWidth"] + 1
            context.close()
        finally:
            browser.close()
