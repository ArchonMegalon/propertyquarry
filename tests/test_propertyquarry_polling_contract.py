from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_SCRIPT = REPO_ROOT / "ea/app/templates/app/_property_workbench_script.html"
FEEDBACK_SCRIPT = REPO_ROOT / "ea/app/templates/app/_property_workbench_feedback_script.html"
RESULTS_TEMPLATE = REPO_ROOT / "ea/app/templates/app/_property_results_list.html"
WORKBENCH_TEMPLATE = REPO_ROOT / "ea/app/templates/app/property_decision_workbench.html"


def _render_browser_workbench_fixture(
    *,
    principal_id: str,
    run_id: str,
    status: str,
    progress: int,
    current_step: str,
    message: str,
    summary: dict[str, object],
) -> tuple[str, str]:
    import app.product.service as product_service
    from tests.product_test_helpers import build_property_client, start_workspace

    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Persisted Lifecycle")
    run = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        force_refresh=False,
    )
    run.update(
        status=status,
        progress=progress,
        current_step=current_step,
        message=message,
    )
    run["summary"].update(summary)
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run)
    try:
        response = client.get(
            "/app/properties",
            params={"run_id": run_id},
            headers={"host": "propertyquarry.com", "accept": "text/html"},
        )
        assert response.status_code == 200, response.text
        script_match = re.search(
            r'<script[^>]+src="([^"]*property-workbench\.js[^"]*)"',
            response.text,
        )
        assert script_match is not None
        script_response = client.get(
            script_match.group(1),
            headers={"host": "propertyquarry.com"},
        )
        assert script_response.status_code == 200, script_response.text
        return response.text, script_response.text
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)


def test_workbench_network_mutations_and_pollers_are_bounded_and_cancellable() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "const fetchWithTimeout = async" in source
    assert "new AbortController()" in source
    assert "activeRequestControllers" in source
    assert "RUN_POLL_WINDOW_MS" in source
    assert "RUN_POLL_MAX_FAILURES" in source
    assert "VISUAL_POLL_WINDOW_MS" in source
    assert "VISUAL_POLL_MAX_FAILURES" in source
    assert "REPAIR_POLL_WINDOW_MS" in source
    assert "REPAIR_POLL_MAX_FAILURES" in source
    assert "while (true)" not in source
    assert "pollLifecycleGeneration === pageRequestLifecycleGeneration" in source
    assert "data-pqx-poll-resume" in source
    assert "pauseVisualStatusPolling" in source
    assert "data-pw-visual-poll-paused" in source
    assert source.count("document.hidden || navigator.onLine === false") >= 3


def test_opportunity_brief_request_normalizes_template_encoded_candidate_ref() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "const rawCandidateRef = String(button.getAttribute('data-candidate-ref')" in source
    assert "candidateRef = decodeURIComponent(rawCandidateRef);" in source
    assert "encodeURIComponent(candidateRef)" in source


def test_listing_thumbnails_keep_safe_https_and_fail_to_honest_placeholder() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")
    results_template = RESULTS_TEMPLATE.read_text(encoding="utf-8")
    workbench_template = WORKBENCH_TEMPLATE.read_text(encoding="utf-8")

    assert "parsed.protocol === 'https:'" in source
    assert "isSupportedImage" in source
    assert "isWillhabenApiTracker" in source
    assert "candidateUrl.startsWith('//')" in source
    assert "data-pqx-thumbnail-image" in source
    assert "markThumbnailUnavailable" in source
    assert "Media preview not available." in source
    assert "primary_preview_url.startswith('https://')" in results_template
    assert "data-pqx-thumbnail-image" in results_template
    assert "Media preview not available." in results_template
    assert "ranked_preview_url.startswith('https://')" in workbench_template
    assert ".pqx-thumb.is-unavailable .pqx-thumb-placeholder" in workbench_template


def test_rendered_listing_results_keep_remote_thumbnail_and_missing_photo_slot() -> None:
    thumbnail_url = "https://cache.willhaben.at/mmo/8/1234567898.jpg"
    html, _script = _render_browser_workbench_fixture(
        principal_id="exec-property-thumbnail-render",
        run_id="run-property-thumbnail-render",
        status="processed",
        progress=100,
        current_step="completed",
        message="Two homes ready.",
        summary={
            "status": "processed",
            "provider_total": 1,
            "sources_total": 1,
            "sources_completed": 1,
            "listing_total": 2,
            "ranked_total": 2,
            "sources": [
                {
                    "source_key": "willhaben",
                    "source_label": "Willhaben",
                    "status": "processed",
                    "listing_total": 2,
                    "top_candidates": [
                        {
                            "candidate_ref": "property-scout:photo",
                            "rank": 1,
                            "title": "Vienna home with photo",
                            "property_url": "https://www.willhaben.at/iad/immobilien/d/1",
                            "preview_image_url": thumbnail_url,
                            "property_facts": {"price_eur": 420000},
                        },
                        {
                            "candidate_ref": "property-scout:no-photo",
                            "rank": 2,
                            "title": "Vienna home without photo",
                            "property_url": "https://www.willhaben.at/iad/immobilien/d/2",
                            "property_facts": {"price_eur": 390000},
                        },
                    ],
                }
            ],
        },
    )

    assert "Vienna home with photo" in html
    assert re.search(
        rf'<img\s+[^>]*src="{re.escape(thumbnail_url)}"[^>]*data-pqx-thumbnail-image',
        html,
    )


def test_rendered_first_paint_rejects_provider_chrome_for_honest_placeholder() -> None:
    provider_chrome_url = "https://cache.willhaben.at/img/upselling/icon-bump.png"
    html, _script = _render_browser_workbench_fixture(
        principal_id="exec-property-thumbnail-provider-chrome",
        run_id="run-property-thumbnail-provider-chrome",
        status="processed",
        progress=100,
        current_step="completed",
        message="One home ready.",
        summary={
            "status": "processed",
            "provider_total": 1,
            "sources_total": 1,
            "sources_completed": 1,
            "listing_total": 1,
            "ranked_total": 1,
            "sources": [
                {
                    "source_key": "willhaben",
                    "source_label": "Willhaben",
                    "status": "processed",
                    "listing_total": 1,
                    "top_candidates": [
                        {
                            "candidate_ref": "property-scout:provider-chrome",
                            "rank": 1,
                            "title": "Vienna home with real media fallback",
                            "property_url": "https://www.willhaben.at/iad/immobilien/d/4",
                            "preview_image_url": provider_chrome_url,
                            "property_facts": {
                                "price_eur": 405000,
                            },
                        }
                    ],
                }
            ],
        },
    )

    assert provider_chrome_url not in html
    assert "Media preview not available." in html


def test_real_chromium_broken_listing_thumbnail_uses_honest_placeholder() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    thumbnail_url = "https://cache.willhaben.at/mmo/8/1234567898.jpg"
    html, workbench_javascript = _render_browser_workbench_fixture(
        principal_id="exec-property-thumbnail-error",
        run_id="run-property-thumbnail-error",
        status="processed",
        progress=100,
        current_step="completed",
        message="One home ready.",
        summary={
            "status": "processed",
            "provider_total": 1,
            "sources_total": 1,
            "sources_completed": 1,
            "listing_total": 1,
            "ranked_total": 1,
            "sources": [
                {
                    "source_key": "willhaben",
                    "source_label": "Willhaben",
                    "status": "processed",
                    "listing_total": 1,
                    "top_candidates": [
                        {
                            "candidate_ref": "property-scout:broken-photo",
                            "rank": 1,
                            "title": "Vienna home with unavailable photo",
                            "property_url": "https://www.willhaben.at/iad/immobilien/d/3",
                            "preview_image_url": thumbnail_url,
                            "property_facts": {"price_eur": 410000},
                        }
                    ],
                }
            ],
        },
    )

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - developer machines may omit browsers
            pytest.skip(
                "chromium unavailable for listing-thumbnail fallback: "
                f"{type(exc).__name__}"
            )
        try:
            page = browser.new_page()

            def _fulfill_fixture(route: object) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if request.resource_type == "document":
                    route.fulfill(status=200, content_type="text/html", body=html)
                    return
                if parsed.path.endswith("/property-workbench.js"):
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=workbench_javascript,
                    )
                    return
                if request.url == thumbnail_url:
                    route.fulfill(status=404, content_type="image/jpeg", body="")
                    return
                route.fulfill(status=200, content_type="text/plain", body="")

            page.route("**/*", _fulfill_fixture)
            page.goto(
                "https://propertyquarry.test/app/properties?run_id=run-property-thumbnail-error",
                wait_until="load",
            )
            page.wait_for_function(
                """() => document.querySelector('[data-pqx-thumbnail]')
                  ?.classList.contains('is-unavailable')"""
            )
            thumbnail = page.locator("[data-pqx-thumbnail]").first
            assert "is-unavailable" in (thumbnail.get_attribute("class") or "")
            assert thumbnail.locator(".pqx-thumb-placeholder").is_visible()
            assert "Media preview not available." in thumbnail.inner_text()
        finally:
            browser.close()


def test_workbench_persisted_lifecycle_restore_reconciles_without_duplicate_polling() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "pageRequestLifecycleGeneration += 1;" in source
    assert "resumeActiveRunPollAfterPageShow();" in source
    assert "resumePendingVisualPollsAfterPageShow();" in source
    assert "resumeQuietRepairPollAfterPageShow();" in source
    assert "resumeTerminalHydrationAfterPageShow();" in source
    assert "resumeActiveRunPollAfterPageShow = scheduleActiveRunPollResume;" in source
    assert "resumePendingVisualPollsAfterPageShow = resumePendingVisualStatusPolls;" in source
    assert "resumeQuietRepairPollAfterPageShow = () =>" in source
    assert "resumeTerminalHydrationAfterPageShow = scheduleTerminalHydrationResume;" in source
    assert "root.querySelectorAll('[data-pw-visual-request]').forEach((button) =>" in source
    assert "button._pqxVisualPollGeneration === visualPollGeneration" in source
    assert "button._pqxVisualPollLifecycleGeneration === pageRequestLifecycleGeneration" in source
    assert "repairLifecycleGeneration !== pageRequestLifecycleGeneration" in source
    assert "hydrationLifecycleGeneration !== pageRequestLifecycleGeneration" in source
    assert "if (runPollActive) return;" in source
    assert "activeRunPollResumeRequested" in source
    assert "if (event.persisted && pageRequestLifecycleEnded)" not in source


def test_search_launch_reuses_a_payload_bound_idempotency_key_until_acknowledged() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "const stableSearchLaunchIdempotencyKey = async (bodyText) =>" in source
    assert "payload_sha256: digest" in source
    assert "window.sessionStorage.setItem(SEARCH_LAUNCH_IDEMPOTENCY_STORAGE_KEY" in source
    assert "'Idempotency-Key': idempotencyKey" in source
    assert source.count("const launch = await postSearchLaunch(") == 2
    assert source.count("clearSearchLaunchIdempotencyKey(launch.bodyText, launch.idempotencyKey);") == 2

    feedback = FEEDBACK_SCRIPT.read_text(encoding="utf-8")
    assert "button.getAttribute('data-pw-visual-poll-paused') === 'true'" in feedback
    assert "updateVisualButtonFromStatus(button, { firstPollDelaySeconds: 0.1, resetDeadline: true });" in feedback


def test_real_chromium_synthetic_persisted_lifecycle_race_fences_stale_poll() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    import app.product.service as product_service
    from tests.product_test_helpers import build_property_client, start_workspace

    principal_id = "exec-property-persisted-lifecycle"
    run_id = "run-property-persisted-lifecycle"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Persisted Lifecycle")
    run = product_service._new_property_search_run_record(
        run_id=run_id,
        principal_id=principal_id,
        selected_platforms=("willhaben",),
        property_search_preferences={
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
        },
        force_refresh=False,
    )
    run.update(
        status="in_progress",
        progress=20,
        current_step="source_fetch",
        message="Searching before restore",
    )
    run["summary"].update(
        status="in_progress",
        provider_total=1,
        sources_total=1,
        reviewed_listing_total=1,
    )
    with product_service._PROPERTY_SEARCH_RUN_LOCK:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = dict(run)
    try:
        response = client.get(
            "/app/properties",
            params={"run_id": run_id},
            headers={"host": "propertyquarry.com", "accept": "text/html"},
        )
        assert response.status_code == 200, response.text
        script_match = re.search(
            r'<script[^>]+src="([^"]*property-workbench\.js[^"]*)"',
            response.text,
        )
        assert script_match is not None
        script_response = client.get(
            script_match.group(1),
            headers={"host": "propertyquarry.com"},
        )
        assert script_response.status_code == 200, script_response.text
        initial_html = response.text
        workbench_javascript = script_response.text
    finally:
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)

    status_path = f"/app/api/signals/property/search/run/{run_id}"
    stale_payload = {
        "run_id": run_id,
        "status": "processed",
        "progress": 100,
        "current_step": "completed",
        "message": "STALE terminal response must be fenced",
        "status_url": status_path,
        "summary": {
            "status": "processed",
            "provider_total": 1,
            "sources_total": 1,
            "reviewed_listing_total": 1,
        },
    }
    fresh_payload = {
        "run_id": run_id,
        "status": "in_progress",
        "progress": 42,
        "current_step": "source_fetch",
        "message": "Fresh resumed update",
        "status_url": status_path,
        "summary": {
            "status": "in_progress",
            "provider_total": 1,
            "sources_total": 1,
            "reviewed_listing_total": 2,
        },
    }
    status_requests: list[str] = []
    document_requests: list[str] = []
    page_errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - developer machines may omit browsers
            pytest.skip(
                "chromium unavailable for synthetic persisted-lifecycle race: "
                f"{type(exc).__name__}"
            )
        try:
            page = browser.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            # Request routing reloads back/forward documents in this harness. Keep
            # Chromium's real promise/task scheduling, but drive persisted lifecycle
            # events directly so the active-poll race is deterministic. The first
            # transport deliberately ignores abort until the test releases it.
            poll_probe_script = (
                """
(() => {
  const statusPath = __STATUS_PATH__;
  const stalePayload = __STALE_PAYLOAD__;
  const freshPayload = __FRESH_PAYLOAD__;
  const nativeFetch = window.fetch.bind(window);
  const probe = {
    statusCallCount: 0,
    releaseStale: () => false,
    releaseFresh: () => false,
  };
  const controlledResponse = (releaseName, payload) => new Promise((resolve) => {
    let released = false;
    probe[releaseName] = () => {
      if (released) return false;
      released = true;
      resolve(new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }));
      return true;
    };
  });
  window.__pqPersistedLifecyclePollProbe = probe;
  window.fetch = (resource, options = {}) => {
    const rawUrl = typeof resource === 'string'
      ? resource
      : String(resource?.url || resource || '');
    const requestUrl = new URL(rawUrl, window.location.href);
    if (requestUrl.pathname !== statusPath) return nativeFetch(resource, options);
    probe.statusCallCount += 1;
    if (probe.statusCallCount === 1) {
      return controlledResponse('releaseStale', stalePayload);
    }
    if (probe.statusCallCount === 2) {
      return controlledResponse('releaseFresh', freshPayload);
    }
    return nativeFetch(resource, options);
  };
})();
"""
                .replace("__STATUS_PATH__", json.dumps(status_path))
                .replace("__STALE_PAYLOAD__", json.dumps(stale_payload))
                .replace("__FRESH_PAYLOAD__", json.dumps(fresh_payload))
            )
            page.add_init_script(script=poll_probe_script)

            def _fulfill_fixture(route: object) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if request.resource_type == "document":
                    document_requests.append(request.url)
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=initial_html,
                    )
                    return
                if parsed.path.endswith("/property-workbench.js"):
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=workbench_javascript,
                    )
                    return
                if parsed.path == status_path:
                    status_requests.append(request.url)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(fresh_payload),
                    )
                    return
                route.fulfill(status=200, content_type="text/plain", body="")

            page.route("**/*", _fulfill_fixture)
            page.goto(
                f"https://propertyquarry.test/app/properties?run_id={run_id}",
                wait_until="load",
            )
            page.evaluate(
                """() => {
                  const root = document.querySelector('[data-property-decision-workbench]');
                  const mobileButton = root?.querySelector('[data-pw-mobile-visuals] [data-pw-visual-request]');
                  const hasDesktopPending = [...(root?.querySelectorAll('[data-pw-visual-request]') || [])]
                    .some((button) => !button.closest('[data-pw-mobile-visuals]')
                      && ['queued', 'pending', 'processing', 'running', 'in_progress', 'started', 'rendering', 'repairing']
                        .includes(String(button.getAttribute('data-pw-visual-state') || '').toLowerCase()));
                  if (!root || !mobileButton || hasDesktopPending) return;
                  const desktopHost = document.createElement('div');
                  desktopHost.className = 'pqx-context-actions';
                  const desktopButton = mobileButton.cloneNode(true);
                  desktopButton.removeAttribute('id');
                  desktopHost.appendChild(desktopButton);
                  root.appendChild(desktopHost);
                  document.dispatchEvent(new Event('visibilitychange'));
                }"""
            )
            page.wait_for_function(
                "() => window.__pqPersistedLifecyclePollProbe?.statusCallCount === 1"
            )
            assert status_requests == []
            assert len(document_requests) == 1

            persisted = page.evaluate(
                """() => {
                  const pagehide = new PageTransitionEvent('pagehide', { persisted: true });
                  const pageshow = new PageTransitionEvent('pageshow', { persisted: true });
                  window.dispatchEvent(pagehide);
                  window.dispatchEvent(pageshow);
                  return {
                    pagehide: pagehide.persisted,
                    pageshow: pageshow.persisted,
                    statusCallCount: window.__pqPersistedLifecyclePollProbe.statusCallCount,
                  };
                }"""
            )
            assert persisted == {
                "pagehide": True,
                "pageshow": True,
                "statusCallCount": 1,
            }
            page.wait_for_timeout(120)
            assert page.evaluate(
                "() => window.__pqPersistedLifecyclePollProbe.statusCallCount"
            ) == 1
            assert status_requests == []
            assert len(document_requests) == 1

            assert page.evaluate(
                "() => window.__pqPersistedLifecyclePollProbe.releaseStale()"
            ) is True
            page.wait_for_function(
                "() => window.__pqPersistedLifecyclePollProbe.statusCallCount === 2"
            )
            page.wait_for_timeout(120)
            assert page.evaluate(
                "() => window.__pqPersistedLifecyclePollProbe.statusCallCount"
            ) == 2
            assert status_requests == []
            assert len(document_requests) == 1
            assert "STALE terminal response" not in page.locator("body").inner_text()

            assert page.evaluate(
                "() => window.__pqPersistedLifecyclePollProbe.releaseFresh()"
            ) is True
            page.wait_for_function(
                """() => document.querySelector('[data-pqx-run-message]')
                  ?.textContent?.includes('2 homes found')"""
            )
            assert page.evaluate(
                "() => window.__pqPersistedLifecyclePollProbe.statusCallCount"
            ) == 2

            assert status_requests == []
            assert len(document_requests) == 1
            assert "STALE terminal response" not in page.locator("body").inner_text()
            assert page_errors == []
        finally:
            browser.close()


def test_real_chromium_persisted_restore_resumes_all_pending_visual_buttons_once() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    run_id = "run-property-persisted-visuals"
    status_path = f"/app/api/signals/property/search/run/{run_id}"
    initial_html, workbench_javascript = _render_browser_workbench_fixture(
        principal_id="exec-property-persisted-visuals",
        run_id=run_id,
        status="processed",
        progress=100,
        current_step="completed",
        message="Search ready with visual work pending",
        summary={
            "status": "processed",
            "ranked_total": 1,
            "ranked_candidate_total": 1,
            "listing_total": 1,
            "reviewed_listing_total": 1,
            "ranked_candidates": [
                {
                    "candidate_ref": "persisted-visual-home",
                    "source_ref": "willhaben:persisted-visual-home",
                    "title": "Persisted visual home",
                    "property_url": "https://example.test/persisted-visual-home",
                    "floorplan_url": "https://example.test/persisted-visual-home-floorplan.jpg",
                    "fit_score": 91,
                    "tour_status": "pending",
                    "flythrough_status": "queued",
                    "property_facts": {
                        "postal_name": "1010 Wien",
                        "price_display": "EUR 1,500",
                        "area_m2": 70,
                        "rooms": 3,
                    },
                }
            ],
        },
    )
    terminal_payload = {
        "run_id": run_id,
        "status": "processed",
        "progress": 100,
        "current_step": "completed",
        "message": "Search ready with visual work pending",
        "status_url": status_path,
        "summary": {
            "status": "processed",
            "ranked_total": 1,
            "ranked_candidate_total": 1,
            "listing_total": 1,
            "reviewed_listing_total": 1,
        },
    }
    page_errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - developer machines may omit browsers
            pytest.skip(
                "chromium unavailable for persisted visual lifecycle: "
                f"{type(exc).__name__}"
            )
        try:
            page = browser.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.add_init_script(
                script="""
(() => {
  const nativeFetch = window.fetch.bind(window);
  const probe = { calls: 0, methods: [], releases: [] };
  window.__pqVisualLifecycleProbe = probe;
  window.fetch = (resource, options = {}) => {
    const rawUrl = typeof resource === 'string'
      ? resource
      : String(resource?.url || resource || '');
    const requestUrl = new URL(rawUrl, window.location.href);
    const method = String(options.method || resource?.method || 'GET').toUpperCase();
    probe.methods.push(method);
    if (requestUrl.pathname !== '/app/api/signals/property/visual-status') {
      return nativeFetch(resource, options);
    }
    probe.calls += 1;
    const requestKind = String(requestUrl.searchParams.get('request_kind') || 'tour');
    return new Promise((resolve) => {
      let released = false;
      probe.releases.push(() => {
        if (released) return false;
        released = true;
        resolve(new Response(JSON.stringify({
          status: 'processing',
          request_kind: requestKind,
          tour_status: requestKind === 'tour' ? 'processing' : 'pending',
          flythrough_status: requestKind === 'flythrough' ? 'processing' : 'pending',
          status_label: requestKind === 'flythrough' ? 'Walkthrough rendering' : '3D tour rendering',
          status_detail: 'Lifecycle test render is still processing.',
          progress_pct: 35,
          poll_after_seconds: 10,
        }), { status: 200, headers: { 'content-type': 'application/json' } }));
        return true;
      });
    });
  };
})();
"""
            )

            def _fulfill_fixture(route: object) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if request.resource_type == "document":
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=initial_html,
                    )
                    return
                if parsed.path.endswith("/property-workbench.js"):
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=workbench_javascript,
                    )
                    return
                if parsed.path == status_path:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(terminal_payload),
                    )
                    return
                route.fulfill(status=200, content_type="text/plain", body="")

            page.route("**/*", _fulfill_fixture)
            page.goto(
                f"https://propertyquarry.test/app/properties?run_id={run_id}",
                wait_until="load",
            )
            page.wait_for_function(
                """() => {
                  const pending = [...document.querySelectorAll('[data-pw-visual-request]')]
                    .filter((button) => ['queued', 'pending', 'processing', 'running', 'in_progress', 'started', 'rendering', 'repairing']
                      .includes(String(button.getAttribute('data-pw-visual-state') || '').toLowerCase()));
                  return pending.length > 0
                    && pending.some((button) => button.closest('[data-pw-mobile-visuals]'))
                    && pending.some((button) => !button.closest('[data-pw-mobile-visuals]'))
                    && pending.every((button) => button._pqxVisualPollLifecyclePending === true)
                    && window.__pqVisualLifecycleProbe.calls >= pending.length;
                }""",
                timeout=10000,
            )
            before_restore = page.evaluate(
                """() => ({
                  calls: window.__pqVisualLifecycleProbe.calls,
                  pending: [...document.querySelectorAll('[data-pw-visual-request]')]
                    .filter((button) => button._pqxVisualPollLifecyclePending === true).length,
                })"""
            )
            assert before_restore["calls"] == before_restore["pending"]

            page.evaluate(
                """() => {
                  window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
                  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
                  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
                }"""
            )
            page.wait_for_function(
                f"() => window.__pqVisualLifecycleProbe.calls === {before_restore['calls'] * 2}",
                timeout=5000,
            )
            page.wait_for_timeout(250)
            assert page.evaluate("() => window.__pqVisualLifecycleProbe.calls") == (
                before_restore["calls"] * 2
            )
            assert page.evaluate(
                "() => window.__pqVisualLifecycleProbe.methods.includes('POST')"
            ) is False
            page.evaluate(
                "() => window.__pqVisualLifecycleProbe.releases.forEach((release) => release())"
            )
            page.wait_for_timeout(100)
            assert page_errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("lifecycle_kind", ("failed-repair", "terminal-hydration"))
def test_real_chromium_persisted_restore_resumes_nonvisual_reconciliation_once(
    lifecycle_kind: str,
) -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    run_id = f"run-property-persisted-{lifecycle_kind}"
    status_path = f"/app/api/signals/property/search/run/{run_id}"
    failed_repair = lifecycle_kind == "failed-repair"
    status = "failed" if failed_repair else "processed"
    summary: dict[str, object] = {
        "status": status,
        "ranked_total": 1,
        "ranked_candidate_total": 1,
        "listing_total": 1,
        "reviewed_listing_total": 1,
    }
    if failed_repair:
        summary.update(
            repair_status="repairing",
            provider_repair_tasks=[{"status": "running"}],
        )
    initial_html, workbench_javascript = _render_browser_workbench_fixture(
        principal_id=f"exec-property-persisted-{lifecycle_kind}",
        run_id=run_id,
        status=status,
        progress=100,
        current_step="repairing" if failed_repair else "completed",
        message="Repair is active" if failed_repair else "Terminal hydration pending",
        summary=summary,
    )
    response_payload = {
        "run_id": run_id,
        "status": status,
        "progress": 100,
        "current_step": "repairing" if failed_repair else "completed",
        "message": "Authoritative lifecycle response",
        "status_url": status_path,
        "summary": summary,
    }
    page_errors: list[str] = []
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - developer machines may omit browsers
            pytest.skip(
                "chromium unavailable for persisted reconciliation lifecycle: "
                f"{type(exc).__name__}"
            )
        try:
            page = browser.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            probe_script = (
                """
(() => {
  const statusPath = __STATUS_PATH__;
  const responsePayload = __RESPONSE_PAYLOAD__;
  const nativeFetch = window.fetch.bind(window);
  const probe = { calls: 0, methods: [], releases: [] };
  window.__pqReconciliationLifecycleProbe = probe;
  window.fetch = (resource, options = {}) => {
    const rawUrl = typeof resource === 'string'
      ? resource
      : String(resource?.url || resource || '');
    const requestUrl = new URL(rawUrl, window.location.href);
    const method = String(options.method || resource?.method || 'GET').toUpperCase();
    probe.methods.push(method);
    if (requestUrl.pathname !== statusPath) return nativeFetch(resource, options);
    probe.calls += 1;
    return new Promise((resolve) => {
      let released = false;
      probe.releases.push(() => {
        if (released) return false;
        released = true;
        resolve(new Response(JSON.stringify(responsePayload), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }));
        return true;
      });
    });
  };
})();
"""
                .replace("__STATUS_PATH__", json.dumps(status_path))
                .replace("__RESPONSE_PAYLOAD__", json.dumps(response_payload))
            )
            page.add_init_script(script=probe_script)

            def _fulfill_fixture(route: object) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if request.resource_type == "document":
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=initial_html,
                    )
                    return
                if parsed.path.endswith("/property-workbench.js"):
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=workbench_javascript,
                    )
                    return
                route.fulfill(status=200, content_type="text/plain", body="")

            page.route("**/*", _fulfill_fixture)
            page.goto(
                f"https://propertyquarry.test/app/properties?run_id={run_id}",
                wait_until="load",
            )
            page.wait_for_function(
                "() => window.__pqReconciliationLifecycleProbe?.calls === 1",
                timeout=5000,
            )
            page.evaluate(
                """() => {
                  window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
                  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
                  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
                }"""
            )
            if failed_repair:
                page.wait_for_function(
                    "() => window.__pqReconciliationLifecycleProbe.calls === 2",
                    timeout=5000,
                )
            else:
                page.wait_for_timeout(150)
                assert page.evaluate(
                    "() => window.__pqReconciliationLifecycleProbe.calls"
                ) == 1
                assert page.evaluate(
                    "() => window.__pqReconciliationLifecycleProbe.releases[0]()"
                ) is True
                page.wait_for_function(
                    "() => window.__pqReconciliationLifecycleProbe.calls === 2",
                    timeout=5000,
                )
            page.wait_for_timeout(150)
            assert page.evaluate(
                "() => window.__pqReconciliationLifecycleProbe.calls"
            ) == 2
            assert page.evaluate(
                "() => window.__pqReconciliationLifecycleProbe.methods.includes('POST')"
            ) is False
            page.evaluate(
                "() => window.__pqReconciliationLifecycleProbe.releases.forEach((release) => release())"
            )
            page.wait_for_timeout(150)
            assert page_errors == []
        finally:
            browser.close()
