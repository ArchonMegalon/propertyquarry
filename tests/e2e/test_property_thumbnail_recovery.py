from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Route


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


def test_thumbnail_controller_retries_bounded_chain_then_falls_back_to_placeholder(
    browser: Browser,
) -> None:
    html = f"""
      <!doctype html>
      <html>
        <body>
          <main data-property-decision-workbench>
            <script type="application/json" data-property-workbench-json>{{}}</script>
            <span data-property-workspace-meta="{{}}"></span>
            <span id="recovers" class="pqx-thumb" data-pqx-thumbnail
                  data-pqx-thumbnail-fallbacks='["/missing-first.png", "/fallback.png"]'>
              <img src="{_ONE_PIXEL_GIF}" data-pqx-thumbnail-image>
            </span>
            <span id="exhausts" class="pqx-thumb" data-pqx-thumbnail
                  data-pqx-thumbnail-fallbacks='["/missing-first.png", "/missing.png"]'>
              <img src="{_ONE_PIXEL_GIF}" data-pqx-thumbnail-image>
            </span>
          </main>
        </body>
      </html>
    """

    def serve(route: Route) -> None:
        if route.request.url.endswith("/fallback.png"):
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
    finally:
        context.close()
