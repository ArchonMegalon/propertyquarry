from __future__ import annotations

from contextlib import nullcontext

import pytest

from app.product import outbound_url_security
from app.product import service as product_service


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requested_bytes = 0

    def read(self, maximum: int) -> bytes:
        self.requested_bytes = maximum
        return self.payload[:maximum]


def test_property_scout_fetch_html_bounds_the_network_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"<html>safe</html>")
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES", "262144")
    monkeypatch.setattr(
        outbound_url_security,
        "open_guarded_url",
        lambda *_args, **_kwargs: nullcontext(response),
    )

    html = product_service._property_scout_fetch_html("https://example.test/listings")

    assert html == "<html>safe</html>"
    assert response.requested_bytes == 262145


def test_property_scout_fetch_html_rejects_an_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"x" * 262145)
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES", "262144")
    monkeypatch.setattr(
        outbound_url_security,
        "open_guarded_url",
        lambda *_args, **_kwargs: nullcontext(response),
    )

    with pytest.raises(
        ValueError,
        match=r"^property_scout_response_too_large:max_bytes=262144$",
    ):
        product_service._property_scout_fetch_html("https://example.test/listings")


def test_property_scout_response_limit_defaults_and_clamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES", raising=False)
    assert product_service._property_scout_response_max_bytes() == 8 * 1024 * 1024

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES", "1")
    assert product_service._property_scout_response_max_bytes() == 256 * 1024

    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES", str(64 * 1024 * 1024))
    assert product_service._property_scout_response_max_bytes() == 32 * 1024 * 1024
