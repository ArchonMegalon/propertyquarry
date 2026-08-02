from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Iterator

import pytest

from app.propertyquarry_release_probe import (
    PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER,
    propertyquarry_release_probe_signature,
)
from scripts import propertyquarry_live_authenticated_smoke as authenticated_smoke
from scripts import propertyquarry_live_mobile_surface_smoke as mobile_smoke
from scripts import propertyquarry_map_preview_flagship_gate as map_gate
from scripts.propertyquarry_live_http_security import normalized_origin, validated_live_base_origin
from scripts.propertyquarry_live_probe_auth import (
    live_probe_request_headers,
    release_probe_path_allowed,
)


_NOW = 1_800_000_000
_RELEASE_PROBE_SECRET = "propertyquarry-client-release-probe-secret-000001"
_RESEARCH_ROUTE = "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
_SHORTLIST_RUN_PATH = "/app/shortlist/run/run-gold-mobile"


@contextmanager
def _http_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_base_origin_requires_https_except_exact_loopback() -> None:
    assert validated_live_base_origin("https://propertyquarry.com") == "https://propertyquarry.com"
    assert validated_live_base_origin("http://127.0.0.1:8090") == "http://127.0.0.1:8090"
    with pytest.raises(ValueError, match="requires_https"):
        validated_live_base_origin("http://propertyquarry.com")
    with pytest.raises(ValueError, match="origin_only"):
        validated_live_base_origin("https://propertyquarry.com/app")
    with pytest.raises(ValueError, match="port_invalid"):
        validated_live_base_origin("https://propertyquarry.com:0")


def test_release_probe_headers_sign_only_exact_origin_and_allowlisted_route() -> None:
    authorized_origin = "https://propertyquarry.com"
    input_headers = {
        "Accept": "application/json",
        "Authorization": "Bearer legacy-token-must-not-survive",
        "X-EA-API-Token": "legacy-token-must-not-survive",
        "X-EA-Principal-ID": "caller-selected-principal",
        "Host": "caller-controlled-host",
    }
    nonce = "client-release-probe-exact-origin-0001"

    signed = live_probe_request_headers(
        url=f"{authorized_origin}{_RESEARCH_ROUTE}",
        authorized_origin=authorized_origin,
        headers=input_headers,
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="GET",
        timestamp=_NOW,
        nonce=nonce,
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )
    public = live_probe_request_headers(
        url=f"{authorized_origin}/health/ready",
        authorized_origin=authorized_origin,
        headers=input_headers,
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="GET",
        timestamp=_NOW,
        nonce="client-release-probe-public-route-0001",
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )
    cross_origin = live_probe_request_headers(
        url=f"https://images.example.test{_RESEARCH_ROUTE}",
        authorized_origin=authorized_origin,
        headers=input_headers,
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="GET",
        timestamp=_NOW,
        nonce="client-release-probe-cross-origin-0001",
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )

    expected_signature = propertyquarry_release_probe_signature(
        secret=_RELEASE_PROBE_SECRET,
        method="GET",
        path="/app/research/perf-candidate-1020",
        query_string="run_id=run-gold-mobile",
        timestamp=_NOW,
        nonce=nonce,
        origin=authorized_origin,
    )
    assert signed[PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER] == str(_NOW)
    assert signed[PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER] == nonce
    assert signed[PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER] == expected_signature
    assert signed["Accept"] == "application/json"
    for result in (signed, public, cross_origin):
        lowered = {str(name).lower() for name in result}
        assert "authorization" not in lowered
        assert "x-ea-api-token" not in lowered
        assert "x-ea-principal-id" not in lowered
    assert "host" not in {str(name).lower() for name in cross_origin}
    for result in (public, cross_origin):
        lowered = {str(name).lower() for name in result}
        assert PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER.lower() not in lowered
        assert PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER.lower() not in lowered
        assert PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER.lower() not in lowered


def test_release_probe_allowlists_exact_legacy_support_redirect_surface() -> None:
    assert release_probe_path_allowed("https://propertyquarry.com/app/support") is True
    assert release_probe_path_allowed("https://propertyquarry.com/app/settings/support") is True
    assert release_probe_path_allowed("https://propertyquarry.com/app/support/") is False
    assert release_probe_path_allowed("https://propertyquarry.com/app/support?next=account") is False

def test_release_probe_headers_bind_head_and_refuse_post() -> None:
    authorized_origin = "https://propertyquarry.com"
    nonce = "client-release-probe-head-method-0001"
    head = live_probe_request_headers(
        url=f"{authorized_origin}{_SHORTLIST_RUN_PATH}",
        authorized_origin=authorized_origin,
        headers={"Accept": "application/json"},
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="HEAD",
        timestamp=_NOW,
        nonce=nonce,
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )
    post = live_probe_request_headers(
        url=f"{authorized_origin}{_SHORTLIST_RUN_PATH}",
        authorized_origin=authorized_origin,
        headers={"Accept": "application/json"},
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="POST",
        timestamp=_NOW,
        nonce="client-release-probe-post-method-0001",
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )

    assert head[PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER] == (
        propertyquarry_release_probe_signature(
            secret=_RELEASE_PROBE_SECRET,
            method="HEAD",
            path=_SHORTLIST_RUN_PATH,
            query_string="",
            timestamp=_NOW,
            nonce=nonce,
            origin=authorized_origin,
        )
    )
    post_names = {str(name).lower() for name in post}
    assert PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER.lower() not in post_names
    assert PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER.lower() not in post_names
    assert PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER.lower() not in post_names


def test_release_probe_helper_drops_inherited_probe_headers_before_scoping() -> None:
    authorized_origin = "https://propertyquarry.com"
    inherited = {
        "Accept": "application/json",
        PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER: "1",
        PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER: "attacker-controlled-nonce",
        PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER: "f" * 64,
    }
    public = live_probe_request_headers(
        url=f"{authorized_origin}/health/ready",
        authorized_origin=authorized_origin,
        headers=inherited,
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="GET",
        timestamp=_NOW,
        nonce="client-release-probe-inherited-public-0001",
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )
    cross_origin = live_probe_request_headers(
        url="https://images.example.test/pixel.png",
        authorized_origin=authorized_origin,
        headers=inherited,
        release_probe_secret=_RELEASE_PROBE_SECRET,
        method="GET",
        timestamp=_NOW,
        nonce="client-release-probe-inherited-cross-0001",
        configured_routes=(_RESEARCH_ROUTE, _SHORTLIST_RUN_PATH),
    )

    for result in (public, cross_origin):
        lowered = {str(name).lower() for name in result}
        assert PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER.lower() not in lowered
        assert PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER.lower() not in lowered
        assert PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER.lower() not in lowered


def test_authenticated_billing_bridge_never_fetches_cross_origin_absolute_path() -> None:
    destination_requests: list[dict[str, str]] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            destination_requests.append({str(key): str(value) for key, value in self.headers.items()})
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with _http_server(DestinationHandler) as destination_origin:
        with _http_server(SourceHandler) as source_origin:
            fetch_calls: list[str] = []

            def credentialed_fetcher(url: str, timeout_seconds: float) -> dict[str, object]:
                fetch_calls.append(url)
                return authenticated_smoke.fetch_url(
                    url,
                    timeout_seconds=timeout_seconds,
                    api_token="sentinel-billing-token",
                    principal_id="principal-sensitive",
                    country_code="AT",
                )

            location = f"{destination_origin}/app/api/property/billing/bridge-launch"
            result = authenticated_smoke._resolve_billing_external_handoff(
                base_url=source_origin,
                location=location,
                fetcher=credentialed_fetcher,
                timeout_seconds=3,
            )

    assert result["external_location"] == location
    assert result["bridge_launch_used"] is False
    assert fetch_calls == []
    assert destination_requests == []


def test_mobile_authenticated_redirect_never_reaches_second_origin() -> None:
    destination_requests: list[dict[str, str]] = []
    source_requests: list[dict[str, str]] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            destination_requests.append({str(key): str(value) for key, value in self.headers.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"destination")

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with _http_server(DestinationHandler) as destination_origin:
        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                source_requests.append({str(key): str(value) for key, value in self.headers.items()})
                self.send_response(302)
                self.send_header("Location", f"{destination_origin}/capture")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        with _http_server(SourceHandler) as source_origin:
            result = mobile_smoke._http_get_for_smoke(
                f"{source_origin}/start",
                headers={
                    "Authorization": "Bearer sentinel-mobile-token",
                    "X-EA-API-Token": "sentinel-mobile-token",
                    "X-EA-Principal-ID": "principal-sensitive",
                },
                timeout_seconds=3,
                follow_redirects=True,
                authorized_origin=normalized_origin(source_origin),
            )

    assert result["status_code"] == 302
    assert result["redirect_blocked"] == "cross_origin"
    assert source_requests[0]["Authorization"] == "Bearer sentinel-mobile-token"
    assert destination_requests == []


def test_map_gate_authenticated_redirect_never_reaches_second_origin() -> None:
    destination_requests: list[dict[str, str]] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            destination_requests.append({str(key): str(value) for key, value in self.headers.items()})
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with _http_server(DestinationHandler) as destination_origin:
        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"{destination_origin}/capture")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        with _http_server(SourceHandler) as source_origin:
            result = map_gate._fetch(
                f"{source_origin}/start",
                timeout_seconds=3,
                api_token="sentinel-map-token",
                principal_id="principal-sensitive",
                authorized_origin=normalized_origin(source_origin),
            )

    assert result["status_code"] == 302
    assert result["redirect_blocked"] == "cross_origin"
    assert destination_requests == []


def test_map_gate_live_discovery_keeps_only_same_origin_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "http://127.0.0.1:8090"
    same_path = "/app/api/property/map-previews/" + "a" * 40 + ".png"
    external_url = "https://images.example.test/app/api/property/map-previews/" + "b" * 40 + ".png"

    monkeypatch.setattr(
        map_gate,
        "_fetch",
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "body": f'<img src="{same_path}"><img src="{external_url}">'.encode(),
        },
    )

    assert map_gate._discover_preview_urls(
        base_url=base_url,
        routes=["/app/search"],
        timeout_seconds=3,
        host_header="",
        api_token="sentinel-token",
        principal_id="principal",
    ) == [f"{base_url}{same_path}"]


def test_playwright_route_adds_auth_only_on_exact_origin() -> None:
    class Route:
        def __init__(self, url: str, *, inherited_auth: bool = False) -> None:
            request_headers = {"accept": "text/html"}
            if inherited_auth:
                request_headers.update(
                    {
                        "authorization": "Bearer inherited-browser-token",
                        "x-ea-api-token": "inherited-browser-token",
                        "x-ea-principal-id": "inherited-principal",
                    }
                )
            self.request = SimpleNamespace(url=url, method="GET", headers=request_headers)
            self.calls: list[dict[str, object]] = []

        def continue_(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    headers = {
        "Authorization": "Bearer sentinel-browser-token",
        "X-EA-API-Token": "sentinel-browser-token",
        "X-EA-Principal-ID": "principal-sensitive",
    }
    same_origin = Route("https://propertyquarry.com/app/search")
    other_origin = Route("https://images.example.test/pixel.png", inherited_auth=True)

    mobile_smoke._continue_playwright_route_with_origin_scoped_headers(
        same_origin,
        authorized_origin="https://propertyquarry.com",
        headers=headers,
    )
    mobile_smoke._continue_playwright_route_with_origin_scoped_headers(
        other_origin,
        authorized_origin="https://propertyquarry.com",
        headers=headers,
    )

    assert same_origin.calls[0]["headers"]["Authorization"] == "Bearer sentinel-browser-token"
    assert other_origin.calls == [{"headers": {"accept": "text/html"}}]


def test_authenticated_receipt_redacts_reflected_concrete_token() -> None:
    token = "sentinel-reflected-api-token"
    release_probe_secret = "sentinel-reflected-release-probe-secret-0001"

    def _fetcher(_url: str, _timeout: float) -> dict[str, object]:
        return {
            "status_code": 200,
            "headers": {
                "Content-Type": "text/html",
                "Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Permissions-Policy": "camera=(), microphone=()",
            },
            "body": f"reflected {token} and {release_probe_secret}".encode(),
            "final_url": (
                "https://propertyquarry.com/probe"
                f"?token={token}&release_probe={release_probe_secret}"
            ),
            "error": f"upstream reflected {token} and {release_probe_secret}",
        }

    receipt = authenticated_smoke.build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token=token,
        principal_id="principal-test",
        release_probe_secret=release_probe_secret,
        routes=("/probe",),
        retry_count=0,
        fetcher=_fetcher,
    )

    serialized = json.dumps(receipt, sort_keys=True)
    assert token not in serialized
    assert release_probe_secret not in serialized
    assert "[redacted-secret]" in serialized


@pytest.mark.parametrize(
    "route",
    (
        "/app/research/candidate.secret",
        "/app/research/candidate%2Fsecret",
        "/app/research/~candidate-secret",
        "/app/research/candidate.secret%2F~opaque?run_id=run-secret#fragment-secret",
        "/app/research/candidate-secret#fragment-secret",
    ),
)
def test_research_detail_route_is_redacted_in_logs_and_receipts(route: str) -> None:
    assert mobile_smoke._route_log_label(route) == "/app/research/[redacted]"
    assert mobile_smoke._redact_sensitive_receipt_text(route) == "/app/research/[redacted]"
