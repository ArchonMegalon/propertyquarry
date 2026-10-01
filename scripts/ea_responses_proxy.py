#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

EA_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ea"
if str(EA_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_RUNTIME_ROOT))

from app.api.dependencies import RequestContext  # noqa: E402
from app.api.routes.responses import _run_response  # noqa: E402
from app.main import app  # noqa: E402
from app.observability import (  # noqa: E402
    bind_runtime_trace_context,
    bounded_correlation_id,
    new_server_trace_context,
)
from app.services.admission_control import (  # noqa: E402
    AdmissionBackend,
    AdmissionBackendUnavailable,
    ConcurrencyDimension,
    QuotaCharge,
    build_admission_backend,
)
from app.services.responses_upstream import (  # noqa: E402
    _provider_health_snapshot,
    _provider_row_is_ready,
    list_response_models,
)
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402


LOG = logging.getLogger("ea.responses_proxy")
CONTAINER = app.state.container
AUTH_TOKEN = str(CONTAINER.settings.auth.api_token or "").strip()


@dataclass(frozen=True)
class ProxyConfig:
    host: str = "127.0.0.1"
    port: int = 8091
    auth_token: str = ""
    dev_mode: bool = False
    max_body_bytes: int = 1_048_576
    request_timeout_seconds: int = 30
    max_concurrency: int = 4
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    max_output_tokens: int = 32_768
    max_input_items: int = 256
    max_tools: int = 128


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name.lower()}_invalid")


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        parsed = int(raw or str(default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name.lower()}_invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name.lower()}_out_of_range")
    return parsed


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _load_proxy_config() -> ProxyConfig:
    return ProxyConfig(
        host=str(os.environ.get("EA_RESPONSES_PROXY_HOST") or "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int("EA_RESPONSES_PROXY_PORT", default=8091, minimum=1, maximum=65_535),
        auth_token=str(os.environ.get("EA_RESPONSES_PROXY_AUTH_TOKEN") or AUTH_TOKEN or "").strip(),
        dev_mode=_env_bool("EA_RESPONSES_PROXY_DEV_MODE"),
        max_body_bytes=_env_int(
            "EA_RESPONSES_PROXY_MAX_BODY_BYTES",
            default=1_048_576,
            minimum=1_024,
            maximum=16_777_216,
        ),
        request_timeout_seconds=_env_int(
            "EA_RESPONSES_PROXY_REQUEST_TIMEOUT_SECONDS",
            default=30,
            minimum=1,
            maximum=300,
        ),
        max_concurrency=_env_int("EA_RESPONSES_PROXY_MAX_CONCURRENCY", default=4, minimum=1, maximum=64),
        rate_limit_requests=_env_int(
            "EA_RESPONSES_PROXY_RATE_LIMIT_REQUESTS",
            default=120,
            minimum=1,
            maximum=100_000,
        ),
        rate_limit_window_seconds=_env_int(
            "EA_RESPONSES_PROXY_RATE_LIMIT_WINDOW_SECONDS",
            default=60,
            minimum=1,
            maximum=3_600,
        ),
        max_output_tokens=_env_int(
            "EA_RESPONSES_PROXY_MAX_OUTPUT_TOKENS",
            default=32_768,
            minimum=1,
            maximum=1_000_000,
        ),
        max_input_items=_env_int(
            "EA_RESPONSES_PROXY_MAX_INPUT_ITEMS",
            default=256,
            minimum=1,
            maximum=10_000,
        ),
        max_tools=_env_int("EA_RESPONSES_PROXY_MAX_TOOLS", default=128, minimum=1, maximum=1_000),
    )


def _validate_proxy_config(config: ProxyConfig) -> None:
    loopback = _is_loopback_host(config.host)
    has_token = bool(str(config.auth_token or "").strip())
    if config.dev_mode and not loopback:
        raise RuntimeError("ea_responses_proxy_dev_mode_requires_loopback")
    if not has_token and not (config.dev_mode and loopback):
        raise RuntimeError("ea_responses_proxy_auth_token_required")
    if config.port < 0 or config.port > 65_535:
        raise RuntimeError("ea_responses_proxy_port_out_of_range")
    if min(
        config.max_body_bytes,
        config.request_timeout_seconds,
        config.max_concurrency,
        config.rate_limit_requests,
        config.rate_limit_window_seconds,
        config.max_output_tokens,
        config.max_input_items,
        config.max_tools,
    ) < 1:
        raise RuntimeError("ea_responses_proxy_limit_out_of_range")


def _proxy_admission_backend(config: ProxyConfig) -> AdmissionBackend:
    database_url = str(
        os.environ.get("DATABASE_URL")
        or getattr(CONTAINER.settings, "database_url", "")
        or ""
    ).strip()
    return build_admission_backend(
        runtime_mode="dev" if config.dev_mode else "prod",
        database_url=database_url,
    )


def _lease_renew_interval_seconds(lease_seconds: int) -> float:
    return max(0.25, min(30.0, float(max(1, int(lease_seconds))) / 3.0))


class _AdmissionLeaseKeepalive:
    """Renew one distributed lease until request and stream teardown complete."""

    def __init__(
        self,
        backend: AdmissionBackend,
        lease_id: str,
        *,
        lease_seconds: int,
    ) -> None:
        self._backend = backend
        self._lease_id = str(lease_id or "").strip()
        self._lease_seconds = max(1, int(lease_seconds))
        self._interval_seconds = _lease_renew_interval_seconds(self._lease_seconds)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="responses-proxy-admission-lease",
            daemon=True,
        )

    @property
    def healthy(self) -> bool:
        return not self._lost.is_set()

    def __enter__(self) -> "_AdmissionLeaseKeepalive":
        if not self._lease_id:
            self._lost.set()
            return self
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                renewed = self._backend.renew(
                    self._lease_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                LOG.exception("responses proxy admission lease renewal failed")
                self._lost.set()
                return
            if not renewed:
                LOG.error("responses proxy admission lease expired before renewal")
                self._lost.set()
                return


def _response_cost_violation(payload: dict[str, Any], *, config: ProxyConfig) -> str:
    requested_tokens = payload.get("max_output_tokens")
    if requested_tokens is not None:
        if isinstance(requested_tokens, bool):
            return "max_output_tokens_invalid"
        try:
            normalized_tokens = int(requested_tokens)
        except (TypeError, ValueError):
            return "max_output_tokens_invalid"
        if normalized_tokens < 1:
            return "max_output_tokens_invalid"
        if normalized_tokens > config.max_output_tokens:
            return "max_output_tokens_exceeds_limit"
    input_items = payload.get("input")
    if isinstance(input_items, list) and len(input_items) > config.max_input_items:
        return "input_items_exceed_limit"
    tools = payload.get("tools")
    if isinstance(tools, list) and len(tools) > config.max_tools:
        return "tools_exceed_limit"
    return ""


def _proxy_readiness(
    config: ProxyConfig,
    *,
    admission_backend: AdmissionBackend | None = None,
) -> tuple[bool, dict[str, Any]]:
    try:
        _validate_proxy_config(config)
        backend = admission_backend or _proxy_admission_backend(config)
        backend.probe()
        models = list_response_models()
    except AdmissionBackendUnavailable:
        return False, {
            "status": "not_ready",
            "reason": "responses_proxy_admission_unavailable",
        }
    except Exception as exc:
        return False, {
            "status": "not_ready",
            "reason": "responses_proxy_dependency_check_failed",
            "detail": exc.__class__.__name__,
        }
    if not isinstance(models, list) or not models:
        return False, {"status": "not_ready", "reason": "responses_proxy_model_catalog_empty"}
    provider_health = _provider_health_snapshot(lightweight=True)
    providers = dict(provider_health.get("providers") or {})
    ready_provider_count = sum(
        1
        for provider in providers.values()
        if isinstance(provider, dict) and _provider_row_is_ready(provider)
    )
    if ready_provider_count < 1:
        return False, {
            "status": "not_ready",
            "reason": "responses_proxy_provider_unavailable",
            "provider_count": len(providers),
        }
    return True, {
        "status": "ready",
        "reason": "responses_proxy_ready",
        "model_count": len(models),
        "ready_provider_count": ready_provider_count,
        "security_mode": "loopback_dev" if config.dev_mode else "authenticated",
    }


def _normalize_profile(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value == "jury":
        value = "audit"
    if value == "review-light":
        value = "review_light"
    if value not in {"core", "core_batch", "core_rescue", "easy", "repair", "groundwork", "review_light", "survival", "audit"}:
        return ""
    return value


def _preferred_onemin_labels(headers: BaseHTTPRequestHandler.headers.__class__) -> tuple[str, ...]:
    labels: list[str] = []
    for header_name in (
        "X-EA-Onemin-Account-Alias",
        "X-EA-Onemin-Account-Env",
        "X-EA-Onemin-Account",
        "X-EA-Onemin-Preferred-Accounts",
    ):
        raw = str(headers.get(header_name) or "").strip()
        if not raw:
            continue
        for part in raw.replace(";", ",").split(","):
            label = str(part or "").strip()
            if label and label not in labels:
                labels.append(label)
    return tuple(labels)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class ResponsesProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EAResponsesProxy"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._config.request_timeout_seconds)

    @property
    def _config(self) -> ProxyConfig:
        return getattr(self.server, "proxy_config")

    @property
    def _admission_backend(self) -> AdmissionBackend:
        return getattr(self.server, "admission_backend")

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _request_observability(self):  # type: ignore[no-untyped-def]
        trace_context = getattr(self, "_runtime_trace_context", None)
        if trace_context is None:
            trace_context = new_server_trace_context(self.headers.get("traceparent"))
            self._runtime_trace_context = trace_context
        correlation_id = str(getattr(self, "_runtime_correlation_id", "") or "")
        if not correlation_id:
            correlation_id = bounded_correlation_id(
                self.headers.get("x-correlation-id")
            )
            self._runtime_correlation_id = correlation_id
        return trace_context, correlation_id

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        trace_context, correlation_id = self._request_observability()
        self.close_connection = True
        body = _json_bytes(payload)
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("traceparent", trace_context.traceparent)
        self.send_header("x-correlation-id", correlation_id)
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_payload(body)

    def _write_payload(self, payload: bytes) -> bool:
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("responses proxy client disconnected before payload flush")
            return False

    def _allow_rate_limited_request(self) -> bool:
        client_key = str(self.client_address[0] if self.client_address else "unknown")
        try:
            decision = self._admission_backend.consume(
                QuotaCharge(
                    key=f"responses_proxy:request:ip:{client_key}",
                    units=1,
                    limit=self._config.rate_limit_requests,
                    window_seconds=self._config.rate_limit_window_seconds,
                    dimension="ip",
                )
            )
        except AdmissionBackendUnavailable:
            self._send_json(
                503,
                {
                    "error": {
                        "code": "admission_unavailable",
                        "message": "request admission could not be verified",
                    }
                },
            )
            return False
        if decision.allowed:
            return True
        self._send_json(
            429,
            {
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "request rate limit exceeded",
                }
            },
        )
        return False

    def _auth_context(self) -> RequestContext | None:
        authorization = str(self.headers.get("Authorization") or "").strip()
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        provided = str(
            self.headers.get("x-ea-api-token")
            or self.headers.get("x-api-token")
            or bearer
            or ""
        ).strip()
        expected = str(self._config.auth_token or "").strip()
        if expected and not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            self._send_json(
                401,
                {"error": {"code": "auth_required", "message": "auth_required"}},
            )
            return None
        principal_id = str(self.headers.get("X-EA-Principal-ID") or "").strip()
        if not principal_id:
            principal_id = str(CONTAINER.settings.auth.default_principal_id or "").strip() or "local-user"
        return RequestContext(
            principal_id=principal_id,
            authenticated=bool(expected),
            auth_source="api_token" if expected else "loopback_dev",
        )

    def _read_payload(self) -> dict[str, Any] | None:
        if str(self.headers.get("Transfer-Encoding") or "").strip():
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "transfer_encoding_unsupported"}},
            )
            return None
        raw_content_length = str(self.headers.get("Content-Length") or "").strip()
        if not raw_content_length:
            self._send_json(
                411,
                {"error": {"code": "length_required", "message": "content_length_required"}},
            )
            return None
        try:
            content_length = int(raw_content_length)
        except ValueError:
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "content_length_invalid"}},
            )
            return None
        if content_length < 1:
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "request_body_required"}},
            )
            return None
        if content_length > self._config.max_body_bytes:
            self._send_json(
                413,
                {"error": {"code": "payload_too_large", "message": "request body exceeds configured limit"}},
            )
            return None
        try:
            raw = self.rfile.read(content_length)
        except TimeoutError:
            self._send_json(
                408,
                {"error": {"code": "request_timeout", "message": "request body read timed out"}},
            )
            return None
        if len(raw) != content_length:
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "request_body_incomplete"}},
            )
            return None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "invalid_json"}},
            )
            return None
        if not isinstance(payload, dict):
            self._send_json(
                400,
                {"error": {"code": "bad_request", "message": "invalid_payload"}},
            )
            return None
        return payload

    def _write_starlette_response(
        self,
        response: JSONResponse | StreamingResponse,
        *,
        lease_keepalive: _AdmissionLeaseKeepalive | None = None,
    ) -> None:
        if lease_keepalive is not None and not lease_keepalive.healthy:
            self._send_json(
                503,
                {
                    "error": {
                        "code": "admission_lease_lost",
                        "message": "request concurrency authority expired",
                    }
                },
            )
            return
        trace_context, correlation_id = self._request_observability()
        self.close_connection = True
        self.send_response(int(getattr(response, "status_code", 200) or 200))
        for key, value in response.headers.items():
            lowered = str(key).strip().lower()
            if lowered in {"content-length", "traceparent", "x-correlation-id"}:
                continue
            self.send_header(key, value)
        self.send_header("traceparent", trace_context.traceparent)
        self.send_header("x-correlation-id", correlation_id)
        self.send_header("Connection", "close")
        body = getattr(response, "body", None)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body is not None:
            self._write_payload(body)
            return

        async def _stream() -> None:
            if lease_keepalive is None:
                async for chunk in response.body_iterator:
                    payload = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                    if not self._write_payload(payload):
                        break
                return

            iterator = response.body_iterator.__aiter__()
            pending: asyncio.Task[object] | None = None
            try:
                while lease_keepalive.healthy:
                    if pending is None:
                        pending = asyncio.create_task(anext(iterator))
                    completed, _pending = await asyncio.wait(
                        {pending},
                        timeout=0.25,
                    )
                    if not lease_keepalive.healthy:
                        break
                    if not completed:
                        continue
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    payload = (
                        chunk.encode("utf-8")
                        if isinstance(chunk, str)
                        else bytes(chunk)
                    )
                    if not self._write_payload(payload):
                        return
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(BaseException):
                        await pending
                close_iterator = getattr(iterator, "aclose", None)
                if callable(close_iterator):
                    with suppress(BaseException):
                        await close_iterator()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_stream())
        finally:
            loop.close()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health/live":
            self._send_json(200, {"status": "live", "reason": "responses_proxy_live"})
            return
        if parsed.path == "/health/ready":
            ready, payload = _proxy_readiness(
                self._config,
                admission_backend=self._admission_backend,
            )
            self._send_json(200 if ready else 503, payload)
            return
        if parsed.path == "/v1/models":
            if not self._allow_rate_limited_request():
                return
            if self._auth_context() is None:
                return
            self._send_json(200, {"object": "list", "data": list_response_models()})
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/v1/responses":
            self._send_json(404, {"error": {"code": "not_found", "message": "not_found"}})
            return
        if not self._allow_rate_limited_request():
            return
        context = self._auth_context()
        if context is None:
            return
        payload = self._read_payload()
        if payload is None:
            return
        trace_context, correlation_id = self._request_observability()
        metadata = (
            dict(payload.get("metadata") or {})
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        metadata["ea_correlation_id"] = correlation_id
        metadata["ea_traceparent"] = trace_context.traceparent
        payload["metadata"] = metadata
        cost_violation = _response_cost_violation(payload, config=self._config)
        if cost_violation:
            self._send_json(
                422,
                {
                    "error": {
                        "code": "request_cost_limit",
                        "message": cost_violation,
                    }
                },
            )
            return
        profile = _normalize_profile(
            str(self.headers.get("X-EA-Codex-Profile") or self.headers.get("X-CodexEA-Profile") or "")
        )
        lease_seconds = self._config.request_timeout_seconds + 30
        try:
            concurrency = self._admission_backend.acquire(
                (
                    ConcurrencyDimension(
                        key="responses_proxy:concurrency:global",
                        limit=self._config.max_concurrency,
                        dimension="global",
                    ),
                ),
                lease_seconds=lease_seconds,
            )
        except AdmissionBackendUnavailable:
            self._send_json(
                503,
                {
                    "error": {
                        "code": "admission_unavailable",
                        "message": "request admission could not be verified",
                    }
                },
            )
            return
        if not concurrency.allowed:
            self._send_json(
                503,
                {
                    "error": {
                        "code": "concurrency_limit",
                        "message": "responses proxy is at its configured concurrency limit",
                    }
                },
            )
            return
        try:
            with _AdmissionLeaseKeepalive(
                self._admission_backend,
                concurrency.lease_id,
                lease_seconds=lease_seconds,
            ) as lease_keepalive:
                try:
                    with bind_runtime_trace_context(
                        trace_context,
                        correlation_id=correlation_id,
                    ):
                        response = _run_response(
                            payload,
                            context=context,
                            container=CONTAINER,
                            codex_profile=profile or None,
                            preferred_onemin_labels=_preferred_onemin_labels(self.headers),
                        )
                except Exception as exc:
                    LOG.exception("responses proxy request failed")
                    if not lease_keepalive.healthy:
                        self._send_json(
                            503,
                            {
                                "error": {
                                    "code": "admission_lease_lost",
                                    "message": "request concurrency authority expired",
                                }
                            },
                        )
                    else:
                        self._send_json(
                            500,
                            {
                                "error": {
                                    "code": "internal_error",
                                    "message": "internal server error",
                                    "details": exc.__class__.__name__,
                                }
                            },
                        )
                    return
                self._write_starlette_response(
                    response,
                    lease_keepalive=lease_keepalive,
                )
        finally:
            try:
                self._admission_backend.release(concurrency.lease_id)
            except AdmissionBackendUnavailable:
                LOG.exception("responses proxy admission lease release failed")


class ResponsesProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        config: ProxyConfig,
        admission_backend: AdmissionBackend | None = None,
    ) -> None:
        _validate_proxy_config(config)
        self.proxy_config = config
        self.admission_backend = admission_backend or _proxy_admission_backend(config)
        super().__init__(server_address, handler_class)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, str(os.environ.get("EA_RESPONSES_PROXY_LOG_LEVEL") or "INFO").strip().upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = _load_proxy_config()
    _validate_proxy_config(config)
    server = ResponsesProxyServer((config.host, config.port), ResponsesProxyHandler, config=config)
    LOG.info(
        "responses proxy listening host=%s port=%s security_mode=%s max_body_bytes=%s max_concurrency=%s",
        config.host,
        config.port,
        "loopback_dev" if config.dev_mode else "authenticated",
        config.max_body_bytes,
        config.max_concurrency,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
