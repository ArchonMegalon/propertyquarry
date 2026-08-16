from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, expect

from app.api.routes import landing_view_models
from app.product.service import ProductService


_GREENFIELD_PATH = Path(__file__).with_name("test_propertyquarry_greenfield_browser.py")
_GREENFIELD_SPEC = importlib.util.spec_from_file_location("propertyquarry_greenfield_browser_fixtures", _GREENFIELD_PATH)
assert _GREENFIELD_SPEC is not None and _GREENFIELD_SPEC.loader is not None
_GREENFIELD_MODULE = importlib.util.module_from_spec(_GREENFIELD_SPEC)
sys.modules[_GREENFIELD_SPEC.name] = _GREENFIELD_MODULE
_GREENFIELD_SPEC.loader.exec_module(_GREENFIELD_MODULE)

browser = _GREENFIELD_MODULE.browser
propertyquarry_browser_server = _GREENFIELD_MODULE.propertyquarry_browser_server
_assert_no_horizontal_overflow = _GREENFIELD_MODULE._assert_no_horizontal_overflow
_choose_research_visual_style = _GREENFIELD_MODULE._choose_research_visual_style
_issue_browser_workspace_session = _GREENFIELD_MODULE._issue_browser_workspace_session
_new_context = _GREENFIELD_MODULE._new_context
_remote_png_bytes = _GREENFIELD_MODULE._remote_png_bytes
_settled_checked_provider_values = _GREENFIELD_MODULE._settled_checked_provider_values
_video_frame_brightness = _GREENFIELD_MODULE._video_frame_brightness


def _prepare_what_matters_toggle_probe(page) -> int:
    page.locator('[data-property-step-trigger="children"]').click()
    page.wait_for_function(
        """() => document.querySelector(
          '[data-console-form-variant="property_search"]'
        )?.dataset.propertyActiveStep === 'children'"""
    )
    groups = page.locator(
        "[data-property-what-matters-panel] details[data-what-matters-group]"
    )
    groups.first.wait_for(state="attached")
    visible_group_indices = [
        index
        for index in range(groups.count())
        if groups.nth(index).locator(":scope > summary").is_visible()
    ]
    if len(visible_group_indices) < 2:
        diagnostics = page.evaluate(
            """() => Array.from(document.querySelectorAll(
              '[data-property-what-matters-panel] details[data-what-matters-group]'
            )).map((details) => {
              const field = details.closest('[data-property-field-step]');
              const panel = details.closest('[data-property-what-matters-panel]');
              return {
                fieldStep: field?.getAttribute('data-property-field-step') || '',
                fieldHidden: Boolean(field?.hidden),
                fieldAriaHidden: field?.getAttribute('aria-hidden') || '',
                panelHidden: Boolean(panel?.hidden),
                panelDisplay: panel ? getComputedStyle(panel).display : '',
                summaryDisplay: getComputedStyle(details.querySelector(':scope > summary')).display,
              };
            })"""
        )
        raise AssertionError(f"expected two visible What Matters groups: {diagnostics}")
    for probe_index, group_index in enumerate(visible_group_indices):
        groups.nth(group_index).evaluate(
            "(details, index) => details.dataset.pqWhatMattersProbe = String(index)",
            probe_index,
        )
    page.evaluate(
        """() => {
            document.querySelectorAll(
              '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
            ).forEach((details) => {
              details.open = false;
            });
        }"""
    )
    # Native details toggle events are queued. Install the probe only after the
    # reset has settled so each count belongs to a user interaction below.
    page.wait_for_timeout(50)
    page.evaluate(
        """() => {
            const groups = Array.from(document.querySelectorAll(
              '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
            ));
            window.__pqWhatMattersToggleCounts = groups.map(() => 0);
            groups.forEach((details, index) => {
              details.addEventListener('toggle', () => {
                window.__pqWhatMattersToggleCounts[index] += 1;
              });
            });
        }"""
    )
    return len(visible_group_indices)


def _what_matters_probe_state(page) -> dict[str, object]:
    state = page.evaluate(
        """() => {
            const groups = Array.from(document.querySelectorAll(
              '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
            ));
            return {
              open: groups.map((details) => details.open),
              names: groups.map((details) => details.getAttribute('name')),
              counts: Array.from(window.__pqWhatMattersToggleCounts || []),
              focusedSummaryIndex: groups.findIndex(
                (details) => details.querySelector(':scope > summary') === document.activeElement
              ),
            };
        }"""
    )
    assert isinstance(state, dict)
    return state


def test_propertyquarry_mobile_what_matters_is_single_panel_and_keyboard_stable(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        group_count = _prepare_what_matters_toggle_probe(page)
        groups = page.locator("details[data-pq-what-matters-probe]")
        first_summary = groups.nth(0).locator(":scope > summary")
        second_summary = groups.nth(1).locator(":scope > summary")
        expect(first_summary).to_be_visible()
        expect(second_summary).to_be_visible()

        state = _what_matters_probe_state(page)
        assert state["names"] == ["property-what-matters-mobile"] * group_count

        first_summary.click()
        page.wait_for_function(
            """() => document.querySelectorAll(
              '[data-property-what-matters-panel] details[data-pq-what-matters-probe][open]'
            ).length === 1"""
        )
        page.wait_for_timeout(50)
        first_open = _what_matters_probe_state(page)
        assert first_open["open"][:2] == [True, False]
        assert first_open["counts"][:2] == [1, 0]
        assert first_open["focusedSummaryIndex"] == 0

        second_summary.click()
        page.wait_for_function(
            """() => {
              const groups = Array.from(document.querySelectorAll(
                '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
              ));
              return !groups[0]?.open && Boolean(groups[1]?.open);
            }"""
        )
        page.wait_for_timeout(50)
        second_open = _what_matters_probe_state(page)
        assert sum(bool(value) for value in second_open["open"]) == 1
        assert second_open["open"][:2] == [False, True]
        # The selected panel toggles once. The first panel's second event is
        # the expected close caused by the single-panel accordion contract.
        assert second_open["counts"][:2] == [2, 1]
        assert second_open["focusedSummaryIndex"] == 1

        first_summary.press("Enter")
        page.wait_for_function(
            """() => {
              const groups = Array.from(document.querySelectorAll(
                '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
              ));
              return Boolean(groups[0]?.open) && !groups[1]?.open;
            }"""
        )
        page.wait_for_timeout(50)
        keyboard_open = _what_matters_probe_state(page)
        assert sum(bool(value) for value in keyboard_open["open"]) == 1
        assert keyboard_open["counts"][:2] == [3, 2]
        assert keyboard_open["focusedSummaryIndex"] == 0
    finally:
        context.close()


def test_propertyquarry_desktop_what_matters_is_independent_and_reduced_motion_is_instant(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok
        group_count = _prepare_what_matters_toggle_probe(page)
        groups = page.locator("details[data-pq-what-matters-probe]")
        first_summary = groups.nth(0).locator(":scope > summary")
        second_summary = groups.nth(1).locator(":scope > summary")
        expect(first_summary).to_be_visible()
        expect(second_summary).to_be_visible()

        initial = _what_matters_probe_state(page)
        assert initial["names"] == [None] * group_count
        assert page.evaluate("window.matchMedia('(max-width: 760px)').matches") is False

        first_summary.click()
        page.wait_for_timeout(50)
        first_open = _what_matters_probe_state(page)
        assert first_open["open"][:2] == [True, False], first_open
        second_summary.click()
        page.wait_for_function(
            """() => {
              const groups = Array.from(document.querySelectorAll(
                '[data-property-what-matters-panel] details[data-pq-what-matters-probe]'
              ));
              return Boolean(groups[0]?.open) && Boolean(groups[1]?.open);
            }"""
        )
        page.wait_for_timeout(50)
        both_open = _what_matters_probe_state(page)
        assert both_open["open"][:2] == [True, True]
        assert both_open["counts"][:2] == [1, 1]
        assert both_open["focusedSummaryIndex"] == 1

        page.emulate_media(reduced_motion="reduce")
        page.evaluate(
            """() => {
              window.__pqScrollIntoViewCalls = [];
              Element.prototype.scrollIntoView = function(options) {
                window.__pqScrollIntoViewCalls.push(
                  options && typeof options === 'object' ? { ...options } : options
                );
              };
            }"""
        )
        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function(
            """() => document.querySelector(
              '[data-console-form-variant="property_search"]'
            )?.dataset.propertyActiveStep === 'providers'"""
        )
        scroll_calls = page.evaluate("window.__pqScrollIntoViewCalls")
        assert isinstance(scroll_calls, list) and len(scroll_calls) == 1
        assert scroll_calls[0]["behavior"] == "auto"
        assert all(
            not isinstance(call, dict) or call.get("behavior") != "smooth"
            for call in scroll_calls
        )
    finally:
        context.close()


def test_propertyquarry_reduced_motion_skip_link_focus_is_immediately_visible(
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    base_url = str(propertyquarry_browser_server["base_url"])
    context = _new_context(browser, mobile=False, width=1440, height=900)
    page = context.new_page()
    try:
        page.emulate_media(reduced_motion="reduce")
        response = page.goto(f"{base_url}/app/settings/usage", wait_until="domcontentloaded")
        assert response is not None and response.ok
        page.keyboard.press("Tab")
        metrics = page.evaluate(
            """() => {
                const link = document.querySelector('.skip-link');
                if (!(link instanceof HTMLElement)) return null;
                const rect = link.getBoundingClientRect();
                const centerX = Math.min(
                    window.innerWidth - 1,
                    Math.max(0, rect.left + (rect.width / 2)),
                );
                const centerY = Math.min(
                    window.innerHeight - 1,
                    Math.max(0, rect.top + (rect.height / 2)),
                );
                const hit = document.elementFromPoint(centerX, centerY);
                return {
                    active: document.activeElement === link,
                    top: rect.top,
                    bottom: rect.bottom,
                    viewportHeight: window.innerHeight,
                    animations: link.getAnimations().length,
                    hit: Boolean(hit && (hit === link || hit.closest('.skip-link') === link)),
                };
            }"""
        )
        assert isinstance(metrics, dict), metrics
        assert metrics["active"] is True, metrics
        assert float(metrics["top"]) >= 0, metrics
        assert float(metrics["bottom"]) <= float(metrics["viewportHeight"]), metrics
        assert metrics["animations"] == 0, metrics
        assert metrics["hit"] is True, metrics
    finally:
        context.close()


def test_propertyquarry_mobile_flagship_flow_runs_search_opens_research_map_and_walkthrough(
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    observed: dict[str, object] = {}
    family_diorama_url = "/static/property/home/example-shortlist-home-1.png"
    family_listing_photo_url = "/static/property/home/example-shortlist-home-2.png"
    family_map_preview_url = "/app/api/property/map-previews/" + ("f" * 40) + ".png"
    family_map_overlay = [
        {
            "label": "Tiergarten",
            "selected": True,
            "path": "M92 74 L548 74 L548 294 L92 294 Z",
        }
    ]

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        observed["principal_id"] = principal_id
        observed["actor"] = actor
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        observed["max_results_per_source"] = max_results_per_source
        if callable(progress_callback):
            progress_callback(
                step="sources_resolved",
                message="Resolved providers for the flagship mobile flow.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": max(len(selected_platforms), 1)},
            )
            progress_callback(
                step="provider_scan",
                message="Checking saved markets and shortlist coverage.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"listing_total": 7},
            )
            progress_callback(
                step="completed",
                message="Property scouting run completed.",
                status="processed",
                steps_delta=1,
                summary_updates={"high_fit_total": 2, "tour_existing_total": 1},
            )
        return {
            "generated_at": "2026-07-08T07:00:00+00:00",
            "status": "processed",
            "sources_total": max(len(selected_platforms), 1),
            "listing_total": 7,
            "review_created_total": 0,
            "review_existing_total": 2,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 1,
            "high_fit_total": 2,
            "watch_notified_total": 0,
            "sources": [],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    original_get_property_search_run_status = ProductService.get_property_search_run_status

    def _flagship_run_status_with_diorama(self, *, principal_id: str, run_id: str, lightweight: bool = False):
        try:
            payload = original_get_property_search_run_status(
                self,
                principal_id=principal_id,
                run_id=run_id,
                lightweight=lightweight,
            )
        except TypeError:
            payload = original_get_property_search_run_status(
                self,
                principal_id=principal_id,
                run_id=run_id,
            )
        summary = dict(payload.get("summary") or {}) if isinstance(payload.get("summary"), dict) else {}

        def _with_family_diorama(rows: list[object]) -> list[object]:
            patched_rows: list[object] = []
            for row in rows:
                if not isinstance(row, dict):
                    patched_rows.append(row)
                    continue
                candidate = dict(row)
                if str(candidate.get("title") or "").strip() == "Family flat near Tiergarten":
                    facts = (
                        dict(candidate.get("property_facts") or {})
                        if isinstance(candidate.get("property_facts"), dict)
                        else {}
                    )
                    facts.setdefault("preview_image_url", family_listing_photo_url)
                    facts.setdefault("image_url", family_listing_photo_url)
                    facts.setdefault("media_urls_json", [family_listing_photo_url])
                    candidate["property_facts"] = facts
                    candidate["diorama_preview_url"] = family_diorama_url
                patched_rows.append(candidate)
            return patched_rows

        sources = []
        for source in list(summary.get("sources") or []):
            if not isinstance(source, dict):
                sources.append(source)
                continue
            source_row = dict(source)
            source_row["top_candidates"] = _with_family_diorama(list(source_row.get("top_candidates") or []))
            sources.append(source_row)
        if sources:
            summary["sources"] = sources
        if isinstance(summary.get("ranked_candidates"), list):
            summary["ranked_candidates"] = _with_family_diorama(list(summary.get("ranked_candidates") or []))
        return {**payload, "summary": summary}

    monkeypatch.setattr(ProductService, "get_property_search_run_status", _flagship_run_status_with_diorama)

    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)

    context = _new_context(browser, mobile=True, width=390, height=844)
    page = context.new_page()
    visual_requests: list[dict[str, object]] = []
    visual_status_polls = 0
    family_map_preview_bytes = _remote_png_bytes(
        label="Tiergarten map",
        size=(640, 368),
    )

    def _capture_visual_request(route) -> None:
        payload = route.request.post_data_json or {}
        visual_requests.append(payload if isinstance(payload, dict) else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T07:01:00+00:00",
                    "status": "created",
                    "property_url": visual_requests[-1].get("property_url", ""),
                    "title": "Family flat near Tiergarten",
                    "request_kind": visual_requests[-1].get("request_kind", "flythrough"),
                    "tour_url": "",
                    "tour_status": "pending",
                    "flythrough_url": "",
                    "flythrough_status": "pending",
                    "status_label": "Walkthrough queued",
                    "status_detail": "Walkthrough is queued after your request.",
                    "eta_label": "about 8 min",
                    "progress_pct": 22,
                    "poll_after_seconds": 1,
                    "delivery_status": "queued",
                    "blocked_reason": "",
                    "source_ref": visual_requests[-1].get("source_ref", ""),
                    "run_id": visual_requests[-1].get("run_id", ""),
                    "candidate_ref": visual_requests[-1].get("candidate_ref", ""),
                }
            ),
        )

    def _capture_visual_status(route) -> None:
        nonlocal visual_status_polls
        visual_status_polls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated_at": "2026-07-08T07:01:03+00:00",
                    "status": "ready",
                    "property_url": "https://www.immobilienscout24.de/expose/family-tiergarten",
                    "title": "Family flat near Tiergarten",
                    "request_kind": "flythrough",
                    "tour_url": "",
                    "tour_status": "",
                    "flythrough_url": f"{base_url}/tours/altbau-u6?pane=flythrough-pane&autoplay=1",
                    "flythrough_status": "ready",
                    "status_label": "Open walkthrough",
                    "status_detail": "Walkthrough is ready.",
                    "eta_label": "",
                    "progress_pct": 100,
                    "poll_after_seconds": 0,
                    "source_ref": "immobilienscout24:family-tiergarten",
                    "run_id": "run-42",
                    "candidate_ref": "family-tiergarten",
                }
            ),
        )

    page.route("**/app/api/signals/willhaben/property-tour", _capture_visual_request)
    page.route("**/app/api/signals/property/visual-status?**", _capture_visual_status)
    page.route(
        f"**{family_map_preview_url}",
        lambda route: route.fulfill(
            status=200,
            content_type="image/png",
            body=family_map_preview_bytes,
        ),
    )
    try:
        response = page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_role("heading", name="Continue your property search.")).to_be_visible()
        _assert_no_horizontal_overflow(page)

        _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
        response = page.goto(f"{base_url}/sign-in", wait_until="networkidle")
        assert response is not None and response.ok
        expect(page.get_by_role("link", name="Open search").first).to_be_visible()
        for banned in ("Morning Memo", "Office signals", "Workspace loop"):
            assert page.get_by_text(banned, exact=True).count() == 0

        with page.expect_navigation(url=re.compile(r".*/app/search(?:\\?.*)?$"), wait_until="commit", timeout=10_000):
            page.get_by_role("link", name="Open search").first.click()
        page.locator('[data-console-form-variant="property_search"]').wait_for(state="visible")
        _assert_no_horizontal_overflow(page)

        page.select_option('select[name="country_code"]', "AT")
        page.select_option('select[name="region_code"]', "vienna")
        page.wait_for_function(
            """() => document.querySelector('[data-property-field-name="location_query"]')?.dataset.locationMapAvailable === 'true'"""
        )
        page.locator('[data-location-mode-button="map"]').click()
        page.locator("[data-location-map-open]").click()
        location_dialog = page.locator("[data-location-map-dialog]")
        expect(location_dialog).to_be_visible()
        district = location_dialog.locator("[data-location-map-district]").first
        district.wait_for(state="visible")
        district_value = str(district.get_attribute("data-location-value") or "").strip()
        assert district_value
        district.evaluate(
            """(node) => {
                node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
            }"""
        )
        page.locator("[data-location-map-close]").click()
        expect(location_dialog).not_to_be_visible()
        page.wait_for_function(
            """(value) => {
                const input = Array.from(document.querySelectorAll('input[name="location_query"]'))
                  .find((node) => String(node.value || '').trim() === String(value || '').trim());
                return Boolean(input && input.checked);
            }""",
            arg=district_value,
        )
        expect(page.locator("[data-location-selected-summary]")).not_to_have_text(re.compile(r"0 districts selected", re.I))

        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function(
            """() => document.querySelector('[data-console-form-variant="property_search"]')?.dataset.propertyActiveStep === 'providers'"""
        )
        select_all_providers = page.locator('[data-checkbox-group-select-all="selected_platforms"]').first
        expect(select_all_providers).to_be_visible()
        select_all_providers.click()
        page.wait_for_function("""() => document.querySelectorAll('input[name="selected_platforms"]:checked').length > 0""")
        final_search_button = page.locator("[data-property-start-top]").first
        expect(final_search_button).to_be_visible()
        expect(final_search_button).to_have_text("Launch search")

        with page.expect_navigation(
            url=re.compile(r".*/app/properties\?run_id=.*"),
            wait_until="commit",
            timeout=30_000,
        ):
            with page.expect_response("**/app/api/property/search-runs") as start_response:
                final_search_button.click()
        assert start_response.value.ok
        run_id = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query).get("run_id", [""])[0]
        assert run_id
        page.wait_for_selector(
            "[data-pqx-finished-compare], [data-pqx-empty-results], [data-pqx-screenfit-target=\"run-progress\"], [data-workbench-results-table], [data-pqx-mobile-panel=\"results\"]",
            timeout=10_000,
        )
        _assert_no_horizontal_overflow(page)

        deadline = time.time() + 5.0
        while "property_search_preferences" not in observed and time.time() < deadline:
            time.sleep(0.05)
        assert observed["principal_id"] == "pq-greenfield-browser"
        preferences = dict(observed["property_search_preferences"])
        selected_locations = [str(value).strip().lower() for value in list(preferences.get("selected_location_values") or []) if str(value).strip()]
        assert preferences["country_code"] == "AT"
        assert preferences["region_code"] == "vienna"
        assert preferences["full_region_scope"] is False
        assert district_value.lower() in selected_locations

        open_shortlist = page.get_by_role("link", name="Shortlist").first
        expect(open_shortlist).to_be_visible()
        open_shortlist.click()
        page.wait_for_url(re.compile(r".*/app/shortlist\?run_id=.*"), wait_until="commit", timeout=5_000)
        best_match = page.locator("[data-workbench-row]", has_text="Altbau near U6").first
        expect(best_match).to_be_visible()
        expect(best_match.get_by_role("link", name="3D tour")).to_be_visible()
        tour_href = str(best_match.get_by_role("link", name="3D tour").get_attribute("href") or "").strip()
        assert tour_href.endswith("/tours/altbau-u6/control/3dvista")
        _assert_no_horizontal_overflow(page)

        tour_page = context.new_page()
        try:
            tour_entry = tour_href if tour_href.startswith("http") else f"{base_url}{tour_href}"
            response = tour_page.goto(
                f"{tour_entry}?pane=floorplan-pane",
                wait_until="domcontentloaded",
            )
            assert response is not None and response.ok
            expect(tour_page.locator("body", has_text="Altbau near U6")).to_be_visible()
            expect(tour_page.locator("#stage-image")).to_be_visible()
        finally:
            tour_page.close()

        monkeypatch.setattr(
            landing_view_models,
            "_forward_geocode_preview_point",
            lambda label: None,
        )
        monkeypatch.setattr(
            landing_view_models,
            "_build_scope_boundary_preview",
            lambda **kwargs: {
                "image_url": family_map_preview_url,
                "summary": "Tiergarten",
                "district_rows": family_map_overlay,
            },
        )
        family_row = page.locator("[data-workbench-row]", has_text="Family flat near Tiergarten").first
        expect(family_row).to_be_visible()
        family_row.get_by_role("link", name="Open property", exact=True).click()
        page.wait_for_url(re.compile(r".*/app/research/[^?]+.*"), wait_until="commit", timeout=5_000)
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator(".prd-media-frame")).to_be_visible()
        expect(page.locator("[data-prd-hero-image]")).to_have_attribute("src", family_diorama_url)
        _assert_no_horizontal_overflow(page)

        map_open = page.locator("[data-prd-map-open]").first
        expect(map_open).to_be_visible()
        page.wait_for_function(
            """() => {
                const image = document.querySelector('[data-prd-map-open] img');
                return Boolean(image && image.complete && image.naturalWidth > 0);
            }"""
        )
        map_open.click()
        map_lightbox = page.locator("[data-prd-map-lightbox]").first
        expect(map_lightbox).to_be_visible()
        page.wait_for_function(
            """() => {
                const image = document.querySelector('[data-prd-map-lightbox-image]');
                return Boolean(image && image.complete && image.naturalWidth > 0);
            }"""
        )
        page.wait_for_function("""() => document.querySelectorAll('[data-prd-map-lightbox-overlay] path').length > 0""")
        assert page.locator("[data-prd-map-lightbox-overlay] path").count() >= 1
        page.locator("[data-prd-map-close]").click()
        expect(map_lightbox).not_to_be_visible()

        request_walkthrough = page.get_by_role(
            "button",
            name=re.compile("Request camera walkthrough", re.I),
        ).first
        expect(request_walkthrough).to_be_visible()
        request_walkthrough.click()
        with page.expect_response("**/app/api/signals/willhaben/property-tour", timeout=5_000) as visual_response:
            _choose_research_visual_style(page, accept_external_processing=True)
        assert visual_response.value.ok
        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "flythrough"
        assert payload["run_id"] == run_id
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
        assert payload["external_processing_consent_granted"] is True
        assert "urban jungle" in str(payload.get("diorama_style_hint") or "").lower()
        expect(page.locator("[data-prd-visual-status]")).to_contain_text("queued after your request", timeout=5_000)
        expect(page.locator("[data-prd-visual-eta]")).to_contain_text("about 8 min", timeout=5_000)

        open_walkthrough = page.get_by_role("button", name=re.compile("Open walkthrough", re.I)).first
        expect(open_walkthrough).to_be_visible(timeout=5_000)
        assert visual_status_polls >= 1
        walkthrough_href = str(open_walkthrough.get_attribute("data-pw-visual-href") or "").strip()
        assert walkthrough_href.endswith("/tours/altbau-u6?pane=flythrough-pane&autoplay=1")
        open_walkthrough.click()
        page.wait_for_url(re.compile(r".*/tours/altbau-u6(?:\\?.*)?$"), wait_until="commit", timeout=10_000)
        video = page.locator("#flythrough-video, #tour-video").first
        expect(video).to_be_visible()
        page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video?.play?.()?.catch(() => null) || null;
            }"""
        )
        page.wait_for_timeout(1800)
        video_state = page.evaluate(
            """() => {
                const video = document.getElementById('flythrough-video') || document.getElementById('tour-video');
                return video ? {
                    currentTime: video.currentTime,
                    duration: video.duration,
                    readyState: video.readyState,
                    videoWidth: video.videoWidth,
                } : null;
            }"""
        )
        assert video_state is not None
        assert video_state["readyState"] >= 2
        assert video_state["videoWidth"] >= 640
        assert video_state["currentTime"] > 0.2
        assert video_state["duration"] >= 2.5
        assert _video_frame_brightness(page) > 10.0
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()


_RENTER_VALUE_LOOP_CASES: tuple[dict[str, object], ...] = (
    {
        "country_code": "AT",
        "region_code": "vienna",
        "provider_id": "willhaben",
        "source_label": "Willhaben Austria",
        "title": "Vienna rental near U4",
        "runner_title": "Vienna rental near Augarten",
        "property_url": (
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/"
            "wien/wien-1040-wieden/vienna-rental-u4"
        ),
        "runner_url": (
            "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/"
            "wien/wien-1020-leopoldstadt/vienna-rental-augarten"
        ),
        "postal_name": "1040 Wien",
        "price_display": "EUR 1,850 / month",
        "total_rent_eur": 1850.0,
        "tour_url": "",
        "tour_mode": "honest_unavailable",
    },
)


@pytest.mark.parametrize(
    "market_case",
    _RENTER_VALUE_LOOP_CASES,
    ids=lambda market_case: str(market_case["country_code"]).lower(),
)
def test_propertyquarry_renter_value_loop_survives_logout_and_relogin(
    market_case: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    browser: Browser,
    propertyquarry_browser_server: dict[str, object],
) -> None:
    """Prove the candidate-bound renter loop without implying live-provider delivery."""

    country_code = str(market_case["country_code"])
    region_code = str(market_case["region_code"])
    provider_id = str(market_case["provider_id"])
    title = str(market_case["title"])
    runner_title = str(market_case["runner_title"])
    property_url = str(market_case["property_url"])
    runner_url = str(market_case["runner_url"])
    tour_url = str(market_case["tour_url"])
    observed: dict[str, object] = {}
    status_calls: dict[str, int] = {}

    primary_candidate = {
        "candidate_ref": f"{country_code.lower()}-renter-primary",
        "title": title,
        "property_url": property_url,
        "source_label": str(market_case["source_label"]),
        "source_ref": f"{provider_id}:{country_code.lower()}-renter-primary",
        "listing_mode": "rent",
        "match_score": 94,
        "fit_summary": "Personal fit 94/100 · shortlist · Rent, transit, and floor-plan fit.",
        "recommendation": "shortlist",
        "review_url": f"/app/handoffs/human_task:{country_code.lower()}-renter-primary",
        "tour_url": tour_url,
        "match_reasons": ["Monthly rent, transit, and layout fit the saved renter brief."],
        "mismatch_reasons": (
            []
            if tour_url
            else ["No first-party tour is available yet; request one explicitly if useful."]
        ),
        "property_facts": {
            "listing_mode": "rent",
            "price_display": str(market_case["price_display"]),
            "total_rent_eur": float(market_case["total_rent_eur"]),
            "rooms": 3,
            "area_m2": 78,
            "area_sqm": 78,
            "postal_name": str(market_case["postal_name"]),
            "has_floorplan": True,
            "floorplan_count": 1,
            "nearest_subway_m": 420,
        },
    }
    runner_candidate = {
        "candidate_ref": f"{country_code.lower()}-renter-runner-up",
        "title": runner_title,
        "property_url": runner_url,
        "source_label": str(market_case["source_label"]),
        "source_ref": f"{provider_id}:{country_code.lower()}-renter-runner-up",
        "listing_mode": "rent",
        "match_score": 81,
        "fit_summary": "Personal fit 81/100 · compare · Rent fits, but the route is longer.",
        "recommendation": "shortlist",
        "review_url": f"/app/handoffs/human_task:{country_code.lower()}-renter-runner-up",
        "tour_url": "",
        "match_reasons": ["The monthly rent remains inside the saved range."],
        "mismatch_reasons": ["Longer transit route and no first-party tour yet."],
        "property_facts": {
            "listing_mode": "rent",
            "price_display": "EUR 2,050 / month",
            "total_rent_eur": 2050.0,
            "rooms": 3,
            "area_m2": 74,
            "area_sqm": 74,
            "postal_name": str(market_case["postal_name"]),
            "has_floorplan": False,
        },
    }
    ranked_candidates = [primary_candidate, runner_candidate]

    def _fake_sync_direct_property_scout(
        self,
        *,
        principal_id: str,
        actor: str,
        selected_platforms: tuple[str, ...] = (),
        property_search_preferences: dict[str, object] | None = None,
        force_refresh: bool = False,
        max_results_per_source: int | None = None,
        progress_callback: callable | None = None,
    ) -> dict[str, object]:
        del self, force_refresh
        observed["principal_id"] = principal_id
        observed["actor"] = actor
        observed["selected_platforms"] = tuple(selected_platforms)
        observed["property_search_preferences"] = dict(property_search_preferences or {})
        observed["max_results_per_source"] = max_results_per_source
        if callable(progress_callback):
            progress_callback(
                step="sources_resolved",
                message=f"Resolved governed {country_code} renter providers.",
                status="in_progress",
                steps_delta=1,
                summary_updates={"sources_total": 1},
            )
            progress_callback(
                step="completed",
                message="Deterministic renter fixture dispatch completed.",
                status="processed",
                steps_delta=1,
                summary_updates={"listing_total": 4, "high_fit_total": 2},
            )
        return {
            "generated_at": "2026-07-19T10:00:00+00:00",
            "status": "processed",
            "sources_total": 1,
            "listing_total": 4,
            "review_created_total": 0,
            "review_existing_total": 2,
            "notified_total": 0,
            "email_notified_total": 0,
            "tour_created_total": 0,
            "tour_existing_total": 1 if tour_url else 0,
            "high_fit_total": 2,
            "watch_notified_total": 0,
            "sources": [],
        }

    def _renter_run_status(
        self,
        *,
        principal_id: str,
        run_id: str,
        lightweight: bool = False,
    ) -> dict[str, object]:
        del self, lightweight
        assert principal_id == "pq-greenfield-browser"
        call_index = status_calls.get(run_id, 0)
        status_calls[run_id] = call_index + 1
        processed = call_index >= 2
        summary: dict[str, object] = {
            "sources_total": 1,
            "listing_total": 4 if processed else 1,
            "ranked_total": 2 if processed else 0,
            "high_fit_total": 2 if processed else 0,
            "tour_existing_total": 1 if processed and tour_url else 0,
            "ranked_candidates": ranked_candidates if processed else [],
            "sources": [
                {
                    "source_key": provider_id,
                    "source_label": str(market_case["source_label"]),
                    "status": "scanned" if processed else "in_progress",
                    "listing_total": 4 if processed else 1,
                    "high_fit_total": 2 if processed else 0,
                    "top_candidates": ranked_candidates if processed else [],
                }
            ],
        }
        return {
            "run_id": run_id,
            "principal_id": principal_id,
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "status": "processed" if processed else "in_progress",
            "progress": 100 if processed else 46,
            "message": (
                "Two ranked rental homes are ready."
                if processed
                else "Scanning the selected renter providers."
            ),
            "generated_at": "2026-07-19T10:00:00+00:00",
            "updated_at": "2026-07-19T10:00:04+00:00" if processed else "2026-07-19T10:00:01+00:00",
            "selected_platforms": list(observed.get("selected_platforms") or [provider_id]),
            "property_search_preferences": {
                "country_code": country_code,
                "region_code": region_code,
                "listing_mode": "rent",
                "search_goal": "home",
                "selected_platforms": list(observed.get("selected_platforms") or [provider_id]),
                "preference_person_id": "elisabeth",
            },
            "summary": summary,
            "events": [
                {
                    "step": "sources_resolved",
                    "message": f"Resolved governed {country_code} renter providers.",
                    "status": "in_progress",
                },
                {
                    "step": "completed" if processed else "provider_scan",
                    "message": (
                        "Two ranked rental homes are ready."
                        if processed
                        else "Scanning the selected renter providers."
                    ),
                    "status": "processed" if processed else "in_progress",
                },
            ],
        }

    monkeypatch.setattr(ProductService, "sync_direct_property_scout", _fake_sync_direct_property_scout)
    monkeypatch.setattr(ProductService, "get_property_search_run_status", _renter_run_status)
    monkeypatch.setattr(landing_view_models, "_forward_geocode_preview_point", lambda label: None)

    base_url = str(propertyquarry_browser_server["base_url"])
    client = propertyquarry_browser_server["client"]
    assert isinstance(client, TestClient)
    context = _new_context(browser, mobile=False, width=1440, height=1000)
    page = context.new_page()
    page.add_init_script(
        """
        (() => {
          try {
            Object.defineProperty(window, 'isSecureContext', { get: () => true, configurable: true });
          } catch (_error) {}
          window.localStorage.setItem('propertyquarry.browserNotifications.enabled', '1');
          class RenterLoopNotification {
            static permission = 'granted';
            static requestPermission = async () => 'granted';
            constructor(title, options) {
              const rows = JSON.parse(window.localStorage.getItem('pq-renter-loop-notifications') || '[]');
              rows.push({
                title: String(title || ''),
                body: String(options?.body || ''),
                tag: String(options?.tag || ''),
                renotify: Boolean(options?.renotify),
              });
              window.localStorage.setItem('pq-renter-loop-notifications', JSON.stringify(rows));
              this.close = () => {};
              this.onclick = null;
            }
          }
          try {
            Object.defineProperty(window, 'Notification', { value: RenterLoopNotification, configurable: true });
          } catch (_error) {
            window.Notification = RenterLoopNotification;
          }
        })();
        """
    )
    try:
        _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
        response = page.goto(f"{base_url}/app/search", wait_until="networkidle")
        assert response is not None and response.ok

        page.select_option('select[name="search_goal"]', "home")
        page.select_option('select[name="listing_mode"]', "rent")
        page.select_option('select[name="country_code"]', country_code)
        page.wait_for_function(
            """(regionCode) => Array.from(document.querySelectorAll('select[name="region_code"] option'))
              .some((option) => option.value === regionCode)""",
            arg=region_code,
        )
        page.select_option('select[name="region_code"]', region_code)
        assert page.locator('select[name="search_goal"]').input_value() == "home"
        assert page.locator('select[name="listing_mode"]').input_value() == "rent"
        assert page.locator('select[name="country_code"]').input_value() == country_code
        assert page.locator('select[name="region_code"]').input_value() == region_code

        page.locator('[data-property-step-trigger="providers"]').click()
        page.wait_for_function(
            """() => document.querySelector(
              '[data-console-form-variant="property_search"]'
            )?.dataset.propertyActiveStep === 'providers'"""
        )
        select_all_providers = page.locator(
            '[data-checkbox-group-select-all="selected_platforms"]'
        ).first
        expect(select_all_providers).to_be_visible()
        select_all_providers.click()
        selected_before_dispatch = _settled_checked_provider_values(page)
        assert provider_id in selected_before_dispatch
        selected_provider_contract = page.locator(
            'input[name="selected_platforms"]:checked'
        ).evaluate_all(
            """nodes => nodes.map((node) => ({
              value: String(node.value || ''),
              country: String(node.dataset.countryCode || ''),
              modes: String(node.dataset.listingModes || '').split(',').filter(Boolean),
              disabled: Boolean(node.disabled),
            }))"""
        )
        assert selected_provider_contract
        assert all(not row["disabled"] for row in selected_provider_contract)
        assert all(row["country"] in {"", country_code} for row in selected_provider_contract)
        assert all(not row["modes"] or "rent" in row["modes"] for row in selected_provider_contract)

        with page.expect_navigation(
            url=re.compile(r".*/app/properties\?run_id=.*"),
            wait_until="commit",
            timeout=30_000,
        ):
            with page.expect_response("**/app/api/property/search-runs") as start_response:
                page.locator("[data-property-start-top]").click()
        assert start_response.value.ok
        run_id = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query).get("run_id", [""])[0]
        assert run_id

        primary_row = page.locator("[data-workbench-row]", has_text=title).first
        expect(primary_row).to_be_visible(timeout=12_000)
        expect(page.locator("[data-workbench-row]", has_text=runner_title).first).to_be_visible()
        visible_ranked_titles = page.locator(
            "[data-workbench-row]:visible .pqx-result-title"
        ).all_inner_texts()
        assert visible_ranked_titles[:2] == [title, runner_title], visible_ranked_titles
        expect(primary_row).to_contain_text(re.compile(r"month|Monat", re.I))

        deadline = time.time() + 6.0
        while "property_search_preferences" not in observed and time.time() < deadline:
            time.sleep(0.05)
        assert observed["principal_id"] == "pq-greenfield-browser"
        assert list(observed["selected_platforms"]) == selected_before_dispatch
        dispatched_preferences = dict(observed["property_search_preferences"])
        assert dispatched_preferences["search_goal"] == "home"
        assert dispatched_preferences["listing_mode"] == "rent"
        assert dispatched_preferences["country_code"] == country_code
        assert dispatched_preferences["region_code"] == region_code
        assert dispatched_preferences["selected_platforms"] == selected_before_dispatch

        page.wait_for_function(
            """(runId) => JSON.parse(
              window.localStorage.getItem('pq-renter-loop-notifications') || '[]'
            ).some((row) => row.tag === `propertyquarry-run-${runId}`)""",
            arg=run_id,
            timeout=8_000,
        )
        delivered_notifications = page.evaluate(
            "JSON.parse(window.localStorage.getItem('pq-renter-loop-notifications') || '[]')"
        )
        delivered = next(
            row
            for row in delivered_notifications
            if row["tag"] == f"propertyquarry-run-{run_id}"
        )
        assert delivered == {
            "title": "PropertyQuarry results are ready",
            "body": "2 matching homes ready.",
            "tag": f"propertyquarry-run-{run_id}",
            "renotify": False,
        }

        shortlist_link = page.locator(
            'a[href^="/app/shortlist"][href*="run_id="]:visible'
        ).first
        expect(shortlist_link).to_be_visible()
        shortlist_link.click()
        page.wait_for_url(re.compile(r".*/app/shortlist\?run_id=.*"), wait_until="commit")
        primary_row = page.locator("[data-workbench-row]", has_text=title).first
        expect(primary_row).to_be_visible()

        if market_case["tour_mode"] == "first_party":
            tour_link = primary_row.get_by_role("link", name="3D tour").first
            expect(tour_link).to_be_visible()
            observed_tour_href = str(tour_link.get_attribute("href") or "").strip()
            tour_entry = urllib.parse.urljoin(base_url, observed_tour_href)
            tour_parts = urllib.parse.urlsplit(tour_entry)
            base_parts = urllib.parse.urlsplit(base_url)
            assert (tour_parts.scheme, tour_parts.netloc) == (
                base_parts.scheme,
                base_parts.netloc,
            )
            assert tour_parts.path == tour_url
            tour_page = context.new_page()
            try:
                tour_response = tour_page.goto(
                    f"{tour_entry}?pane=floorplan-pane",
                    wait_until="domcontentloaded",
                )
                assert tour_response is not None and tour_response.ok
                expect(tour_page.locator("body", has_text="Altbau near U6")).to_be_visible()
                expect(tour_page.locator("#stage-image")).to_be_visible()
            finally:
                tour_page.close()
        else:
            expect(primary_row.locator('a[href*="/tours/"]')).to_have_count(0)

        packet_path = str(primary_row.get_attribute("data-candidate-packet-url") or "").strip()
        assert packet_path.startswith("/app/research/")
        packet_link = primary_row.locator('a[href*="/app/research/"]').first
        expect(packet_link).to_be_visible()
        packet_link.click()
        page.wait_for_url(re.compile(r".*/app/research/[^?]+.*"), wait_until="commit")
        packet_url = page.url
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("body", has_text=title)).to_be_visible()
        expect(page.locator("body", has_text=re.compile(r"month|Monat", re.I))).to_be_visible()

        if market_case["tour_mode"] == "honest_unavailable":
            expect(page.locator('a[href*="/tours/"]')).to_have_count(0)
            request_tour = page.locator("[data-pw-visual-request]").first
            expect(request_tour).to_be_visible()
            assert str(request_tour.get_attribute("data-property-url") or "") == property_url
            expect(page.get_by_text("On request.", exact=True)).to_be_visible()
            expect(page.locator("body")).to_contain_text(
                "No first-party tour is available yet; request one explicitly if useful."
            )

        feedback_root = page.locator("[data-object-feedback]").first
        feedback_payload = json.loads(
            str(feedback_root.get_attribute("data-object-feedback-payload") or "{}")
        )
        save_endpoint = str(feedback_payload.get("save_endpoint") or "")
        assert save_endpoint.endswith("/preference-profile/property-feedback")
        feedback_root.locator('[data-object-feedback-reaction="like"]').click()
        with page.expect_response("**/preference-profile/property-feedback") as feedback_response_info:
            feedback_root.locator("[data-object-feedback-save]").click()
        feedback_response = feedback_response_info.value
        assert feedback_response.ok
        feedback_body = feedback_response.json()
        assert feedback_body["status"] == "recorded"
        assert feedback_body["reaction"] == "like"
        assert feedback_body["structured_feedback_status"] == "recorded"
        expect(feedback_root.locator("[data-object-feedback-status]")).to_contain_text(
            "Saved. Profile now has"
        )

        response = page.goto(
            f"{base_url}/app/properties/notifications/preview?template=search_results_ready",
            wait_until="networkidle",
        )
        assert response is not None and response.ok
        expect(page.get_by_role("heading", name="Email preview")).to_be_visible()
        expect(page.get_by_role("heading", name="PropertyQuarry found 2 strong matches")).to_be_visible()
        preview_cta = page.frame_locator("iframe").get_by_role("link", name="Open shortlist")
        expect(preview_cta).to_be_visible()
        preview_href = str(preview_cta.get_attribute("href") or "")
        assert re.search(r"/app/shortlist\?run_id=[^&]+", preview_href)

        response = page.goto(f"{base_url}/app/account", wait_until="networkidle")
        assert response is not None and response.ok
        logout_form = page.locator("[data-account-page-sign-out]")
        expect(logout_form).to_be_visible()
        logout_form.locator('button[type="submit"]').click()
        page.wait_for_load_state("domcontentloaded")
        assert not any(
            cookie["name"] == "ea_workspace_session"
            for cookie in context.cookies()
        )
        assert page.locator("[data-account-page-sign-out]").count() == 0

        _issue_browser_workspace_session(client=client, context=context, base_url=base_url)
        assert any(
            cookie["name"] == "ea_workspace_session" and cookie["value"]
            for cookie in context.cookies()
        )
        response = page.goto(packet_url, wait_until="domcontentloaded")
        assert response is not None and response.ok
        expect(page.locator("[data-property-research-detail]")).to_be_visible()
        expect(page.locator("body", has_text=title)).to_be_visible()

        learning_endpoint = save_endpoint.removesuffix("/property-feedback") + "/learning-summary"
        durable_feedback = page.evaluate(
            """async (endpoint) => {
              const response = await fetch(endpoint, {
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { accept: 'application/json' },
              });
              return { status: response.status, body: await response.json() };
            }""",
            arg=learning_endpoint,
        )
        assert durable_feedback["status"] == 200
        recent_feedback = durable_feedback["body"]["recent_feedback"]
        assert any(
            row["reaction"] == "like"
            and row["event_type"] == "listing_feedback_like"
            and row["object_id"] == property_url
            for row in recent_feedback
        ), recent_feedback
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()
