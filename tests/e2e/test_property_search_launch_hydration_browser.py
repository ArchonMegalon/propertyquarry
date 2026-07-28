from __future__ import annotations

from pathlib import Path

import pytest


playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

ROOT = Path(__file__).resolve().parents[2]
LOADER_PATH = ROOT / "ea/app/templates/app/_property_search_loader_script.html"
WORKBENCH_PATH = ROOT / "ea/app/templates/app/property_decision_workbench.html"
APP_URL = "https://property.test/app/search"
WORKBENCH_URL = "https://property.test/app/assets/property-workbench.js?v=browser-test"


def _launch_guard_source() -> str:
    source = WORKBENCH_PATH.read_text(encoding="utf-8")
    return (
        source.split("/* PQ_SEARCH_LAUNCH_BOOT_GUARD_START */", 1)[1]
        .split("/* PQ_SEARCH_LAUNCH_BOOT_GUARD_END */", 1)[0]
        .strip()
    )


def _loader_source() -> str:
    return LOADER_PATH.read_text(encoding="utf-8")


def _workbench_fixture_script() -> str:
    return """
(() => {
  const root = document.querySelector('[data-property-decision-workbench]');
  if (!root || root.dataset.pqWorkbenchController === 'loaded') return;
  root.dataset.pqWorkbenchController = 'initializing';
  let launchInFlight = false;
  root.querySelector('[data-property-start-top]').addEventListener('click', async (event) => {
    event.preventDefault();
    if (launchInFlight) return;
    launchInFlight = true;
    await fetch('/v1/onboarding/property-search/preferences', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: '{}',
    });
    await fetch('/app/api/property/search-runs', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: '{}',
    });
  });
  root.dataset.pqWorkbenchController = 'loaded';
  root.dispatchEvent(new CustomEvent('propertyquarry:workbench-ready'));
})();
"""


def _shell_html(*, include_loader: bool) -> str:
    loader = (
        f'<script data-workbench-src="{WORKBENCH_URL}" async>{_loader_source()}</script>'
        if include_loader
        else ""
    )
    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Search hydration fixture</title></head>
  <body>
    <main data-property-decision-workbench data-pqx-surface="search">
      <span data-property-top-launch-status role="status"></span>
      <button type="button" data-property-start-top aria-label="Launch search">
        <span>Launch search</span>
      </button>
      <script>{_launch_guard_source()}</script>
      <form data-console-form-variant="property_search"></form>
      <span data-property-launch-status></span>
      <div data-property-inline-error></div>
    </main>
    {loader}
  </body>
</html>"""


def _install_routes(
    context,
    *,
    include_loader: bool,
    fail_first_initializer: bool = False,
    fail_first_network_load: bool = False,
):
    calls = {"workbench": 0, "preferences": 0, "start": 0}

    def route_request(route) -> None:
        url = route.request.url
        if url.startswith(APP_URL):
            route.fulfill(status=200, content_type="text/html", body=_shell_html(include_loader=include_loader))
            return
        if url.startswith(WORKBENCH_URL):
            calls["workbench"] += 1
            if fail_first_network_load and calls["workbench"] == 1:
                route.abort("failed")
            elif fail_first_initializer and calls["workbench"] == 1:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="throw new Error('fixture initializer failed');",
                )
            else:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=_workbench_fixture_script(),
                )
            return
        if url.endswith("/v1/onboarding/property-search/preferences"):
            calls["preferences"] += 1
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            return
        if url.endswith("/app/api/property/search-runs"):
            calls["start"] += 1
            route.fulfill(status=200, content_type="application/json", body='{"run_id":"fixture-run"}')
            return
        route.abort()

    context.route("https://property.test/**", route_request)
    return calls


def _append_loader(page) -> None:
    page.evaluate(
        """({source, workbenchUrl}) => {
          const script = document.createElement('script');
          script.setAttribute('data-workbench-src', workbenchUrl);
          script.textContent = source;
          document.body.appendChild(script);
        }""",
        {"source": _loader_source(), "workbenchUrl": WORKBENCH_URL},
    )


def test_immediate_launch_click_is_replayed_after_loader_arrives() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        calls = _install_routes(context, include_loader=False)
        page = context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded")
            page.locator("[data-property-start-top]").click()

            assert page.locator("[data-property-start-top]").get_attribute("data-pq-launch-queued") == "true"
            assert "Preparing" in page.locator("[data-property-top-launch-status]").inner_text()

            _append_loader(page)
            page.wait_for_function("() => window.fetch && document.querySelector('[data-property-decision-workbench]')?.dataset.pqWorkbenchController === 'loaded'")
            page.wait_for_function("() => document.querySelector('[data-property-start-top]')?.dataset.pqHydrationPending !== 'true'")
            page.wait_for_timeout(50)

            assert calls["workbench"] == 1
            assert calls["preferences"] == 1
            assert calls["start"] == 1
        finally:
            context.close()
            browser.close()


def test_initializer_failure_is_visible_and_next_click_reloads_before_launch() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        calls = _install_routes(context, include_loader=True, fail_first_initializer=True)
        page = context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded")
            page.locator("[data-property-start-top]").click()
            page.locator("[data-property-top-launch-status]").filter(
                has_text="Select Launch search to reload safely"
            ).wait_for()

            assert calls["workbench"] == 1
            assert calls["start"] == 0
            assert page.locator("script[data-pq-workbench-main]").count() == 1
            assert page.locator("[data-property-decision-workbench]").get_attribute(
                "data-pq-workbench-retry-mode"
            ) == "reload"

            page.locator("[data-property-start-top]").click()
            page.wait_for_url("**/app/search?launch_retry=*")
            page.locator("[data-property-top-launch-status]").filter(
                has_text="Search controls are ready"
            ).wait_for()
            assert calls["workbench"] == 2
            assert calls["start"] == 0

            page.locator("[data-property-start-top]").click()
            page.wait_for_function("() => document.querySelector('[data-property-decision-workbench]')?.dataset.pqWorkbenchController === 'loaded'")
            page.wait_for_timeout(50)

            assert calls["preferences"] == 1
            assert calls["start"] == 1
        finally:
            context.close()
            browser.close()


def test_network_load_failure_reinjects_without_reloading_or_double_launch() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        calls = _install_routes(
            context,
            include_loader=True,
            fail_first_network_load=True,
        )
        page = context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded")
            page.locator("[data-property-start-top]").click()
            page.locator("[data-property-top-launch-status]").filter(
                has_text="Select Launch search to retry"
            ).wait_for()

            assert calls["workbench"] == 1
            assert calls["start"] == 0
            assert page.locator("script[data-pq-workbench-main]").count() == 0

            page.locator("[data-property-start-top]").click()
            page.wait_for_function("() => document.querySelector('[data-property-decision-workbench]')?.dataset.pqWorkbenchController === 'loaded'")
            page.wait_for_timeout(50)

            assert calls["workbench"] == 2
            assert calls["preferences"] == 1
            assert calls["start"] == 1
        finally:
            context.close()
            browser.close()


def test_rapid_pre_hydration_clicks_launch_exactly_once() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        calls = _install_routes(context, include_loader=False)
        page = context.new_page()
        try:
            page.goto(APP_URL, wait_until="domcontentloaded")
            page.evaluate(
                """() => {
                  const button = document.querySelector('[data-property-start-top]');
                  button.click();
                  button.click();
                  button.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                }"""
            )
            _append_loader(page)
            page.wait_for_function("() => document.querySelector('[data-property-decision-workbench]')?.dataset.pqWorkbenchController === 'loaded'")
            page.wait_for_timeout(100)

            assert calls["workbench"] == 1
            assert calls["preferences"] == 1
            assert calls["start"] == 1
        finally:
            context.close()
            browser.close()
