from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import RequestContext
from app.api.errors import install_error_handlers
from app.api.ingress import (
    IngressAbuseMiddleware,
    IngressPolicy,
    parse_trusted_proxy_cidrs,
    resolve_client_ip,
)
from app.api.ingress_admission import (
    AdmissionBackend as IngressAdmissionBackend,
    AdmissionOperation,
    AdmissionRequest,
    IngressAdmissionUnavailable,
    InMemoryIngressAdmissionStore,
)
from app.api.routes.product_api_contracts import (
    PropertyMagicFitReferenceUploadIn,
    PropertyMagicFitSceneCreateIn,
    PropertySearchRunStartIn,
)
from app.observability import RuntimeMetrics
from app.services.admission_control import MemoryAdmissionBackend, QuotaCharge
from app.services.admission_control import AdmissionBackendUnavailable


def _policy(**overrides: object) -> IngressPolicy:
    base = IngressPolicy(
        quotas_enabled=True,
        max_body_bytes=128,
        max_upload_body_bytes=512,
        window_seconds=60,
        ip_request_limit=100,
        account_request_limit=100,
        ip_cost_limit=1_000,
        account_cost_limit=1_000,
        high_cost_ip_concurrency=4,
        high_cost_account_concurrency=2,
        active_search_limit=1,
        trusted_proxy_cidrs=parse_trusted_proxy_cidrs("127.0.0.0/8,::1/128"),
    )
    return replace(base, **overrides)


def _app_with_ingress(
    *,
    policy: IngressPolicy,
    clock=lambda: 1.0,
    admission_store=None,
    context_resolver=None,
    active_search_counter=None,
) -> FastAPI:
    app = FastAPI()
    app.state.runtime_metrics = RuntimeMetrics()
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=policy,
        clock=clock,
        admission_store=admission_store,
        context_resolver=context_resolver,
        active_search_counter=active_search_counter,
    )

    @app.get("/limited")
    def limited() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"size": len(body)}

    @app.post("/app/api/property/decision-copilot")
    def expensive() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/app/api/property/search-runs")
    def search() -> dict[str, bool]:
        return {"started": True}

    return app


def test_ingress_policy_is_production_on_and_test_dev_deterministic() -> None:
    production = IngressPolicy.from_environ(runtime_mode="prod", environ={})
    test = IngressPolicy.from_environ(runtime_mode="test", environ={})
    explicit_dev = IngressPolicy.from_environ(
        runtime_mode="dev",
        environ={"PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED": "true"},
    )

    assert production.quotas_enabled is True
    assert test.quotas_enabled is False
    assert explicit_dev.quotas_enabled is True
    with pytest.raises(RuntimeError, match="ingress_quotas_required_in_prod"):
        IngressPolicy.from_environ(
            runtime_mode="prod",
            environ={"PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED": "false"},
        )
    assert resolve_client_ip(
        peer_host="127.0.0.1",
        headers={"x-forwarded-for": "198.51.100.3"},
        trusted_proxy_cidrs=production.trusted_proxy_cidrs,
    ) == "198.51.100.3"


def test_ingress_rejects_declared_and_streamed_oversized_bodies() -> None:
    app = _app_with_ingress(policy=_policy(quotas_enabled=False, max_body_bytes=8))
    client = TestClient(app)

    declared = client.post(
        "/echo",
        content=b"{}",
        headers={"Content-Length": "9", "Content-Type": "application/json"},
    )

    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "request_body_too_large"
    assert declared.json()["error"]["details"] == {"max_body_bytes": 8}

    async def _streamed_request() -> tuple[int, dict[str, object]]:
        middleware = IngressAbuseMiddleware(
            _body_reader_asgi_app,
            policy=_policy(quotas_enabled=False, max_body_bytes=8),
        )
        messages = iter(
            [
                {"type": "http.request", "body": b"12345", "more_body": True},
                {"type": "http.request", "body": b"67890", "more_body": False},
            ]
        )
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return next(messages)

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/echo",
                "raw_path": b"/echo",
                "query_string": b"",
                "headers": [],
                "client": ("203.0.113.5", 1234),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )
        status = int(next(message["status"] for message in sent if message["type"] == "http.response.start"))
        body = b"".join(bytes(message.get("body") or b"") for message in sent if message["type"] == "http.response.body")
        return status, json.loads(body.decode("utf-8"))

    status, payload = asyncio.run(_streamed_request())
    assert status == 413
    assert payload["error"]["code"] == "request_body_too_large"  # type: ignore[index]


async def _body_reader_asgi_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _body_ignoring_asgi_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class _OrderingAdmissionStore(InMemoryIngressAdmissionStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__(clock=lambda: 1.0)
        self.events = events

    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ):  # type: ignore[no-untyped-def]
        self.events.append("ip-admission")
        return super().consume_ip_request(
            subject=subject,
            units=units,
            limit=limit,
            window_seconds=window_seconds,
        )

    def admit(self, request: AdmissionRequest):  # type: ignore[no-untyped-def]
        self.events.append("cost-admission")
        return super().admit(request)


async def _raw_metered_body_request(
    *,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    asgi_app=_body_reader_asgi_app,  # type: ignore[no-untyped-def]
    method: str = "POST",
    admission_store=None,  # type: ignore[no-untyped-def]
    events: list[str] | None = None,
) -> tuple[int, dict[str, object]]:
    middleware = IngressAbuseMiddleware(
        asgi_app,
        policy=_policy(),
        context_resolver=lambda request: None,
        admission_store=admission_store,
    )
    messages = iter(
        [{"type": "http.request", "body": body, "more_body": False}]
    )
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if events is not None:
            events.append("body-read")
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "2",
            "method": method,
            "scheme": "https",
            "path": "/app/api/property/decision-copilot",
            "raw_path": b"/app/api/property/decision-copilot",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.5", 1234),
            "server": ("testserver", 443),
            "state": {},
        },
        receive,
        send,
    )
    status = int(
        next(
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        )
    )
    response_body = b"".join(
        bytes(message.get("body") or b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return (
        status,
        json.loads(response_body.decode("utf-8"))
        if response_body
        else {},
    )


def test_metered_body_requires_exact_non_streaming_content_length() -> None:
    missing_status, missing = asyncio.run(
        _raw_metered_body_request(headers=[], body=b"1234")
    )
    understated_status, understated = asyncio.run(
        _raw_metered_body_request(
            headers=[(b"content-length", b"1")],
            body=b"1234",
        )
    )
    transfer_status, transfer = asyncio.run(
        _raw_metered_body_request(
            headers=[
                (b"content-length", b"4"),
                (b"transfer-encoding", b"chunked"),
            ],
            body=b"1234",
        )
    )

    assert missing_status == 411
    assert missing["error"]["code"] == "content_length_required"  # type: ignore[index]
    assert understated_status == 400
    assert understated["error"]["code"] == "request_body_length_mismatch"  # type: ignore[index]
    assert transfer_status == 400
    assert (
        transfer["error"]["code"]  # type: ignore[index]
        == "request_transfer_encoding_unsupported"
    )


def test_metered_body_is_reconciled_before_a_no_read_endpoint_responds() -> None:
    status, payload = asyncio.run(
        _raw_metered_body_request(
            headers=[(b"content-length", b"1")],
            body=b"x" * 64,
            asgi_app=_body_ignoring_asgi_app,
        )
    )

    assert status == 400
    assert payload["error"]["code"] == "request_body_length_mismatch"  # type: ignore[index]


def test_metered_delete_body_is_reconciled_before_route_dispatch() -> None:
    status, payload = asyncio.run(
        _raw_metered_body_request(
            headers=[(b"content-length", b"1")],
            body=b"x" * 64,
            asgi_app=_body_ignoring_asgi_app,
            method="DELETE",
        )
    )

    assert status == 400
    assert payload["error"]["code"] == "request_body_length_mismatch"  # type: ignore[index]


def test_body_is_buffered_only_after_distributed_ip_admission() -> None:
    events: list[str] = []
    status, payload = asyncio.run(
        _raw_metered_body_request(
            headers=[(b"content-length", b"64")],
            body=b"x" * 64,
            asgi_app=_body_ignoring_asgi_app,
            admission_store=_OrderingAdmissionStore(events),
            events=events,
        )
    )

    assert status == 204
    assert payload == {}
    assert events == ["ip-admission", "body-read", "cost-admission"]


def test_ingress_rate_limit_has_retry_after_envelope_and_health_bypass() -> None:
    app = _app_with_ingress(policy=_policy(ip_request_limit=2))
    client = TestClient(app)

    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    rejected = client.get("/limited")

    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "59"
    assert rejected.json()["error"]["code"] == "ingress_rate_limit_exceeded"
    assert rejected.json()["error"]["correlation_id"]
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live/").status_code in {200, 307}
    assert client.get("/healthz").status_code == 200
    metrics = app.state.runtime_metrics.render_prometheus(readiness_ready=True)
    assert 'propertyquarry_ingress_rejections_total{reason="ingress_rate_limit_exceeded",dimension="ip"} 1' in metrics


def test_ingress_fails_closed_when_distributed_admission_is_unavailable() -> None:
    class _UnavailableAdmission:
        def consume_ip_request(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise IngressAdmissionUnavailable(
                "database unavailable",
                backend=IngressAdmissionBackend.POSTGRES,
                operation=AdmissionOperation.IP_REQUEST,
            )

    app = _app_with_ingress(
        policy=_policy(),
        admission_store=_UnavailableAdmission(),
    )

    response = TestClient(app).get("/limited")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "ingress_admission_unavailable"
    metrics = app.state.runtime_metrics.render_prometheus(readiness_ready=True)
    assert (
        'propertyquarry_ingress_admission_operations_total{backend="postgres",'
        'operation="ip_request",outcome="backend_unavailable"} 1'
    ) in metrics


def test_trusted_proxy_resolution_ignores_spoofed_headers_from_untrusted_peer() -> None:
    trusted = parse_trusted_proxy_cidrs("127.0.0.0/8,10.0.0.0/8")

    assert resolve_client_ip(
        peer_host="203.0.113.10",
        headers={"x-forwarded-for": "198.51.100.7"},
        trusted_proxy_cidrs=trusted,
    ) == "203.0.113.10"
    assert resolve_client_ip(
        peer_host="10.0.0.4",
        headers={"x-forwarded-for": "198.51.100.7, 10.0.0.3"},
        trusted_proxy_cidrs=trusted,
    ) == "198.51.100.7"


def test_quota_key_capacity_fails_closed_without_resetting_live_callers() -> None:
    now = [1.0]
    quota = MemoryAdmissionBackend(clock=lambda: now[0], max_quota_keys=2)

    def consume(key: str) -> bool:
        return quota.consume(
            QuotaCharge(key=key, units=1, limit=2, window_seconds=60)
        ).allowed

    assert consume("caller-a") is True
    assert consume("caller-b") is True
    assert consume("caller-c") is False
    assert consume("caller-a") is True
    assert consume("caller-a") is False

    now[0] = 61.0
    assert consume("caller-c") is True


def test_account_cost_quota_cannot_be_bypassed_with_principal_header() -> None:
    context = RequestContext(principal_id="verified-account", authenticated=True, auth_source="test")
    app = _app_with_ingress(
        policy=_policy(account_cost_limit=12),
        context_resolver=lambda request: context,
    )
    client = TestClient(app)

    first = client.post(
        "/app/api/property/decision-copilot",
        json={},
        headers={"X-EA-Principal-ID": "spoof-a"},
    )
    second = client.post(
        "/app/api/property/decision-copilot",
        json={},
        headers={"X-EA-Principal-ID": "spoof-b"},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "ingress_cost_quota_exceeded"
    assert second.json()["error"]["details"] == {"retry_after_seconds": 59}


def test_identity_resolution_fails_closed_without_replacing_auth_errors() -> None:
    unavailable = _app_with_ingress(
        policy=_policy(),
        context_resolver=lambda request: (_ for _ in ()).throw(RuntimeError("identity store unavailable")),
    )
    unavailable_response = TestClient(unavailable).post("/echo", json={})

    assert unavailable_response.status_code == 503
    assert unavailable_response.headers["Retry-After"] == "5"
    assert unavailable_response.json()["error"]["code"] == "ingress_identity_check_unavailable"

    auth_rejected = _app_with_ingress(
        policy=_policy(),
        context_resolver=lambda request: (_ for _ in ()).throw(
            HTTPException(status_code=401, detail="auth_required")
        ),
    )
    auth_response = TestClient(auth_rejected).post("/echo", json={})

    # The middleware preserves IP controls but delegates canonical auth handling
    # to the application instead of converting a caller error into a 503.
    assert auth_response.status_code == 200


@pytest.mark.parametrize(
    "principal_id",
    (
        "",
        "x" * 1_025,
        "verified-account\nforged",
        " verified-account",
    ),
)
def test_malformed_authenticated_principal_fails_closed_without_disclosure(
    principal_id: str,
) -> None:
    context = RequestContext(
        principal_id=principal_id,
        authenticated=True,
        auth_source="test",
    )
    app = _app_with_ingress(
        policy=_policy(),
        context_resolver=lambda request: context,
    )

    response = TestClient(app).post(
        "/app/api/property/decision-copilot",
        json={},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "ingress_identity_invalid"
    metrics = app.state.runtime_metrics.render_prometheus(
        readiness_ready=False
    )
    assert (
        "propertyquarry_ingress_rejections_total"
        '{reason="ingress_identity_invalid",dimension="account"} 1'
    ) in metrics
    if principal_id:
        assert principal_id not in response.text
        assert principal_id not in metrics


def test_high_cost_account_concurrency_is_shed() -> None:
    entered = threading.Event()
    release = threading.Event()
    context = RequestContext(principal_id="concurrent-account", authenticated=True, auth_source="test")
    app = FastAPI()
    app.state.runtime_metrics = RuntimeMetrics()
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=_policy(high_cost_account_concurrency=1),
        admission_store=InMemoryIngressAdmissionStore(clock=lambda: 1.0),
        context_resolver=lambda request: context,
    )

    @app.post("/app/api/property/decision-copilot")
    def expensive() -> dict[str, bool]:
        entered.set()
        release.wait(timeout=3)
        return {"ok": True}

    first_result: dict[str, object] = {}

    def _first_request() -> None:
        with TestClient(app) as first_client:
            first_result["response"] = first_client.post(
                "/app/api/property/decision-copilot",
                json={},
            )

    thread = threading.Thread(target=_first_request)
    thread.start()
    assert entered.wait(timeout=2)
    with TestClient(app) as second_client:
        rejected = second_client.post("/app/api/property/decision-copilot", json={})
    release.set()
    thread.join(timeout=3)

    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.json()["error"]["code"] == "ingress_concurrency_limit_exceeded"
    assert first_result["response"].status_code == 200  # type: ignore[union-attr]


def test_active_property_search_cap_rejects_before_route_dispatch() -> None:
    context = RequestContext(principal_id="search-account", authenticated=True, auth_source="test")
    app = _app_with_ingress(
        policy=_policy(),
        context_resolver=lambda request: context,
        active_search_counter=lambda request, resolved, limit: 1,
    )
    client = TestClient(app)

    rejected = client.post("/app/api/property/search-runs", json={})

    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "60"
    assert rejected.json()["error"]["code"] == "active_search_limit_exceeded"
    assert rejected.json()["error"]["details"] == {
        "active_search_limit": 1,
        "retry_after_seconds": 60,
    }


def test_active_search_check_and_dispatch_are_serialized_across_replicas() -> None:
    shared_admission = InMemoryIngressAdmissionStore(clock=lambda: 1.0)
    context = RequestContext(
        principal_id="shared-search-account",
        authenticated=True,
        auth_source="test",
    )
    entered = threading.Event()
    release = threading.Event()
    active_count = [0]

    def build_replica(*, blocking_route: bool) -> FastAPI:
        app = FastAPI()
        app.state.runtime_metrics = RuntimeMetrics()
        app.add_middleware(
            IngressAbuseMiddleware,
            policy=_policy(active_search_limit=1),
            admission_store=shared_admission,
            context_resolver=lambda request: context,
            active_search_counter=lambda request, resolved, limit: active_count[0],
        )

        @app.post("/app/api/property/search-runs")
        def start_search() -> dict[str, bool]:
            if blocking_route:
                entered.set()
                release.wait(timeout=3)
            active_count[0] += 1
            return {"started": True}

        return app

    first_replica = build_replica(blocking_route=True)
    second_replica = build_replica(blocking_route=False)
    first_result: dict[str, object] = {}

    def first_request() -> None:
        first_result["response"] = TestClient(first_replica).post(
            "/app/api/property/search-runs",
            json={},
        )

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(timeout=2)

    concurrent = TestClient(second_replica).post(
        "/app/api/property/search-runs",
        json={},
    )
    release.set()
    thread.join(timeout=3)
    after_commit = TestClient(second_replica).post(
        "/app/api/property/search-runs",
        json={},
    )

    assert concurrent.status_code == 429
    assert concurrent.json()["error"]["code"] == "ingress_concurrency_limit_exceeded"
    assert first_result["response"].status_code == 200  # type: ignore[union-attr]
    assert after_commit.status_code == 429
    assert after_commit.json()["error"]["code"] == "active_search_limit_exceeded"
    assert active_count == [1]


def test_ingress_rejections_keep_correlation_and_browser_security_headers() -> None:
    app = _app_with_ingress(policy=_policy(quotas_enabled=False, max_body_bytes=8))
    install_error_handlers(app)
    client = TestClient(app)

    rejected = client.post(
        "/echo",
        content=b"{}",
        headers={"Content-Length": "9", "Content-Type": "application/json"},
    )

    assert rejected.status_code == 413
    assert rejected.headers["x-correlation-id"] == rejected.json()["error"]["correlation_id"]
    assert rejected.headers["x-content-type-options"] == "nosniff"


def test_high_cost_input_schemas_forbid_extra_and_bound_nested_work() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PropertySearchRunStartIn.model_validate({"unexpected": True})
    with pytest.raises(ValidationError, match="too_long"):
        PropertySearchRunStartIn.model_validate({"selected_platforms": [f"source-{index}" for index in range(25)]})
    with pytest.raises(ValidationError, match="too_long"):
        PropertySearchRunStartIn.model_validate(
            {"property_preferences": {f"key-{index}": index for index in range(129)}}
        )
    with pytest.raises(ValidationError, match="min_rooms_out_of_range"):
        PropertySearchRunStartIn.model_validate({"property_preferences": {"min_rooms": 101}})
    with pytest.raises(ValidationError, match="nested_payload_depth_exceeds_limit"):
        PropertySearchRunStartIn.model_validate(
            {
                "property_preferences": {
                    "a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}
                }
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PropertyMagicFitSceneCreateIn.model_validate(
            {"property_ref": "property-1", "surprise": "not accepted"}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PropertyMagicFitReferenceUploadIn.model_validate(
            {
                "items": [
                    {
                        "file_name": "room.jpg",
                        "mime_type": "image/jpeg",
                        "data_url": "data:image/jpeg;base64,AA==",
                        "unexpected": True,
                    }
                ]
            }
        )
