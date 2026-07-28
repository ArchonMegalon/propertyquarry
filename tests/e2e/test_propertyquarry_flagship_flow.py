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
_video_frame_brightness = _GREENFIELD_MODULE._video_frame_brightness


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
        final_search_button = page.locator("[data-property-step-next]").first
        expect(final_search_button).to_be_visible()
        expect(final_search_button).to_have_text("Search")

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

        request_walkthrough = page.get_by_role("button", name=re.compile("Request walkthrough", re.I)).first
        expect(request_walkthrough).to_be_visible()
        request_walkthrough.click()
        with page.expect_response("**/app/api/signals/willhaben/property-tour", timeout=5_000) as visual_response:
            _choose_research_visual_style(page)
        assert visual_response.value.ok
        assert len(visual_requests) == 1
        payload = visual_requests[0]
        assert payload["request_kind"] == "flythrough"
        assert payload["run_id"] == run_id
        assert payload["auto_deliver"] is False
        assert payload["allow_floorplan_only"] is True
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
