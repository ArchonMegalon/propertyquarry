from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Route

from app.api.routes import landing as landing_routes


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_ONE_PIXEL_GIF = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)


def _thumbnail_controller_source() -> str:
    source = (
        Path(__file__).resolve().parents[2]
        / "ea/app/templates/app/_property_workbench_script.html"
    ).read_text(encoding="utf-8")
    thumbnail_controller, separator, _remainder = source.partition(
        "    const clientLocale ="
    )
    assert separator
    return f"{thumbnail_controller}  }})();"


def _fast_ranked_controller_source() -> str:
    source = (
        Path(__file__).resolve().parents[2]
        / "ea/app/templates/app/property_ranked_run_fast.html"
    ).read_text(encoding="utf-8")
    _head, separator, remainder = source.partition("<script>\n")
    assert separator
    controller, separator, _tail = remainder.partition("</script>")
    assert separator
    return controller


def test_thumbnail_controller_retries_bounded_chain_then_falls_back_to_placeholder(
    browser: Browser,
) -> None:
    html = f"""
      <!doctype html>
      <html>
        <body>
          <main data-property-decision-workbench>
            <script type="application/json" data-property-workbench-json>{{"run":{{"run_id":"run-thumbnail"}}}}</script>
            <span data-property-workspace-meta="{{}}"></span>
            <span id="recovers" class="pqx-thumb" data-pqx-thumbnail
                  data-pqx-thumbnail-fallbacks='["/missing-first.png", "/fallback.png"]'>
              <img src="{_ONE_PIXEL_GIF}" data-pqx-thumbnail-image>
            </span>
            <span id="exhausts" class="pqx-thumb" data-pqx-thumbnail
                  data-pqx-thumbnail-fallbacks='["/missing-first.png", "/missing.png"]'>
              <img src="{_ONE_PIXEL_GIF}" data-pqx-thumbnail-image>
            </span>
            <article data-candidate-ref="candidate-refresh">
              <span id="refreshes" class="pqx-thumb" data-pqx-thumbnail
                    data-candidate-ref="candidate-refresh">
                <img src="{_ONE_PIXEL_GIF}" data-pqx-thumbnail-image>
              </span>
            </article>
          </main>
        </body>
      </html>
    """

    def serve(route: Route) -> None:
        if route.request.url.endswith("/fallback.png"):
            route.fulfill(status=200, content_type="image/png", body=_ONE_PIXEL_PNG)
            return
        if "/app/api/property/candidates/candidate-refresh/preview-refresh?" in route.request.url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"candidate":{"preview_image_url":"/refreshed.png"}}',
            )
            return
        if route.request.url.endswith("/refreshed.png"):
            route.fulfill(status=200, content_type="image/png", body=_ONE_PIXEL_PNG)
            return
        if route.request.url.endswith(("/missing-first.png", "/missing.png")):
            route.fulfill(status=404, content_type="text/plain", body="missing")
            return
        route.fulfill(status=200, content_type="text/html", body=html)

    context = browser.new_context()
    page = context.new_page()
    page.route("https://thumbnail.test/**", serve)
    try:
        page.goto("https://thumbnail.test/workbench")
        page.wait_for_function(
            "[...document.images].every((image) => image.complete && image.naturalWidth > 0)"
        )
        page.add_script_tag(content=_thumbnail_controller_source())

        page.locator("#recovers img").dispatch_event("error")
        page.wait_for_function(
            """() => {
              const host = document.querySelector('#recovers');
              const image = host.querySelector('img');
              return image.dataset.pqxThumbnailFallbackAttempted === '1'
                && image.dataset.pqxThumbnailFallbackIndex === '2'
                && image.naturalWidth > 0
                && !image.hidden
                && !host.classList.contains('is-recovering')
                && !host.classList.contains('is-unavailable');
            }"""
        )

        page.locator("#exhausts img").dispatch_event("error")
        page.wait_for_function(
            """() => {
              const host = document.querySelector('#exhausts');
              const image = host.querySelector('img');
              return image.dataset.pqxThumbnailFallbackAttempted === '1'
                && image.dataset.pqxThumbnailFallbackIndex === '2'
                && image.hidden
                && host.classList.contains('is-unavailable')
                && !host.classList.contains('is-recovering');
            }"""
        )

        page.locator("#refreshes img").dispatch_event("error")
        page.wait_for_function(
            """() => {
              const host = document.querySelector('#refreshes');
              const image = host.querySelector('img');
              return image.dataset.pqxThumbnailDetailRefreshAttempted === '1'
                && image.src.endsWith('/refreshed.png')
                && image.naturalWidth > 0
                && !image.hidden
                && !host.classList.contains('is-recovering')
                && !host.classList.contains('is-unavailable');
            }"""
        )
    finally:
        context.close()


def test_fast_ranked_results_promote_listing_thumbnail_and_recover_with_fallback(
    browser: Browser,
) -> None:
    html = """
      <!doctype html>
      <html>
        <body>
          <main data-pq-fast-ranked-run data-status-url="" data-search-href="/app/search">
            <span data-pq-fast-subtitle></span>
            <section data-pq-fast-status-card>
              <strong data-pq-fast-status-title></strong>
              <span data-pq-fast-status-detail></span>
            </section>
            <section data-pq-fast-list></section>
            <script type="application/json" data-pq-fast-initial-payload>
              {
                "status": "completed",
                "summary": {
                  "status": "completed",
                  "ranked_candidates": [
                    {
                      "candidate_ref": "fast-recovers",
                      "title": "Recovered home",
                      "packet_url": "/app/research/fast-recovers",
                      "preview_image_url": "/missing-primary.png",
                      "preview_image_fallback_urls": ["/fallback.png"]
                    },
                    {
                      "candidate_ref": "fast-exhausts",
                      "title": "Unavailable home",
                      "packet_url": "/app/research/fast-exhausts",
                      "preview_image_url": "/missing-primary.png",
                      "preview_image_fallback_urls": ["/missing-fallback.png"]
                    },
                    {
                      "candidate_ref": "fast-rejects-provider-chrome",
                      "title": "Real listing image",
                      "packet_url": "/app/research/fast-rejects-provider-chrome",
                      "preview_image_url": "/img/upselling/icon-bump.png",
                      "preview_image_fallback_urls": ["/fallback.png"]
                    }
                  ]
                }
              }
            </script>
          </main>
        </body>
      </html>
    """

    def serve(route: Route) -> None:
        if route.request.url.endswith("/fallback.png"):
            route.fulfill(status=200, content_type="image/png", body=_ONE_PIXEL_PNG)
            return
        if route.request.url.endswith(("/missing-primary.png", "/missing-fallback.png")):
            route.fulfill(status=404, content_type="text/plain", body="missing")
            return
        route.fulfill(status=200, content_type="text/html", body=html)

    context = browser.new_context()
    page = context.new_page()
    page.route("https://fast-thumbnail.test/**", serve)
    try:
        page.goto("https://fast-thumbnail.test/app/shortlist/run/run-thumbnail")
        page.add_script_tag(content=_fast_ranked_controller_source())

        page.wait_for_function(
            """() => {
              const rows = document.querySelectorAll('.pq-fast-row');
              const image = rows[0]?.querySelector('[data-pq-fast-thumbnail-image]');
              return rows.length === 3
                && image?.src.endsWith('/fallback.png')
                && image.naturalWidth > 0
                && rows[0].querySelector('.pq-fast-thumb')?.dataset.visualKind === 'preview';
            }"""
        )
        recovered_image = page.locator(".pq-fast-row").nth(0).locator("img")
        assert recovered_image.get_attribute("referrerpolicy") == "no-referrer"

        page.wait_for_function(
            """() => {
              const media = document.querySelectorAll('.pq-fast-row')[1]
                ?.querySelector('.pq-fast-thumb');
              return media?.classList.contains('no-thumb')
                && media.dataset.visualKind === 'placeholder'
                && !media.querySelector('img')
                && media.textContent.includes('Property');
            }"""
        )

        page.wait_for_function(
            """() => {
              const image = document.querySelectorAll('.pq-fast-row')[2]
                ?.querySelector('[data-pq-fast-thumbnail-image]');
              return image?.src.endsWith('/fallback.png')
                && image.naturalWidth > 0
                && !image.src.includes('/img/upselling/');
            }"""
        )
    finally:
        context.close()


def test_fast_ranked_first_paint_uses_safe_listing_preview_without_diorama() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "ea/app/templates/app/property_ranked_run_fast.html"
    ).read_text(encoding="utf-8")

    assert "{% set thumb_href = diorama_href or preview_href %}" in source
    assert 'referrerpolicy="no-referrer" data-pq-fast-thumbnail-image' in source
    assert "const thumbHref = thumbnailHrefs[0] || previewHref;" in source
    assert "'/img/upselling/'" in source
    assert "'/plus-insider-locked.'" in source


@pytest.mark.parametrize(
    "provider_chrome_url",
    [
        "https://cache.willhaben.at/img/upselling/icon-bump.png",
        "https://www.immobilienscout24.at/expose/assets/plus-insider-locked.webp",
    ],
)
def test_fast_ranked_first_paint_rejects_provider_chrome_and_promotes_real_media(
    provider_chrome_url: str,
) -> None:
    listing_image_url = "https://images.example.test/listing-home.webp"
    prepared = landing_routes._propertyquarry_prepare_run_payload(
        product=object(),
        backfill_cached_previews=False,
        run_payload={
            "run_id": "run-provider-chrome",
            "status": "completed",
            "summary": {
                "status": "completed",
                "ranked_candidates": [
                    {
                        "candidate_ref": "provider-chrome-home",
                        "title": "Real home",
                        "property_url": "https://listing.example.test/home/1",
                        "preview_image_url": provider_chrome_url,
                        "property_facts": {
                            "media_urls_json": [provider_chrome_url, listing_image_url],
                        },
                    }
                ],
            },
        },
    )

    candidate = prepared["summary"]["ranked_candidates"][0]
    safe_thumbnail_urls = [
        candidate["preview_image_url"],
        *candidate.get("preview_image_fallback_urls", []),
    ]
    assert candidate["preview_image_url"].startswith(
        ("/app/api/property/map-preview/", "/app/api/property/map-previews/", "https://")
    )
    assert listing_image_url in safe_thumbnail_urls
    assert provider_chrome_url not in safe_thumbnail_urls
