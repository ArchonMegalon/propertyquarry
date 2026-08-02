from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import propertyquarry_authenticated_performance_smoke as performance


RELEASE_PROBE_SECRET = "propertyquarry-test-release-probe-secret-0123456789"
EXPECTED_MANIFEST_SHA256 = "c123456789abcdef" * 4
EXPECTED_RELEASE_IDENTITY = {
    "commit_sha": "a" * 40,
    "image_digest": "sha256:" + ("b" * 64),
    "deployment_id": "propertyquarry-production",
    "manifest_sha256": EXPECTED_MANIFEST_SHA256,
}
OBSERVED_RELEASE_IDENTITY = {
    **EXPECTED_RELEASE_IDENTITY,
    "manifest_status": "complete",
    "manifest_sha256": EXPECTED_MANIFEST_SHA256,
    "replica_id": "propertyquarry-production-7f489d8d5d-k9r2p",
}
DOCUMENT_RELEASE_HEADERS = {
    "x-propertyquarry-release-commit": OBSERVED_RELEASE_IDENTITY["commit_sha"],
    "x-propertyquarry-release-image": OBSERVED_RELEASE_IDENTITY["image_digest"],
    "x-propertyquarry-release-deployment": OBSERVED_RELEASE_IDENTITY[
        "deployment_id"
    ],
    "x-propertyquarry-release-manifest-status": OBSERVED_RELEASE_IDENTITY[
        "manifest_status"
    ],
    "x-propertyquarry-release-manifest-sha256": OBSERVED_RELEASE_IDENTITY[
        "manifest_sha256"
    ],
    "x-propertyquarry-replica-id": OBSERVED_RELEASE_IDENTITY["replica_id"],
}
LIVE_VERSION_PAYLOAD = {
    "release_commit_sha": EXPECTED_RELEASE_IDENTITY["commit_sha"],
    "release_image_digest": EXPECTED_RELEASE_IDENTITY["image_digest"],
    "release_deployment_id": EXPECTED_RELEASE_IDENTITY["deployment_id"],
    "release_manifest_status": OBSERVED_RELEASE_IDENTITY["manifest_status"],
    "release_manifest_sha256": OBSERVED_RELEASE_IDENTITY["manifest_sha256"],
    "replica_id": OBSERVED_RELEASE_IDENTITY["replica_id"],
}
RELEASE_PROBE_HEADER_NAMES = {
    "x-propertyquarry-release-probe-timestamp",
    "x-propertyquarry-release-probe-nonce",
    "x-propertyquarry-release-probe-signature",
}


def _write_synthetic_chromium(
    path: Path,
    *,
    variant: bytes = b"stable",
) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = b"\x7fELF synthetic Chromium test executable " + variant
    payload = prefix + (b"\0" * (performance.MIN_CHROMIUM_EXECUTABLE_BYTES - len(prefix)))
    path.write_bytes(payload)
    path.chmod(0o755)
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": performance._sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


@pytest.fixture(scope="module")
def synthetic_chromium_executable(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, object]:
    return _write_synthetic_chromium(
        tmp_path_factory.mktemp("chromium-launch-binding")
        / "chromium-123.0"
        / "chrome"
    )


def _passing_release_identity_probe(**kwargs: object) -> dict[str, object]:
    assert kwargs["target_url"] == "https://propertyquarry.test/app/search"
    assert kwargs["expected_release_identity"] == EXPECTED_RELEASE_IDENTITY
    return {
        "status": "pass",
        "version_url": "https://propertyquarry.test/version",
        "status_code": 200,
        "tls_verified": True,
        "expected": dict(EXPECTED_RELEASE_IDENTITY),
        "observed": dict(OBSERVED_RELEASE_IDENTITY),
        "matches_expected": True,
        "error": "",
        "credential_persisted": False,
    }


def _install_live_version_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            body = json.dumps(payload).encode("utf-8")
            assert len(body) < limit
            return body

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> FakeResponse:
            captured["url"] = str(getattr(request, "full_url", ""))
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(
        performance.urllib.request,
        "build_opener",
        lambda *_args: FakeOpener(),
    )
    return captured


class _FakeRequest:
    def __init__(self, *, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type
        self.headers = {"accept": "text/html"}


class _FakeNavigationResponse:
    def __init__(self, *, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self._headers = dict(headers)

    def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _FakeRoute:
    def __init__(self, context: "_FakeContext", request: _FakeRequest) -> None:
        self.context = context
        self.request = request
        self.continued = False

    def continue_(self, *, headers: dict[str, str] | None = None) -> None:
        self.continued = True
        self.context.continued_requests.append(
            {
                "url": self.request.url,
                "resource_type": self.request.resource_type,
                "headers": dict(headers or self.request.headers),
            }
        )


class _FakeSession:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.commands: list[tuple[str, object]] = []

    def on(self, name: str, callback: object) -> None:
        self.callbacks[name] = callback

    def send(self, name: str, payload: object | None = None) -> None:
        self.commands.append((name, payload))

    def emit(self, name: str, payload: dict[str, object]) -> None:
        callback = self.callbacks.get(name)
        if callable(callback):
            callback(payload)


class _FakePage:
    def __init__(self, context: "_FakeContext") -> None:
        self.context = context
        self.url = "about:blank"
        self.navigation_urls: list[str] = []
        self.wait_timeouts_ms: list[int] = []
        self.session: _FakeSession | None = None
        self.target_navigation_count = 0
        self.default_timeout_ms = 0
        self.navigation_status = 200
        self.final_url_override = ""
        self.document_release_headers = dict(DOCUMENT_RELEASE_HEADERS)
        self.probe_nonce_ack_override: str | None = None
        self.cache_control_override: str | None = None
        self.suppress_probe_nonce_ack = False
        self.skip_target_request_interception = False
        self.warm_asset_cache_hit = True

    def set_default_navigation_timeout(self, timeout_ms: int) -> None:
        self.default_timeout_ms = timeout_ms

    def goto(self, url: str, **_kwargs: object) -> object:
        self.url = url
        self.navigation_urls.append(url)
        if url == "about:blank":
            return SimpleNamespace(status=200)
        request_headers = (
            {}
            if self.skip_target_request_interception and "/access/" not in url
            else self.context.dispatch_request(
                url=url,
                resource_type="document",
            )
        )
        if "/access/" in url:
            self.url = "https://propertyquarry.test/app"
            return SimpleNamespace(status=200)
        self.target_navigation_count += 1
        assert self.session is not None
        base_timestamp = float(self.target_navigation_count * 10)
        document_id = f"document-{self.target_navigation_count}"
        asset_id = f"asset-{self.target_navigation_count}"
        self.session.emit(
            "Network.requestWillBeSent",
            {
                "requestId": document_id,
                "timestamp": base_timestamp,
                "type": "Document",
                "request": {"url": url},
            },
        )
        self.session.emit(
            "Network.responseReceived",
            {
                "requestId": document_id,
                "response": {
                    "status": self.navigation_status,
                    "fromDiskCache": False,
                },
            },
        )
        self.session.emit(
            "Network.requestWillBeSent",
            {
                "requestId": asset_id,
                "timestamp": base_timestamp + 0.02,
                "type": "Stylesheet",
                "request": {
                    "url": "https://propertyquarry.test/assets/app.css?signature=do-not-serialize"
                },
            },
        )
        self.context.dispatch_request(
            url="https://propertyquarry.test/assets/app.css?signature=do-not-serialize",
            resource_type="stylesheet",
        )
        is_warm = self.target_navigation_count == 2
        asset_from_cache = is_warm and self.warm_asset_cache_hit
        self.session.emit(
            "Network.responseReceived",
            {
                "requestId": asset_id,
                "response": {"status": 200, "fromDiskCache": asset_from_cache},
            },
        )
        if asset_from_cache:
            self.session.emit(
                "Network.requestServedFromCache",
                {"requestId": asset_id},
            )
        self.session.emit(
            "Network.loadingFinished",
            {
                "requestId": asset_id,
                "timestamp": base_timestamp + (0.04 if is_warm else 0.12),
                "encodedDataLength": 0 if asset_from_cache else 700,
            },
        )
        self.session.emit(
            "Network.loadingFinished",
            {
                "requestId": document_id,
                "timestamp": base_timestamp + (0.08 if is_warm else 0.2),
                "encodedDataLength": 500,
            },
        )
        if self.final_url_override:
            self.context.dispatch_request(
                url=self.final_url_override,
                resource_type="document",
            )
            self.url = self.final_url_override
        response_headers = dict(self.document_release_headers)
        request_nonce = str(
            request_headers.get("x-propertyquarry-release-probe-nonce") or ""
        )
        if request_nonce:
            cache_control = (
                self.cache_control_override
                if self.cache_control_override is not None
                else "no-store"
            )
            if cache_control:
                response_headers["cache-control"] = cache_control
            if not self.suppress_probe_nonce_ack:
                response_headers[
                    performance.RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER
                ] = (
                    self.probe_nonce_ack_override
                    if self.probe_nonce_ack_override is not None
                    else hashlib.sha256(
                        f"propertyquarry-release-probe\0{request_nonce}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                )
        return _FakeNavigationResponse(
            status=self.navigation_status,
            headers=response_headers,
        )

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_timeouts_ms.append(timeout_ms)

    def evaluate(self, script: str) -> object:
        if "getEntriesByType('navigation')" in script:
            return {
                "responseStartMs": 20,
                "responseEndMs": 40,
                "domContentLoadedMs": 60,
                "loadEventMs": 80,
                "transferSize": 500,
                "encodedBodySize": 400,
                "decodedBodySize": 800,
            }
        return True


class _FakeContext:
    def __init__(self) -> None:
        self.page = _FakePage(self)
        self.session = _FakeSession()
        self.closed = False
        self.route_handlers: list[object] = []
        self.continued_requests: list[dict[str, object]] = []
        self.fetch_request_counter = 0

    def new_page(self) -> _FakePage:
        return self.page

    def new_cdp_session(self, page: _FakePage) -> _FakeSession:
        page.session = self.session
        return self.session

    def route(self, _pattern: str, handler: object) -> None:
        self.route_handlers.append(handler)

    def dispatch_request(self, *, url: str, resource_type: str) -> dict[str, str]:
        if (
            resource_type == "document"
            and callable(self.session.callbacks.get("Fetch.requestPaused"))
        ):
            self.fetch_request_counter += 1
            request_headers = {"accept": "text/html"}
            command_count_before = len(self.session.commands)
            self.session.emit(
                "Fetch.requestPaused",
                {
                    "requestId": f"fetch-document-{self.fetch_request_counter}",
                    "request": {
                        "url": url,
                        "method": "GET",
                        "headers": request_headers,
                    },
                    "resourceType": "Document",
                },
            )
            continuations = [
                payload
                for name, payload in self.session.commands[command_count_before:]
                if name == "Fetch.continueRequest" and isinstance(payload, dict)
            ]
            assert len(continuations) == 1
            continuation_headers = continuations[0].get("headers")
            if isinstance(continuation_headers, list):
                request_headers = {
                    str(row["name"]): str(row["value"])
                    for row in continuation_headers
                    if isinstance(row, dict)
                    and set(row) == {"name", "value"}
                }
            self.continued_requests.append(
                {
                    "url": url,
                    "resource_type": resource_type,
                    "headers": dict(request_headers),
                }
            )
            return request_headers
        continued_headers: dict[str, str] = {}
        for handler in self.route_handlers:
            request = _FakeRequest(url=url, resource_type=resource_type)
            route = _FakeRoute(self, request)
            assert callable(handler)
            handler(route)
            assert route.continued is True
            continued_headers = dict(
                self.continued_requests[-1].get("headers") or {}
            )
        return continued_headers

    def cookies(self, _urls: list[str]) -> list[dict[str, object]]:
        return [{"name": "ea_workspace_session", "value": "redacted"}]

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    version = "123.0.6312.0"

    def __init__(self) -> None:
        self.context = _FakeContext()
        self.context_kwargs: dict[str, object] = {}
        self.launch_calls: list[dict[str, object]] = []
        self.on_launch: object | None = None
        self.closed = False

    def new_context(self, **kwargs: object) -> _FakeContext:
        self.context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakeBrowserType:
    def __init__(self, browser: _FakeBrowser, *, executable_path: str) -> None:
        self.browser = browser
        self.executable_path = executable_path

    def launch(self, **kwargs: object) -> _FakeBrowser:
        self.browser.launch_calls.append(dict(kwargs))
        if callable(self.browser.on_launch):
            self.browser.on_launch()
        return self.browser


class _FakePlaywrightManager:
    active_browser: _FakeBrowser | None = None

    def __init__(self) -> None:
        browser = self.active_browser or _FakeBrowser()
        self.playwright = SimpleNamespace(
            chromium=_FakeBrowserType(browser, executable_path=sys.executable),
            firefox=_FakeBrowserType(browser, executable_path=sys.executable),
            webkit=_FakeBrowserType(browser, executable_path=sys.executable),
        )

    def __enter__(self) -> object:
        return self.playwright

    def __exit__(self, *_args: object) -> None:
        return None


def _fake_browser_collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_launch: object | None = None,
) -> _FakeBrowser:
    browser = _FakeBrowser()
    browser.on_launch = on_launch
    monkeypatch.setattr(_FakePlaywrightManager, "active_browser", browser)
    monkeypatch.setattr(
        performance,
        "playwright_engine_executable",
        lambda _playwright, *, engine: sys.executable,
    )
    monkeypatch.setattr(
        performance.importlib.metadata,
        "version",
        lambda _name: "1.52.0",
    )
    return browser


def test_default_constrained_profile_is_closed_bounded_and_honest() -> None:
    profile = performance.constrained_client_profile_from_config(None)
    receipt = performance.constrained_client_profile_receipt(profile)

    assert tuple(asdict(profile)) == performance.CONSTRAINED_CLIENT_PROFILE_FIELDS
    assert receipt["name"] == "low_end_mobile_lab_v1"
    assert receipt["cpu"] == {
        "slowdown_rate": 4,
        "claim": "browser_lab_emulation_only",
    }
    assert receipt["network"]["latency_ms"] == 150
    assert receipt["network"]["download_kbps"] == 1600
    assert receipt["viewport"]["width"] == 390
    assert receipt["viewport"]["claim"] == "emulated_viewport_not_physical_device"
    assert receipt["cache_policy"] == {
        "cold": "browser_http_cache_cleared_before_first_navigation",
        "warm": "same_context_neutral_page_repeat_cache_eligible",
        "service_workers": "blocked",
    }


def test_cli_keeps_release_probe_and_other_secrets_out_of_subprocess_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    synthetic_chromium_executable: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    class TrackingStdinBuffer:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            payload = self.payload
            self.payload = b""
            return payload

    stdin_buffer = TrackingStdinBuffer((RELEASE_PROBE_SECRET + "\n").encode())

    def fake_build(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        for name in (
            "DATABASE_URL",
            "ONEMIN_AI_API_KEY",
            "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA",
            "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID",
            "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256",
            "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST",
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH",
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256",
            "PROPERTYQUARRY_LIVE_PROBE_SECRET",
            "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
            "PROPERTYQUARRY_PERFORMANCE_TARGET_URL",
            "LD_LIBRARY_PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "PLAYWRIGHT_BROWSERS_PATH",
            "PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        ):
            assert name not in os.environ
        assert os.environ["PATH"] == "/usr/bin:/bin"
        return {"schema": performance.AUTHENTICATED_PERFORMANCE_SCHEMA, "status": "pass"}

    monkeypatch.setenv(
        "PROPERTYQUARRY_PERFORMANCE_TARGET_URL",
        "https://propertyquarry.com/app/search",
    )
    monkeypatch.delenv("PROPERTYQUARRY_LIVE_PROBE_SECRET", raising=False)
    monkeypatch.delenv(
        "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
        raising=False,
    )
    monkeypatch.setenv("PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA", "a" * 40)
    monkeypatch.setenv(
        "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST",
        "sha256:" + ("b" * 64),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID",
        "propertyquarry-production",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256",
        EXPECTED_RELEASE_IDENTITY["manifest_sha256"],
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH",
        str(synthetic_chromium_executable["path"]),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256",
        str(synthetic_chromium_executable["sha256"]),
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@database/property")
    monkeypatch.setenv("ONEMIN_AI_API_KEY", "provider-secret-do-not-inherit")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/attacker-library-path")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/attacker-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/attacker-ca-directory")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/attacker-browsers")
    monkeypatch.setenv(
        "PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        "/tmp/attacker-chromium",
    )
    monkeypatch.setattr(performance, "build_authenticated_performance_receipt", fake_build)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stdin_buffer))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "propertyquarry_authenticated_performance_smoke.py",
            "--release-probe-secret-stdin",
        ],
    )

    assert performance.main() == 0
    assert captured["constrained_client_target_url"] == (
        "https://propertyquarry.com/app/search"
    )
    assert captured["constrained_client_authentication_bootstrap_url"] == ""
    assert captured["constrained_client_release_probe_secret"] == RELEASE_PROBE_SECRET
    assert captured["release_commit_sha"] == "a" * 40
    assert captured["release_image_digest"] == "sha256:" + ("b" * 64)
    assert captured["release_deployment_id"] == "propertyquarry-production"
    assert captured["release_manifest_sha256"] == EXPECTED_RELEASE_IDENTITY[
        "manifest_sha256"
    ]
    assert captured["expected_chromium_executable_path"] == (
        synthetic_chromium_executable["path"]
    )
    assert captured["expected_chromium_executable_sha256"] == (
        synthetic_chromium_executable["sha256"]
    )
    assert stdin_buffer.read_sizes == [4_097]
    assert stdin_buffer.payload == b""
    output = capsys.readouterr()
    assert RELEASE_PROBE_SECRET not in output.out
    assert RELEASE_PROBE_SECRET not in output.err
    assert "provider-secret-do-not-inherit" not in output.out
    assert output.err == ""


@pytest.mark.parametrize(
    "environment_name",
    [
        "PROPERTYQUARRY_LIVE_PROBE_SECRET",
        "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
    ],
)
def test_cli_rejects_release_probe_secret_environment_delivery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment_name: str,
) -> None:
    build_called = False

    def fake_build(**_kwargs: object) -> dict[str, object]:
        nonlocal build_called
        build_called = True
        return {"schema": performance.AUTHENTICATED_PERFORMANCE_SCHEMA, "status": "pass"}

    monkeypatch.delenv("PROPERTYQUARRY_LIVE_PROBE_SECRET", raising=False)
    monkeypatch.delenv(
        "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
        raising=False,
    )
    monkeypatch.setenv(environment_name, RELEASE_PROBE_SECRET)
    monkeypatch.setattr(performance, "build_authenticated_performance_receipt", fake_build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["propertyquarry_authenticated_performance_smoke.py"],
    )

    with pytest.raises(SystemExit) as exc_info:
        performance.main()

    assert exc_info.value.code == 2
    assert build_called is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert RELEASE_PROBE_SECRET not in captured.err
    assert "use --release-probe-secret-stdin" in captured.err


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.pop("network_latency_ms"),
        lambda row: row.__setitem__("undeclared", 1),
        lambda row: row.__setitem__("cpu_slowdown_rate", True),
        lambda row: row.__setitem__("download_kbps", 0),
        lambda row: row.__setitem__("viewport_width", 200),
        lambda row: row.__setitem__("max_request_count", 0),
    ],
)
def test_constrained_profile_config_rejects_omission_extension_type_and_bounds(
    mutation: object,
) -> None:
    config = asdict(performance.ConstrainedClientProfile())
    assert callable(mutation)
    mutation(config)

    with pytest.raises(performance.PerformanceConfigError):
        performance.constrained_client_profile_from_config(config)


def test_constrained_profile_rejects_incoherent_threshold_order() -> None:
    profile = replace(
        performance.ConstrainedClientProfile(),
        warm_navigation_budget_ms=20_000,
    )

    with pytest.raises(
        performance.PerformanceConfigError,
        match="warm_navigation_budget_ms_must_not_exceed_cold_budget",
    ):
        performance.validate_constrained_client_profile(profile)


@pytest.mark.parametrize(
    ("warm_budget", "cold_budget"),
    [(True, 2400), (49, 2400), (1200, 1199), (1200, 60_001)],
)
def test_server_route_thresholds_are_strict(
    warm_budget: object,
    cold_budget: object,
) -> None:
    with pytest.raises(performance.PerformanceConfigError):
        performance._validate_route_budgets(
            warm_route_budget_ms=warm_budget,
            cold_route_budget_ms=cold_budget,
        )


def test_server_route_measurement_keeps_warm_compatibility_without_best_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        status_code=200,
        text="<html><body>PropertyQuarry</body></html>",
        headers={},
        content=b"PropertyQuarry",
    )
    samples = iter(((response, 900), (response, 1100)))
    monkeypatch.setattr(
        performance,
        "_request_measured_route",
        lambda _client, _path: next(samples),
    )
    monkeypatch.setattr(performance, "_asset_text", lambda *_args, **_kwargs: "")

    row = performance._measure_route(
        object(),
        "/health",
        budget_ms=1200,
        cold_budget_ms=2400,
    )

    assert row["first_duration_ms"] == 900
    assert row["duration_ms"] == 1100
    assert row["attempt_durations_ms"] == [900, 1100]
    assert row["measurements"]["cold"]["sequence"] == 1
    assert row["measurements"]["warm"]["sequence"] == 2
    assert row["cold_to_warm"]["duration_delta_ms"] == -200
    assert row["ok"] is True


@pytest.mark.parametrize("status_code", [404, 500])
def test_waterfall_capture_marks_http_error_responses_failed(
    status_code: int,
) -> None:
    session = _FakeSession()
    capture = performance._BrowserWaterfallCapture(
        session,
        slowest_resource_limit=5,
    )
    capture.begin("cold")
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "http-error",
            "timestamp": 10.0,
            "type": "Script",
            "request": {
                "url": "https://propertyquarry.test/app/assets/private.js?token=secret"
            },
        },
    )
    session.emit(
        "Network.responseReceived",
        {
            "requestId": "http-error",
            "response": {
                "status": status_code,
                "fromDiskCache": False,
            },
        },
    )
    session.emit(
        "Network.loadingFinished",
        {
            "requestId": "http-error",
            "timestamp": 10.25,
            "encodedDataLength": 128,
        },
    )

    metrics = capture.finish()

    assert metrics["request_count"] == 1
    assert metrics["failed_request_count"] == 1
    assert metrics["incomplete_request_count"] == 0
    assert metrics["failed_requests"] == [
        {
            "url": "https://propertyquarry.test/app/assets/:asset",
            "resource_type": "Script",
            "status_code": status_code,
            "duration_ms": 250,
            "transferred_bytes": 128,
            "cache_source": "network",
            "failed": True,
            "incomplete": False,
        }
    ]
    assert metrics["slowest_resources"] == metrics["failed_requests"]
    assert "private.js" not in json.dumps(metrics, sort_keys=True)
    assert "token=secret" not in json.dumps(metrics, sort_keys=True)


def test_waterfall_capture_marks_missing_response_status_failed() -> None:
    session = _FakeSession()
    capture = performance._BrowserWaterfallCapture(
        session,
        slowest_resource_limit=5,
    )
    capture.begin("warm")
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "missing-status",
            "timestamp": 20.0,
            "type": "Fetch",
            "request": {"url": "https://propertyquarry.test/private/customer-42"},
        },
    )
    session.emit(
        "Network.loadingFinished",
        {
            "requestId": "missing-status",
            "timestamp": 20.1,
            "encodedDataLength": 0,
        },
    )

    metrics = capture.finish()

    assert metrics["request_count"] == 1
    assert metrics["failed_request_count"] == 1
    assert metrics["incomplete_request_count"] == 0
    assert metrics["failed_requests"] == [
        {
            "url": performance._sanitized_resource_url(
                "https://propertyquarry.test/private/customer-42"
            ),
            "resource_type": "Fetch",
            "status_code": 0,
            "duration_ms": 100,
            "transferred_bytes": 0,
            "cache_source": "network",
            "failed": True,
            "incomplete": False,
        }
    ]


def test_waterfall_capture_marks_service_worker_http_failure_and_cache_source() -> None:
    session = _FakeSession()
    capture = performance._BrowserWaterfallCapture(
        session,
        slowest_resource_limit=5,
    )
    capture.begin("warm")
    session.emit(
        "Network.requestWillBeSent",
        {
            "requestId": "service-worker-failure",
            "timestamp": 30.0,
            "type": "Fetch",
            "request": {"url": "https://propertyquarry.test/app/search"},
        },
    )
    session.emit(
        "Network.responseReceived",
        {
            "requestId": "service-worker-failure",
            "response": {
                "status": 503,
                "fromServiceWorker": True,
                "fromDiskCache": False,
            },
        },
    )
    session.emit(
        "Network.loadingFinished",
        {
            "requestId": "service-worker-failure",
            "timestamp": 30.05,
            "encodedDataLength": 96,
        },
    )

    metrics = capture.finish()

    assert metrics["failed_request_count"] == 1
    assert metrics["cache_hit_count"] == 1
    assert metrics["subresource_cache_hit_count"] == 0
    assert metrics["failed_requests"] == [
        {
            "url": "https://propertyquarry.test/app/search",
            "resource_type": "Fetch",
            "status_code": 503,
            "duration_ms": 50,
            "transferred_bytes": 96,
            "cache_source": "service_worker",
            "failed": True,
            "incomplete": False,
        }
    ]


def test_chromium_constrained_collector_records_identity_cold_warm_and_waterfall(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    browser = _fake_browser_collector(monkeypatch)
    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "pass"
    assert receipt["identity"]["engine"] == "chromium"
    assert receipt["identity"]["browser_version"] == "123.0.6312.0"
    assert receipt["identity"]["executable_path"] == synthetic_chromium_executable[
        "path"
    ]
    assert receipt["identity"]["executable_sha256"] == synthetic_chromium_executable[
        "sha256"
    ]
    assert browser.launch_calls == [
        {
            "headless": True,
            "executable_path": synthetic_chromium_executable["path"],
        }
    ]
    assert receipt["launch_binding"] == {
        "mechanism": "playwright_explicit_executable_path",
        "executable_path": synthetic_chromium_executable["path"],
        "executable_sha256": synthetic_chromium_executable["sha256"],
        "prelaunch_bytes": synthetic_chromium_executable["bytes"],
        "postlaunch_identity_match": True,
    }
    assert receipt["profile_support"]["cpu_throttling"]["applied"] is True
    assert receipt["profile_support"]["network_throttling"]["applied"] is True
    assert receipt["measurements"]["cold"]["cache_state"] == "cleared_before_navigation"
    assert receipt["measurements"]["cold"]["final_url"] == (
        "https://propertyquarry.test/app/search"
    )
    assert receipt["measurements"]["cold"]["request_count"] == 2
    assert receipt["measurements"]["cold"]["transferred_bytes"] == 1200
    assert receipt["measurements"]["cold"]["failed_request_count"] == 0
    assert receipt["measurements"]["cold"]["failed_requests"] == []
    assert receipt["measurements"]["cold"]["incomplete_request_count"] == 0
    assert browser.context.page.navigation_urls == [
        "https://propertyquarry.test/app/search",
        "about:blank",
        "https://propertyquarry.test/app/search",
    ]
    assert browser.context.page.wait_timeouts_ms == [1_000, 1_000]
    assert receipt["measurements"]["warm"]["cache_state"] == (
        "same_context_neutral_page_repeat_cache_observed"
    )
    assert receipt["measurements"]["warm"]["cache_hit_count"] == 1
    assert receipt["measurements"]["warm"]["subresource_cache_hit_count"] == 1
    assert receipt["measurements"]["warm"]["final_url"] == (
        "https://propertyquarry.test/app/search"
    )
    assert receipt["measurements"]["warm"]["transferred_bytes"] == 500
    assert receipt["measurements"]["cold"]["slowest_resources"][0][
        "duration_ms"
    ] == 200
    assert receipt["authentication"] == {
        "method": "signed_release_probe_per_navigation",
        "navigation_signing_mechanism": (
            "chromium_cdp_Fetch.requestPaused_document_only"
        ),
        "playwright_routing_used": False,
        "subresource_http_cache_preserved": True,
        "signed_navigation_count": 2,
        "distinct_nonce_count": 2,
        "target_surface_observed": True,
        "release_probe_secret_persisted": False,
    }
    signed_documents = [
        request
        for request in browser.context.continued_requests
        if request["resource_type"] == "document"
    ]
    assert len(signed_documents) == 2
    signed_headers = [request["headers"] for request in signed_documents]
    assert all(RELEASE_PROBE_HEADER_NAMES.issubset(headers) for headers in signed_headers)
    assert len(
        {
            headers["x-propertyquarry-release-probe-nonce"]
            for headers in signed_headers
        }
    ) == 2
    bound_nonce_hashes: set[str] = set()
    for phase in ("cold", "warm"):
        binding = receipt["measurements"][phase][
            "document_authentication_binding"
        ]
        assert binding["cache_control"] == "no-store"
        assert binding["expected_nonce_sha256"] == binding[
            "acknowledged_nonce_sha256"
        ]
        assert len(binding["acknowledged_nonce_sha256"]) == 64
        bound_nonce_hashes.add(binding["acknowledged_nonce_sha256"])
    assert len(bound_nonce_hashes) == 2
    asset_headers = [
        request["headers"]
        for request in browser.context.continued_requests
        if request["resource_type"] == "stylesheet"
    ]
    assert asset_headers == []
    assert browser.context.route_handlers == []
    assert any(
        check == {
            "name": "warm_signed_release_probe_nonces_unique",
            "ok": True,
        }
        for check in receipt["measurements"]["warm"]["checks"]
    )
    for phase in ("cold", "warm"):
        assert receipt["measurements"][phase][
            "document_release_identity"
        ] == OBSERVED_RELEASE_IDENTITY
        for resource in receipt["measurements"][phase]["slowest_resources"]:
            assert 100 <= resource["status_code"] < 400
        phase_checks = {
            check["name"]: check
            for check in receipt["measurements"][phase]["checks"]
        }
        assert phase_checks[f"{phase}_navigation_status_ok"] == {
            "name": f"{phase}_navigation_status_ok",
            "ok": True,
            "status_code": 200,
        }
        assert phase_checks[f"{phase}_final_target_url_observed"] == {
            "name": f"{phase}_final_target_url_observed",
            "ok": True,
        }
        assert phase_checks[f"{phase}_document_release_identity_exact"] == {
            "name": f"{phase}_document_release_identity_exact",
            "ok": True,
        }
    commands = dict(browser.context.session.commands)
    assert commands["Fetch.enable"] == {
        "patterns": [
            {
                "urlPattern": "*",
                "resourceType": "Document",
                "requestStage": "Request",
            }
        ],
        "handleAuthRequests": False,
    }
    assert sum(
        name == "Fetch.continueRequest"
        for name, _payload in browser.context.session.commands
    ) == 2
    assert commands["Emulation.setCPUThrottlingRate"] == {"rate": 4}
    assert commands["Network.emulateNetworkConditions"] == {
        "offline": False,
        "latency": 150,
        "downloadThroughput": 200_000,
        "uploadThroughput": 93_750,
        "connectionType": "cellular3g",
    }
    assert commands["Network.setCacheDisabled"] == {"cacheDisabled": False}
    assert ("Network.clearBrowserCache", None) in browser.context.session.commands
    serialized = json.dumps(receipt, sort_keys=True)
    assert RELEASE_PROBE_SECRET not in serialized
    assert "signature=do-not-serialize" not in serialized
    assert receipt["field_core_web_vitals_claimed"] is False
    assert receipt["physical_device_claimed"] is False


@pytest.mark.parametrize(
    ("mutation", "failed_check_suffix"),
    (
        ("missing_ack", "server_verified_probe_nonce_acknowledged"),
        ("stale_ack", "server_verified_probe_nonce_acknowledged"),
        ("cacheable", "document_cache_control_no_store"),
        ("cached_document", "server_verified_probe_nonce_acknowledged"),
        ("no_cache_reuse", "warm_http_cache_reuse_observed"),
    ),
)
def test_signed_collector_rejects_unacknowledged_or_cacheable_document(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
    mutation: str,
    failed_check_suffix: str,
) -> None:
    browser = _fake_browser_collector(monkeypatch)
    page = browser.context.page
    if mutation == "missing_ack":
        page.suppress_probe_nonce_ack = True
    elif mutation == "stale_ack":
        page.probe_nonce_ack_override = "d123456789abcdef" * 4
    elif mutation == "cacheable":
        page.cache_control_override = "public, max-age=300"
    elif mutation == "cached_document":
        page.skip_target_request_interception = True
        page.document_release_headers["cache-control"] = "no-store"
        page.document_release_headers[
            performance.RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER
        ] = "d123456789abcdef" * 4
    else:
        page.warm_asset_cache_hit = False

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "fail"
    assert any(
        check["name"].endswith(failed_check_suffix) and check["ok"] is False
        for phase in ("cold", "warm")
        for check in receipt["measurements"][phase]["checks"]
    )
    if mutation == "no_cache_reuse":
        assert receipt["authentication"][
            "subresource_http_cache_preserved"
        ] is False


def test_passing_receipt_validator_handles_unhashable_nonce_ack_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)
    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )
    receipt["measurements"]["cold"]["document_authentication_binding"][
        "acknowledged_nonce_sha256"
    ] = []

    errors = performance._passing_engine_receipt_errors(
        receipt,
        expected_engine="chromium",
        expected_target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        profile=performance.ConstrainedClientProfile(),
    )

    assert "cold_document_authentication_binding_invalid" in errors


def test_chromium_collector_rejects_stale_document_release_header(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    browser = _fake_browser_collector(monkeypatch)
    browser.context.page.document_release_headers[
        "x-propertyquarry-release-commit"
    ] = "d" * 40

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "fail"
    for phase in ("cold", "warm"):
        measurement = receipt["measurements"][phase]
        assert measurement["document_release_identity"] == {
            **OBSERVED_RELEASE_IDENTITY,
            "commit_sha": "d" * 40,
        }
        checks = {check["name"]: check for check in measurement["checks"]}
        assert checks[f"{phase}_document_release_identity_exact"] == {
            "name": f"{phase}_document_release_identity_exact",
            "ok": False,
        }


def test_chromium_collector_rejects_wrong_expected_executable_digest(
    synthetic_chromium_executable: dict[str, object],
) -> None:
    expected_sha256 = str(synthetic_chromium_executable["sha256"])
    wrong_sha256 = expected_sha256[:-1] + (
        "0" if expected_sha256[-1] != "0" else "1"
    )

    with pytest.raises(
        performance.PerformanceConfigError,
        match="expected_chromium_executable_digest_mismatch",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://propertyquarry.test/app/search",
            authentication_bootstrap_url="",
            release_probe_secret=RELEASE_PROBE_SECRET,
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(
                synthetic_chromium_executable["path"]
            ),
            expected_chromium_executable_sha256=wrong_sha256,
        )


def test_chromium_collector_rejects_noncanonical_expected_executable_path(
    synthetic_chromium_executable: dict[str, object],
) -> None:
    with pytest.raises(
        performance.PerformanceConfigError,
        match="expected_chromium_executable_path_invalid",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://propertyquarry.test/app/search",
            authentication_bootstrap_url="",
            release_probe_secret=RELEASE_PROBE_SECRET,
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=(
                str(synthetic_chromium_executable["path"]) + " "
            ),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )


def test_chromium_collector_rejects_symlink_expected_executable_path(
    tmp_path: Path,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    symlink_path = tmp_path / "chromium-symlink" / "chrome"
    symlink_path.parent.mkdir(parents=True)
    symlink_path.symlink_to(str(synthetic_chromium_executable["path"]))

    with pytest.raises(
        performance.PerformanceConfigError,
        match="expected_chromium_executable_path_invalid",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://propertyquarry.test/app/search",
            authentication_bootstrap_url="",
            release_probe_secret=RELEASE_PROBE_SECRET,
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(symlink_path),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )


def test_chromium_collector_rejects_writable_executable_ancestor(
    synthetic_chromium_executable: dict[str, object],
) -> None:
    executable_path = Path(str(synthetic_chromium_executable["path"]))
    executable_path.parent.chmod(0o777)
    try:
        with pytest.raises(
            performance.PerformanceConfigError,
            match="expected_chromium_executable_directory_chain_unsafe",
        ):
            performance.collect_constrained_client_browser_evidence(
                target_url="https://propertyquarry.test/app/search",
                authentication_bootstrap_url="",
                release_probe_secret=RELEASE_PROBE_SECRET,
                profile=performance.ConstrainedClientProfile(),
                expected_release_identity=EXPECTED_RELEASE_IDENTITY,
                expected_chromium_executable_path=str(executable_path),
                expected_chromium_executable_sha256=str(
                    synthetic_chromium_executable["sha256"]
                ),
            )
    finally:
        executable_path.parent.chmod(0o700)


def test_chromium_collector_rejects_executable_changed_during_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mutable_chromium = _write_synthetic_chromium(
        tmp_path / "chromium-mutating" / "chrome",
        variant=b"before-launch",
    )
    executable_path = Path(str(mutable_chromium["path"]))

    def mutate_executable() -> None:
        _write_synthetic_chromium(
            executable_path,
            variant=b"changed-during-launch",
        )

    browser = _fake_browser_collector(
        monkeypatch,
        on_launch=mutate_executable,
    )
    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(mutable_chromium["path"]),
        expected_chromium_executable_sha256=str(mutable_chromium["sha256"]),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert browser.launch_calls == [
        {
            "headless": True,
            "executable_path": mutable_chromium["path"],
        }
    ]
    assert receipt["status"] == "fail"
    assert receipt["error"] == (
        "PerformanceConfigError:expected_chromium_executable_changed"
    )
    assert receipt["field_core_web_vitals_claimed"] is False
    assert receipt["physical_device_claimed"] is False


def test_chromium_constrained_collector_fails_closed_when_resource_budget_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)
    profile = replace(
        performance.ConstrainedClientProfile(),
        max_request_count=1,
    )

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="https://propertyquarry.test/app/access/secret",
        profile=profile,
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "fail"
    cold_checks = {
        row["name"]: row for row in receipt["measurements"]["cold"]["checks"]
    }
    assert cold_checks["cold_request_count_under_budget"] == {
        "name": "cold_request_count_under_budget",
        "ok": False,
        "observed": 2,
        "maximum": 1,
    }


def test_signed_collector_rejects_redirect_to_another_app_surface(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    browser = _fake_browser_collector(monkeypatch)
    browser.context.page.final_url_override = (
        "https://propertyquarry.test/app/account?from=search"
    )

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "fail"
    for phase in ("cold", "warm"):
        measurement = receipt["measurements"][phase]
        assert measurement["status_code"] == 200
        assert measurement["final_url"] == "invalid-or-non-target-url"
        checks = {check["name"]: check for check in measurement["checks"]}
        assert checks[f"{phase}_navigation_status_ok"]["ok"] is True
        assert checks[f"{phase}_final_target_url_observed"] == {
            "name": f"{phase}_final_target_url_observed",
            "ok": False,
        }
    redirected_documents = [
        request
        for request in browser.context.continued_requests
        if request["url"] == browser.context.page.final_url_override
    ]
    assert len(redirected_documents) == 2
    assert all(
        RELEASE_PROBE_HEADER_NAMES.isdisjoint(request["headers"])
        for request in redirected_documents
    )
    assert "app/account" not in json.dumps(receipt, sort_keys=True)


def test_signed_collector_rejects_three_hundred_navigation_status(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    browser = _fake_browser_collector(monkeypatch)
    browser.context.page.navigation_status = 302

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engine="chromium",
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "fail"
    for phase in ("cold", "warm"):
        measurement = receipt["measurements"][phase]
        assert measurement["status_code"] == 302
        assert measurement["final_url"] == (
            "https://propertyquarry.test/app/search"
        )
        checks = {check["name"]: check for check in measurement["checks"]}
        assert checks[f"{phase}_navigation_status_ok"] == {
            "name": f"{phase}_navigation_status_ok",
            "ok": False,
            "status_code": 302,
        }
        assert checks[f"{phase}_final_target_url_observed"]["ok"] is True


@pytest.mark.parametrize("browser_engine", ["firefox", "webkit"])
def test_non_chromium_engines_fail_closed_with_exact_identity_and_limitations(
    monkeypatch: pytest.MonkeyPatch,
    browser_engine: str,
) -> None:
    _fake_browser_collector(monkeypatch)

    receipt = performance.collect_constrained_client_browser_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="https://propertyquarry.test/app/access/secret",
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        browser_engine=browser_engine,
        sync_playwright_factory=_FakePlaywrightManager,
    )

    assert receipt["status"] == "unsupported"
    assert receipt["identity"]["engine"] == browser_engine
    assert receipt["identity"]["browser_version"] == "123.0.6312.0"
    assert receipt["profile_support"]["cpu_throttling"]["applied"] is False
    assert receipt["profile_support"]["network_throttling"]["applied"] is False
    assert receipt["measurements"] == {}
    assert receipt["limitations"]
    assert receipt["field_core_web_vitals_claimed"] is False
    assert receipt["physical_device_claimed"] is False


def test_multi_engine_constrained_summary_blocks_on_unsupported_engines(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)

    def collector(**kwargs: object) -> dict[str, object]:
        return performance.collect_constrained_client_browser_evidence(
            target_url=str(kwargs["target_url"]),
            authentication_bootstrap_url=str(
                kwargs["authentication_bootstrap_url"]
            ),
            release_probe_secret=str(kwargs.get("release_probe_secret") or ""),
            profile=kwargs["profile"],
            expected_release_identity=kwargs.get("expected_release_identity"),
            expected_chromium_executable_path=str(
                kwargs.get("expected_chromium_executable_path") or ""
            ),
            expected_chromium_executable_sha256=str(
                kwargs.get("expected_chromium_executable_sha256") or ""
            ),
            browser_engine=str(kwargs["browser_engine"]),
            sync_playwright_factory=_FakePlaywrightManager,
        )

    receipt = performance.collect_constrained_client_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="https://propertyquarry.test/app/access/secret",
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        browser_engines=("chromium", "firefox", "webkit"),
        collector=collector,
        release_identity_probe=_passing_release_identity_probe,
    )

    assert receipt["status"] == "blocked"
    assert receipt["requested_browser_engines"] == [
        "chromium",
        "firefox",
        "webkit",
    ]
    assert receipt["limitations_by_engine"] == {
        "firefox": receipt["engine_rows"][1]["limitations"],
        "webkit": receipt["engine_rows"][2]["limitations"],
    }
    assert receipt["field_core_web_vitals_claimed"] is False
    assert receipt["physical_device_claimed"] is False


def test_constrained_summary_blocks_document_and_version_manifest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)

    def collector(**kwargs: object) -> dict[str, object]:
        return performance.collect_constrained_client_browser_evidence(
            target_url=str(kwargs["target_url"]),
            authentication_bootstrap_url=str(
                kwargs["authentication_bootstrap_url"]
            ),
            release_probe_secret=str(kwargs.get("release_probe_secret") or ""),
            profile=kwargs["profile"],
            expected_release_identity=kwargs.get("expected_release_identity"),
            expected_chromium_executable_path=str(
                kwargs.get("expected_chromium_executable_path") or ""
            ),
            expected_chromium_executable_sha256=str(
                kwargs.get("expected_chromium_executable_sha256") or ""
            ),
            browser_engine=str(kwargs["browser_engine"]),
            sync_playwright_factory=_FakePlaywrightManager,
        )

    def different_manifest_probe(**kwargs: object) -> dict[str, object]:
        row = _passing_release_identity_probe(**kwargs)
        row["observed"] = {
            **OBSERVED_RELEASE_IDENTITY,
            "manifest_sha256": "e" * 64,
        }
        return row

    receipt = performance.collect_constrained_client_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="",
        release_probe_secret=RELEASE_PROBE_SECRET,
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        collector=collector,
        release_identity_probe=different_manifest_probe,
    )

    assert receipt["status"] == "blocked"
    assert receipt["engine_rows"][0]["status"] == "pass"
    assert receipt["release_identity"]["status"] == "pass"
    assert receipt["release_identity"]["matches_expected"] is True
    assert receipt["release_identity"]["observed"]["manifest_sha256"] == (
        "e" * 64
    )
    for phase in ("cold", "warm"):
        assert receipt["engine_rows"][0]["measurements"][phase][
            "document_release_identity"
        ]["manifest_sha256"] == OBSERVED_RELEASE_IDENTITY["manifest_sha256"]


def test_constrained_summary_rejects_malformed_passing_engine_receipt(
    synthetic_chromium_executable: dict[str, object],
) -> None:
    receipt = performance.collect_constrained_client_evidence(
        target_url="https://propertyquarry.test/app/search",
        authentication_bootstrap_url="https://propertyquarry.test/app/access/secret",
        profile=performance.ConstrainedClientProfile(),
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        collector=lambda **_kwargs: {
            "status": "pass",
            "browser_engine": "chromium",
        },
        release_identity_probe=_passing_release_identity_probe,
    )

    assert receipt["status"] == "blocked"
    assert receipt["engine_rows"] == [
        {
            "status": "fail",
            "browser_engine": "chromium",
            "error": "passing_browser_receipt_validation_failed",
            "validation_errors": ["passing_engine_receipt_fields_invalid"],
            "limitations": [
                "The browser collector returned an incomplete or inconsistent passing receipt."
            ],
        }
    ]


def test_browser_target_and_bootstrap_must_be_secure_and_same_origin(
    synthetic_chromium_executable: dict[str, object],
) -> None:
    with pytest.raises(
        performance.PerformanceConfigError,
        match="target_url_must_use_https_or_loopback_http",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="http://propertyquarry.test/app/search",
            authentication_bootstrap_url="http://propertyquarry.test/app/access/secret",
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(
                synthetic_chromium_executable["path"]
            ),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )

    with pytest.raises(
        performance.PerformanceConfigError,
        match="authentication_bootstrap_url_must_match_target_origin",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://propertyquarry.test/app/search",
            authentication_bootstrap_url="https://identity.propertyquarry.test/access/secret",
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(
                synthetic_chromium_executable["path"]
            ),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )

    with pytest.raises(
        performance.PerformanceConfigError,
        match="target_url_invalid",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://user:password@propertyquarry.test/app/search",
            authentication_bootstrap_url="",
            release_probe_secret=RELEASE_PROBE_SECRET,
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(
                synthetic_chromium_executable["path"]
            ),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )

    with pytest.raises(
        performance.PerformanceConfigError,
        match="target_url_invalid",
    ):
        performance.collect_constrained_client_browser_evidence(
            target_url="https://propertyquarry.test/app/search?run_id=private",
            authentication_bootstrap_url="",
            release_probe_secret=RELEASE_PROBE_SECRET,
            profile=performance.ConstrainedClientProfile(),
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
            expected_chromium_executable_path=str(
                synthetic_chromium_executable["path"]
            ),
            expected_chromium_executable_sha256=str(
                synthetic_chromium_executable["sha256"]
            ),
        )


def test_resource_url_receipts_remove_userinfo_queries_and_sensitive_paths() -> None:
    assert performance._sanitized_resource_url(
        "https://user:password@propertyquarry.test/private/customer-42?token=secret"
    ) == "invalid-url"

    sanitized = performance._sanitized_resource_url(
        "https://propertyquarry.test/private/customer-42?token=secret#fragment"
    )
    assert sanitized.startswith(
        "https://propertyquarry.test/_path-sha256/"
    )
    assert "private" not in sanitized
    assert "customer-42" not in sanitized
    assert "token" not in sanitized
    assert "secret" not in sanitized
    assert "?" not in sanitized
    assert "#" not in sanitized
    assert performance._sanitized_resource_url(
        "https://propertyquarry.test/app/assets/private.css?signature=secret"
    ) == "https://propertyquarry.test/app/assets/:asset"


def test_live_version_probe_records_exact_release_identity_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = dict(LIVE_VERSION_PAYLOAD)
    captured = _install_live_version_response(monkeypatch, payload)

    receipt = performance.probe_live_release_identity(
        target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
    )

    assert captured == {
        "url": "https://propertyquarry.test/version",
        "timeout": 10.0,
    }
    assert receipt == _passing_release_identity_probe(
        target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
    )


@pytest.mark.parametrize(
    ("payload_field", "observed_field", "invalid_value"),
    [
        ("release_commit_sha", "commit_sha", True),
        ("release_image_digest", "image_digest", 7),
        ("release_deployment_id", "deployment_id", False),
        ("release_manifest_status", "manifest_status", ["complete"]),
        ("release_manifest_sha256", "manifest_sha256", True),
        ("replica_id", "replica_id", 3.14),
    ],
)
def test_live_version_probe_rejects_wrong_and_boolean_identity_types(
    monkeypatch: pytest.MonkeyPatch,
    payload_field: str,
    observed_field: str,
    invalid_value: object,
) -> None:
    payload = {
        **LIVE_VERSION_PAYLOAD,
        payload_field: invalid_value,
    }
    _install_live_version_response(monkeypatch, payload)

    receipt = performance.probe_live_release_identity(
        target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
    )

    assert receipt["status"] == "fail"
    assert receipt["matches_expected"] is False
    assert receipt["error"] == "version_response_identity_types_invalid"
    assert receipt["observed"][observed_field] == ""


def test_live_version_probe_rejects_invalid_replica_id_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        **LIVE_VERSION_PAYLOAD,
        "replica_id": "replica id with spaces/and/slashes",
    }
    _install_live_version_response(monkeypatch, payload)

    receipt = performance.probe_live_release_identity(
        target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
    )

    assert receipt["status"] == "fail"
    assert receipt["matches_expected"] is False
    assert receipt["error"] == "release_identity_mismatch"


def test_live_version_probe_rejects_manifest_that_only_self_reports_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        **LIVE_VERSION_PAYLOAD,
        "release_manifest_sha256": "d" * 64,
    }
    _install_live_version_response(monkeypatch, payload)

    receipt = performance.probe_live_release_identity(
        target_url="https://propertyquarry.test/app/search",
        expected_release_identity=EXPECTED_RELEASE_IDENTITY,
    )

    assert receipt["status"] == "fail"
    assert receipt["matches_expected"] is False
    assert receipt["observed"]["manifest_sha256"] == "d" * 64
    assert receipt["expected"]["manifest_sha256"] == EXPECTED_MANIFEST_SHA256
    assert receipt["error"] == "release_identity_mismatch"


def test_checked_in_smoke_records_cold_and_warm_without_claiming_browser_proof() -> None:
    receipt = performance.build_authenticated_performance_receipt()

    assert receipt["schema"] == performance.AUTHENTICATED_PERFORMANCE_SCHEMA
    assert receipt["status"] == "pass"
    assert receipt["flagship_status"] == "blocked"
    assert receipt["flagship_blockers"] == [
        "constrained_authenticated_browser_evidence_missing_or_blocked",
        "signed_release_probe_authentication_missing_or_blocked",
        "exact_live_release_identity_missing_or_mismatched",
    ]
    assert receipt["server_request_evidence"]["status"] == "pass"
    assert receipt["route_count"] == len(receipt["routes"])
    assert all(row["attempt_count"] == 2 for row in receipt["routes"])
    assert all(tuple(row["measurements"]) == ("cold", "warm") for row in receipt["routes"])
    assert all(row["duration_ms"] == row["measurements"]["warm"]["duration_ms"] for row in receipt["routes"])
    assert all(row["first_duration_ms"] == row["measurements"]["cold"]["duration_ms"] for row in receipt["routes"])
    assert receipt["constrained_client_evidence"]["status"] == "not_run"
    assert receipt["claims"] == {
        "cold_and_warm_server_request_lab_evidence": True,
        "constrained_browser_lab_evidence": False,
        "signed_release_probe_authentication": False,
        "exact_live_release_identity_observed": False,
        "field_core_web_vitals": False,
        "physical_device_performance": False,
    }


def test_signed_probe_and_exact_release_identity_are_required_for_flagship_status(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)

    def collector(**kwargs: object) -> dict[str, object]:
        return performance.collect_constrained_client_browser_evidence(
            target_url=str(kwargs["target_url"]),
            authentication_bootstrap_url=str(
                kwargs["authentication_bootstrap_url"]
            ),
            release_probe_secret=str(kwargs.get("release_probe_secret") or ""),
            profile=kwargs["profile"],
            expected_release_identity=kwargs.get("expected_release_identity"),
            expected_chromium_executable_path=str(
                kwargs.get("expected_chromium_executable_path") or ""
            ),
            expected_chromium_executable_sha256=str(
                kwargs.get("expected_chromium_executable_sha256") or ""
            ),
            browser_engine=str(kwargs["browser_engine"]),
            sync_playwright_factory=_FakePlaywrightManager,
        )

    receipt = performance.build_authenticated_performance_receipt(
        constrained_client_target_url="https://propertyquarry.test/app/search",
        constrained_client_release_probe_secret=RELEASE_PROBE_SECRET,
        release_commit_sha=EXPECTED_RELEASE_IDENTITY["commit_sha"],
        release_image_digest=EXPECTED_RELEASE_IDENTITY["image_digest"],
        release_deployment_id=EXPECTED_RELEASE_IDENTITY["deployment_id"],
        release_manifest_sha256=EXPECTED_RELEASE_IDENTITY["manifest_sha256"],
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        constrained_browser_collector=collector,
        release_identity_probe=_passing_release_identity_probe,
    )

    assert receipt["status"] == "pass"
    assert receipt["flagship_status"] == "pass"
    assert receipt["flagship_blockers"] == []
    assert receipt["claims"]["constrained_browser_lab_evidence"] is True
    assert receipt["claims"]["signed_release_probe_authentication"] is True
    assert receipt["claims"]["exact_live_release_identity_observed"] is True
    assert receipt["release_identity"] == EXPECTED_RELEASE_IDENTITY
    assert receipt["constrained_client_evidence"]["release_identity"] == (
        _passing_release_identity_probe(
            target_url="https://propertyquarry.test/app/search",
            expected_release_identity=EXPECTED_RELEASE_IDENTITY,
        )
    )


def test_workspace_bootstrap_compatibility_cannot_qualify_flagship(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)

    def collector(**kwargs: object) -> dict[str, object]:
        return performance.collect_constrained_client_browser_evidence(
            target_url=str(kwargs["target_url"]),
            authentication_bootstrap_url=str(
                kwargs["authentication_bootstrap_url"]
            ),
            release_probe_secret=str(kwargs.get("release_probe_secret") or ""),
            profile=kwargs["profile"],
            expected_release_identity=kwargs.get("expected_release_identity"),
            expected_chromium_executable_path=str(
                kwargs.get("expected_chromium_executable_path") or ""
            ),
            expected_chromium_executable_sha256=str(
                kwargs.get("expected_chromium_executable_sha256") or ""
            ),
            browser_engine=str(kwargs["browser_engine"]),
            sync_playwright_factory=_FakePlaywrightManager,
        )

    receipt = performance.build_authenticated_performance_receipt(
        constrained_client_target_url="https://propertyquarry.test/app/search",
        constrained_client_authentication_bootstrap_url=(
            "https://propertyquarry.test/app/access/local-workspace-bootstrap"
        ),
        release_commit_sha=EXPECTED_RELEASE_IDENTITY["commit_sha"],
        release_image_digest=EXPECTED_RELEASE_IDENTITY["image_digest"],
        release_deployment_id=EXPECTED_RELEASE_IDENTITY["deployment_id"],
        release_manifest_sha256=EXPECTED_RELEASE_IDENTITY["manifest_sha256"],
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        constrained_browser_collector=collector,
        release_identity_probe=_passing_release_identity_probe,
    )

    assert receipt["status"] == "pass"
    assert receipt["constrained_client_evidence"]["status"] == "pass"
    assert receipt["constrained_client_evidence"]["engine_rows"][0][
        "authentication"
    ] == {
        "method": "workspace_access_bootstrap_cookie",
        "cookie_observed": True,
        "target_surface_observed": True,
    }
    assert receipt["flagship_status"] == "blocked"
    assert receipt["flagship_blockers"] == [
        "signed_release_probe_authentication_missing_or_blocked"
    ]
    assert receipt["claims"]["signed_release_probe_authentication"] is False


def test_mismatched_live_release_identity_blocks_signed_flagship_lane(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_chromium_executable: dict[str, object],
) -> None:
    _fake_browser_collector(monkeypatch)

    def collector(**kwargs: object) -> dict[str, object]:
        return performance.collect_constrained_client_browser_evidence(
            target_url=str(kwargs["target_url"]),
            authentication_bootstrap_url=str(
                kwargs["authentication_bootstrap_url"]
            ),
            release_probe_secret=str(kwargs.get("release_probe_secret") or ""),
            profile=kwargs["profile"],
            expected_release_identity=kwargs.get("expected_release_identity"),
            expected_chromium_executable_path=str(
                kwargs.get("expected_chromium_executable_path") or ""
            ),
            expected_chromium_executable_sha256=str(
                kwargs.get("expected_chromium_executable_sha256") or ""
            ),
            browser_engine=str(kwargs["browser_engine"]),
            sync_playwright_factory=_FakePlaywrightManager,
        )

    def mismatched_probe(**kwargs: object) -> dict[str, object]:
        row = _passing_release_identity_probe(**kwargs)
        row["status"] = "fail"
        row["observed"] = {
            **OBSERVED_RELEASE_IDENTITY,
            "commit_sha": "d" * 40,
        }
        row["matches_expected"] = False
        row["error"] = "release_identity_mismatch"
        return row

    receipt = performance.build_authenticated_performance_receipt(
        constrained_client_target_url="https://propertyquarry.test/app/search",
        constrained_client_release_probe_secret=RELEASE_PROBE_SECRET,
        release_commit_sha=EXPECTED_RELEASE_IDENTITY["commit_sha"],
        release_image_digest=EXPECTED_RELEASE_IDENTITY["image_digest"],
        release_deployment_id=EXPECTED_RELEASE_IDENTITY["deployment_id"],
        release_manifest_sha256=EXPECTED_RELEASE_IDENTITY["manifest_sha256"],
        expected_chromium_executable_path=str(
            synthetic_chromium_executable["path"]
        ),
        expected_chromium_executable_sha256=str(
            synthetic_chromium_executable["sha256"]
        ),
        constrained_browser_collector=collector,
        release_identity_probe=mismatched_probe,
    )

    assert receipt["status"] == "pass"
    assert receipt["constrained_client_evidence"]["status"] == "pass"
    assert receipt["flagship_status"] == "blocked"
    assert receipt["flagship_blockers"] == [
        "exact_live_release_identity_missing_or_mismatched"
    ]
    assert receipt["claims"]["signed_release_probe_authentication"] is True
    assert receipt["claims"]["exact_live_release_identity_observed"] is False
