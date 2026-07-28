from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import RequestContext, get_request_context
from app.api.ingress_admission import (
    AdmissionBackend,
    AdmissionDimension,
    AdmissionOperation,
    AdmissionOutcome,
    AdmissionRequest,
    AdmissionResult,
    IngressAdmissionError,
    IngressAdmissionStore,
    InMemoryIngressAdmissionStore,
    LeaseScope,
    QuotaCharge,
    QuotaKind,
)
from app.observability import get_runtime_metrics
from app.product.service import build_product_service
from app.services.admission_control import (
    AdmissionBackend,
    AdmissionBackendUnavailable,
    AdmissionDecision,
    ConcurrencyDimension,
    QuotaCharge,
)

_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_BYPASS_PATHS = {
    "/health",
    "/healthz",
    "/health/live",
    "/health/ready",
    "/internal/metrics",
    "/metrics",
}
_DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.0/8", "::1/128")
_DEFAULT_TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(value, strict=False)
    for value in _DEFAULT_TRUSTED_PROXY_CIDRS
)
LOG = logging.getLogger("ea.ingress")


class RequestBodyLimitExceeded(RuntimeError):
    pass


class RequestBodyLengthMismatch(RuntimeError):
    pass


class IngressLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class IngressRouteRule:
    name: str
    cost_units: int
    high_cost: bool = False
    active_search: bool = False
    max_body_bytes: int | None = None


_ROUTE_RULES: dict[tuple[str, str], IngressRouteRule] = {
    ("POST", "/app/api/property/search-runs"): IngressRouteRule(
        "property_search_start",
        25,
        high_cost=True,
        active_search=True,
    ),
    ("POST", "/app/api/signals/property/search/run"): IngressRouteRule(
        "property_search_start_legacy",
        25,
        high_cost=True,
        active_search=True,
    ),
    ("POST", "/app/api/signals/property/scout"): IngressRouteRule(
        "property_scout_sync",
        25,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/willhaben/property-tour"): IngressRouteRule(
        "property_tour_create",
        25,
        high_cost=True,
    ),
    ("POST", "/app/api/property/decision-copilot"): IngressRouteRule(
        "property_decision_copilot",
        12,
        high_cost=True,
    ),
    ("POST", "/app/api/property/magic-fit-scenes"): IngressRouteRule(
        "property_magic_fit_scene",
        25,
        high_cost=True,
    ),
    ("POST", "/app/api/property/magic-fit-reference-files"): IngressRouteRule(
        "property_magic_fit_upload",
        10,
        high_cost=True,
        max_body_bytes=40_000_000,
    ),
    ("POST", "/app/api/signals/google/photos/session"): IngressRouteRule(
        "google_photos_session",
        5,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/photos/sync"): IngressRouteRule(
        "google_photos_sync",
        15,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/sync"): IngressRouteRule(
        "google_workspace_sync",
        15,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/willhaben-sync"): IngressRouteRule(
        "google_property_sync",
        15,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/property-sync"): IngressRouteRule(
        "google_property_sync",
        15,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/location-history/sync"): IngressRouteRule(
        "google_location_sync",
        15,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/ingest"): IngressRouteRule(
        "office_signal_ingest",
        5,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/pocket/import-local"): IngressRouteRule(
        "pocket_import",
        10,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/noneverbia/import-local"): IngressRouteRule(
        "noneverbia_import",
        10,
        high_cost=True,
    ),
    ("POST", "/app/api/signals/google/location-history/import"): IngressRouteRule(
        "google_location_import",
        10,
        high_cost=True,
    ),
    ("POST", "/v1/responses"): IngressRouteRule(
        "responses_create",
        20,
        high_cost=True,
    ),
}


def _optional_bool(raw: str | None, *, default: bool) -> bool:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("propertyquarry_ingress_boolean_invalid")


def _bounded_env_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(environ.get(name) or "").strip()
    try:
        parsed = int(raw or str(default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name.lower()}_invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name.lower()}_out_of_range")
    return parsed


def parse_trusted_proxy_cidrs(raw: str | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    values = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not values:
        return _DEFAULT_TRUSTED_PROXY_NETWORKS
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise RuntimeError("propertyquarry_trusted_proxy_cidr_invalid") from exc
    return tuple(networks)


@dataclass(frozen=True)
class IngressPolicy:
    quotas_enabled: bool
    max_body_bytes: int = 8_388_608
    max_upload_body_bytes: int = 40_000_000
    window_seconds: int = 60
    ip_request_limit: int = 600
    account_request_limit: int = 240
    ip_cost_limit: int = 1_000
    account_cost_limit: int = 300
    high_cost_ip_concurrency: int = 8
    high_cost_account_concurrency: int = 2
    active_search_limit: int = 1
    concurrency_lease_seconds: int = 600
    trusted_proxy_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_environ(
        cls,
        *,
        runtime_mode: str,
        environ: Mapping[str, str] | None = None,
    ) -> IngressPolicy:
        env = environ if environ is not None else os.environ
        production_default = str(runtime_mode or "").strip().lower() == "prod"
        quotas_enabled = _optional_bool(
            env.get("PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED"),
            default=production_default,
        )
        if production_default and not quotas_enabled:
            raise RuntimeError("propertyquarry_ingress_quotas_required_in_prod")
        return cls(
            quotas_enabled=quotas_enabled,
            max_body_bytes=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_MAX_BODY_BYTES",
                default=8_388_608,
                minimum=1_024,
                maximum=134_217_728,
            ),
            max_upload_body_bytes=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_MAX_UPLOAD_BODY_BYTES",
                default=40_000_000,
                minimum=1_024,
                maximum=134_217_728,
            ),
            window_seconds=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_QUOTA_WINDOW_SECONDS",
                default=60,
                minimum=1,
                maximum=3_600,
            ),
            ip_request_limit=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_IP_REQUEST_LIMIT",
                default=600,
                minimum=1,
                maximum=1_000_000,
            ),
            account_request_limit=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_ACCOUNT_REQUEST_LIMIT",
                default=240,
                minimum=1,
                maximum=1_000_000,
            ),
            ip_cost_limit=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_IP_COST_LIMIT",
                default=1_000,
                minimum=1,
                maximum=10_000_000,
            ),
            account_cost_limit=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_ACCOUNT_COST_LIMIT",
                default=300,
                minimum=1,
                maximum=10_000_000,
            ),
            high_cost_ip_concurrency=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_HIGH_COST_IP_CONCURRENCY",
                default=8,
                minimum=1,
                maximum=1_000,
            ),
            high_cost_account_concurrency=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_HIGH_COST_ACCOUNT_CONCURRENCY",
                default=2,
                minimum=1,
                maximum=1_000,
            ),
            active_search_limit=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_ACTIVE_SEARCH_LIMIT",
                default=1,
                minimum=1,
                maximum=20,
            ),
            concurrency_lease_seconds=_bounded_env_int(
                env,
                "PROPERTYQUARRY_INGRESS_CONCURRENCY_LEASE_SECONDS",
                default=600,
                minimum=30,
                maximum=7_200,
            ),
            trusted_proxy_cidrs=parse_trusted_proxy_cidrs(
                env.get("PROPERTYQUARRY_TRUSTED_PROXY_CIDRS")
            ),
        )
        if production_default:
            for field_name, expected in _PRODUCTION_INGRESS_POLICY_V1.items():
                if getattr(policy, field_name) != expected:
                    raise RuntimeError(
                        "propertyquarry_prod_ingress_policy_v1_mismatch:"
                        + field_name
                    )
        return policy


def _parse_ip(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    normalized = str(value or "").strip().strip('"').strip("[]")
    if not normalized or len(normalized) > 64:
        return None
    if normalized.lower().startswith("for="):
        normalized = normalized[4:].strip().strip('"').strip("[]")
    if "]" in normalized:
        normalized = normalized.split("]", 1)[0].lstrip("[")
    elif normalized.count(":") == 1 and "." in normalized:
        normalized = normalized.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        return None


def _ip_is_trusted(
    value: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(value.version == network.version and value in network for network in networks)


def resolve_client_ip(
    *,
    peer_host: object,
    headers: Mapping[str, str],
    trusted_proxy_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str:
    peer = _parse_ip(peer_host)
    if peer is None:
        return "unknown"
    if not _ip_is_trusted(peer, trusted_proxy_cidrs):
        return peer.compressed

    connecting_ip = _parse_ip(headers.get("cf-connecting-ip"))
    forwarded: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if connecting_ip is not None:
        forwarded = [connecting_ip]
    else:
        raw_xff = str(headers.get("x-forwarded-for") or "")[:2_048]
        for part in raw_xff.split(",")[-16:]:
            parsed = _parse_ip(part)
            if parsed is not None:
                forwarded.append(parsed)
    for candidate in reversed([*forwarded, peer]):
        if _ip_is_trusted(candidate, trusted_proxy_cidrs):
            continue
        return candidate.compressed
    return peer.compressed


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name = bytes(raw_name).decode("latin-1").strip().lower()
        value = bytes(raw_value).decode("latin-1").strip()
        if name in headers:
            headers[name] = f"{headers[name]},{value}"
        else:
            headers[name] = value
    return headers


def _route_rule(method: str, path: str, *, policy: IngressPolicy) -> IngressRouteRule | None:
    normalized_path = str(path or "/").rstrip("/") or "/"
    configured = _ROUTE_RULES.get((method, normalized_path))
    if configured is None:
        if method not in _MUTATION_METHODS:
            return None
        return IngressRouteRule("mutation", 1)
    if configured.max_body_bytes is None:
        return configured
    return IngressRouteRule(
        configured.name,
        configured.cost_units,
        high_cost=configured.high_cost,
        active_search=configured.active_search,
        max_body_bytes=policy.max_upload_body_bytes,
    )


def _account_subject(context: RequestContext | None) -> str:
    if context is None or not bool(getattr(context, "authenticated", False)):
        return ""
    principal_id = str(getattr(context, "principal_id", "") or "")
    if not principal_id:
        raise ValueError("ingress_account_subject_invalid")
    try:
        encoded_principal = principal_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("ingress_account_subject_invalid") from exc
    if (
        principal_id != principal_id.strip()
        or len(encoded_principal) > _MAX_ACCOUNT_SUBJECT_BYTES
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in principal_id
        )
    ):
        raise ValueError("ingress_account_subject_invalid")
    return principal_id


def _request_context_sync(request: Request) -> RequestContext | None:
    container = getattr(request.app.state, "container", None)
    if container is None:
        return None
    return get_request_context(request=request, container=container)


def _active_property_search_count_sync(request: Request, context: RequestContext, *, limit: int) -> int:
    container = getattr(request.app.state, "container", None)
    if container is None:
        return 0
    service = build_product_service(container)
    if int(limit) <= 1:
        active = service.find_active_property_search_run(
            principal_id=context.principal_id,
            limit=8,
        )
        return 1 if isinstance(active, dict) and active else 0
    rows = service.list_property_search_runs(
        principal_id=context.principal_id,
        limit=max(int(limit) + 8, 12),
        hydrate=False,
    )
    terminal = {
        "completed",
        "completed_partial",
        "processed",
        "failed",
        "cancelled",
        "deleted",
        "noop",
        "not started",
    }
    active = 0
    for row in list(rows or []):
        summary = dict(row.get("summary") or {}) if isinstance(row.get("summary"), dict) else {}
        status = str(row.get("status") or summary.get("status") or "").strip().lower()
        if status and status in terminal:
            continue
        active += 1
    return active


class IngressAbuseMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        policy: IngressPolicy,
        admission_backend: AdmissionBackend | None = None,
        context_resolver: Callable[[Request], RequestContext | None] | None = None,
        active_search_counter: Callable[[Request, RequestContext, int], int] | None = None,
        admission_store: IngressAdmissionStore | None = None,
    ) -> None:
        self.app = app
        self.policy = policy
        if policy.quotas_enabled and admission_backend is None:
            raise RuntimeError("propertyquarry_ingress_admission_backend_required")
        self._admission_backend = admission_backend
        self._context_resolver = context_resolver or _request_context_sync
        self._active_search_counter = active_search_counter

    def _record_admission(self, scope: dict[str, Any], *, operation: str, outcome: str) -> None:
        app = scope.get("app")
        if app is None:
            return
        backend_name = str(getattr(self._admission_backend, "backend_name", "missing") or "missing")
        get_runtime_metrics(app).record_ingress_admission(
            backend=backend_name,
            operation=operation,
            outcome=outcome,
        )

    async def _consume_quota(
        self,
        *,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
        charges: tuple[QuotaCharge, ...],
    ) -> AdmissionDecision | None:
        backend = self._admission_backend
        if backend is None:
            raise RuntimeError("propertyquarry_ingress_admission_backend_required")
        try:
            decision = await asyncio.to_thread(backend.consume_many, charges)
        except AdmissionBackendUnavailable:
            self._record_admission(scope, operation="quota", outcome="unavailable")
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=503,
                code="ingress_admission_unavailable",
                message="request admission could not be verified",
                dimension="backend",
                retry_after=5,
            )
            return None
        self._record_admission(
            scope,
            operation="quota",
            outcome="allowed" if decision.allowed else "rejected",
        )
        return decision

    async def _acquire_concurrency(
        self,
        *,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
        dimensions: tuple[ConcurrencyDimension, ...],
    ) -> AdmissionDecision | None:
        backend = self._admission_backend
        if backend is None:
            raise RuntimeError("propertyquarry_ingress_admission_backend_required")
        try:
            decision = await asyncio.to_thread(
                backend.acquire,
                dimensions,
                lease_seconds=self.policy.concurrency_lease_seconds,
            )
        except AdmissionBackendUnavailable:
            self._record_admission(scope, operation="concurrency", outcome="unavailable")
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=503,
                code="ingress_admission_unavailable",
                message="request admission could not be verified",
                dimension="backend",
                retry_after=5,
            )
            return None
        self._record_admission(
            scope,
            operation="concurrency",
            outcome="allowed" if decision.allowed else "rejected",
        )
        return decision

    async def _send_error(
        self,
        *,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
        status_code: int,
        code: str,
        message: str,
        dimension: str,
        retry_after: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        state = scope.setdefault("state", {})
        correlation_id = str(state.get("correlation_id") or uuid.uuid4())
        state["correlation_id"] = correlation_id
        error_details = dict(details or {})
        if retry_after is not None:
            error_details.setdefault("retry_after_seconds", max(1, int(retry_after)))
        response = JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": error_details,
                    "correlation_id": correlation_id,
                }
            },
        )
        response.headers["Cache-Control"] = "no-store"
        if retry_after is not None:
            response.headers["Retry-After"] = str(max(1, int(retry_after)))
        app = scope.get("app")
        if app is not None:
            get_runtime_metrics(app).record_ingress_rejection(reason=code, dimension=dimension)
        await response(scope, receive, send)

    def _admission_backend(self) -> AdmissionBackend:
        if isinstance(self._admission_store, InMemoryIngressAdmissionStore):
            return AdmissionBackend.MEMORY
        return AdmissionBackend.POSTGRES

    @staticmethod
    def _record_admission_result(
        scope: dict[str, Any],
        result: AdmissionResult,
    ) -> None:
        app = scope.get("app")
        if app is None:
            return
        get_runtime_metrics(app).record_ingress_admission_operation(
            backend=result.backend.value,
            operation=result.operation.value,
            outcome=result.outcome.value,
        )

    @staticmethod
    def _record_admission_error(
        scope: dict[str, Any],
        exc: IngressAdmissionError,
    ) -> None:
        app = scope.get("app")
        if app is None:
            return
        get_runtime_metrics(app).record_ingress_admission_operation(
            backend=exc.backend.value,
            operation=exc.operation.value,
            outcome=exc.outcome.value,
        )

    def _record_unexpected_admission_error(
        self,
        scope: dict[str, Any],
        *,
        operation: AdmissionOperation,
    ) -> None:
        app = scope.get("app")
        if app is None:
            return
        get_runtime_metrics(app).record_ingress_admission_operation(
            backend=self._admission_backend().value,
            operation=operation.value,
            outcome=AdmissionOutcome.BACKEND_UNAVAILABLE.value,
        )

    async def _renew_lease(
        self,
        *,
        scope: dict[str, Any],
        lease_token: str,
    ) -> None:
        renewal_interval = max(
            0.25,
            min(float(self.policy.lease_seconds) / 3.0, 10.0),
        )
        while True:
            await asyncio.sleep(renewal_interval)
            try:
                renewed = await asyncio.to_thread(
                    self._admission_store.renew_lease,
                    lease_token,
                    lease_seconds=self.policy.lease_seconds,
                )
            except IngressAdmissionError as exc:
                self._record_admission_error(scope, exc)
                raise IngressLeaseLost("ingress_admission_lease_renewal_failed") from exc
            except Exception as exc:
                self._record_unexpected_admission_error(
                    scope,
                    operation=AdmissionOperation.RENEW,
                )
                raise IngressLeaseLost("ingress_admission_lease_renewal_failed") from exc
            app = scope.get("app")
            if app is not None:
                get_runtime_metrics(app).record_ingress_admission_operation(
                    backend=self._admission_backend().value,
                    operation=AdmissionOperation.RENEW.value,
                    outcome=(
                        AdmissionOutcome.ALLOWED.value
                        if renewed
                        else AdmissionOutcome.LEASE_LIMITED.value
                    ),
                )
            if not renewed:
                raise IngressLeaseLost("ingress_admission_lease_lost")

    async def _run_with_lease(
        self,
        *,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
        lease_token: str,
        run_protected: Callable[
            [Callable[..., Awaitable[None]]],
            Awaitable[None],
        ],
    ) -> None:
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        downstream = asyncio.create_task(
            run_protected(tracked_send),
            name="propertyquarry-ingress-downstream",
        )
        renewal = asyncio.create_task(
            self._renew_lease(scope=scope, lease_token=lease_token),
            name="propertyquarry-ingress-lease-renewal",
        )
        try:
            done, _pending = await asyncio.wait(
                {downstream, renewal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal in done:
                renewal_error = renewal.exception()
                if renewal_error is not None:
                    downstream.cancel()
                    # Downstream cancellation/cleanup must not mask the
                    # authoritative lease-loss failure.
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(downstream, timeout=1.0)
                    if response_started:
                        raise renewal_error
                    await self._send_error(
                        scope=scope,
                        receive=receive,
                        send=send,
                        status_code=503,
                        code="ingress_admission_lease_lost",
                        message="distributed request capacity lease was lost",
                        dimension="lease",
                        retry_after=1,
                    )
                    return
            await downstream
        finally:
            for task in (renewal, downstream):
                if not task.done():
                    task.cancel()
            await asyncio.gather(renewal, downstream, return_exceptions=True)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").strip().upper()
        path = str(scope.get("path") or "/").strip() or "/"
        normalized_path = path.rstrip("/") or "/"
        headers = _headers_from_scope(scope)
        rule = _route_rule(method, normalized_path, policy=self.policy)
        body_limit = int(rule.max_body_bytes if rule and rule.max_body_bytes else self.policy.max_body_bytes)
        raw_content_length = str(headers.get("content-length") or "").strip()
        content_length = 0
        if raw_content_length:
            if (
                not raw_content_length.isascii()
                or not raw_content_length.isdecimal()
                or len(raw_content_length) > 20
                or (
                    raw_content_length != "0"
                    and raw_content_length.startswith("0")
                )
            ):
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="invalid_content_length",
                    message="request Content-Length is invalid",
                    dimension="body",
                )
                return
            content_length = int(raw_content_length)
        elif (
            self.policy.quotas_enabled
            and rule is not None
            and method in {"POST", "PUT", "PATCH"}
        ):
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=411,
                code="content_length_required",
                message="Content-Length is required for metered request bodies",
                dimension="body",
            )
            return
        if (
            self.policy.quotas_enabled
            and rule is not None
            and headers.get("transfer-encoding")
        ):
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=400,
                code="request_transfer_encoding_unsupported",
                message="transfer-encoded request bodies are not accepted",
                dimension="body",
            )
            return
        if content_length > body_limit:
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=413,
                code="request_body_too_large",
                message="request body exceeds the configured limit",
                dimension="body",
                details={"max_body_bytes": body_limit},
            )
            return
        if headers.get("content-encoding", "").lower() not in {"", "identity"}:
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=415,
                code="request_content_encoding_unsupported",
                message="compressed request bodies are not accepted",
                dimension="body",
            )
            return

        received_bytes = 0
        replay_body: bytes | None = None

        async def validated_receive() -> dict[str, Any]:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body") or b"")
                if received_bytes > body_limit:
                    raise RequestBodyLimitExceeded("request_body_too_large")
                if (
                    self.policy.quotas_enabled
                    and raw_content_length
                    and received_bytes > content_length
                ):
                    raise RequestBodyLengthMismatch(
                        "request_body_length_mismatch"
                    )
            return message

        async def limited_receive() -> dict[str, Any]:
            nonlocal replay_body
            if replay_body is not None:
                body = replay_body
                replay_body = None
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await validated_receive()

        bypass = method == "OPTIONS" or normalized_path in _BYPASS_PATHS
        if not self.policy.quotas_enabled or bypass:
            try:
                await self.app(scope, limited_receive, send)
            except RequestBodyLimitExceeded:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=413,
                    code="request_body_too_large",
                    message="request body exceeds the configured limit",
                    dimension="body",
                    details={"max_body_bytes": body_limit},
                )
            except RequestBodyLengthMismatch:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="request_body_length_mismatch",
                    message="request body does not match its declared Content-Length",
                    dimension="body",
                )
            return

        peer = (scope.get("client") or ("", 0))[0]
        client_ip = resolve_client_ip(
            peer_host=peer,
            headers=headers,
            trusted_proxy_cidrs=self.policy.trusted_proxy_cidrs,
        )
        scope.setdefault("state", {})["client_ip"] = client_ip
        quota_decision = await self._consume_quota(
            scope=scope,
            receive=receive,
            send=send,
            charges=(
                QuotaCharge(
                    key=f"ingress:request:ip:{client_ip}",
                    units=1,
                    limit=self.policy.ip_request_limit,
                    window_seconds=self.policy.window_seconds,
                    dimension="ip",
                ),
            ),
        )
        if quota_decision is None:
            return
        if not quota_decision.allowed:
            await self._send_error(
                scope=scope,
                receive=receive,
                send=send,
                status_code=429,
                code="ingress_rate_limit_exceeded",
                message="request rate limit exceeded",
                dimension="ip",
                retry_after=quota_decision.retry_after_seconds,
            )
            return

        if rule is not None and method in _MUTATION_METHODS:
            try:
                body_chunks: list[bytes] = []
                while True:
                    message = await validated_receive()
                    if message.get("type") != "http.request":
                        raise RequestBodyLengthMismatch(
                            "request_body_length_mismatch"
                        )
                    body_chunks.append(bytes(message.get("body") or b""))
                    if not message.get("more_body"):
                        break
                if received_bytes != content_length:
                    raise RequestBodyLengthMismatch(
                        "request_body_length_mismatch"
                    )
                replay_body = b"".join(body_chunks)
            except RequestBodyLimitExceeded:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=413,
                    code="request_body_too_large",
                    message="request body exceeds the configured limit",
                    dimension="body",
                    details={"max_body_bytes": body_limit},
                )
                return
            except RequestBodyLengthMismatch:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="request_body_length_mismatch",
                    message="request body does not match its declared Content-Length",
                    dimension="body",
                )
                return

        context: RequestContext | None = None
        account_subject = ""
        if rule is not None:
            request = Request(scope, receive=limited_receive)
            try:
                context = await asyncio.to_thread(self._context_resolver, request)
            except HTTPException:
                # Let the normal authentication dependency produce its canonical
                # response while retaining the anonymous/IP ingress controls.
                context = None
            except Exception:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=503,
                    code="ingress_identity_check_unavailable",
                    message="request identity could not be checked",
                    dimension="account",
                    retry_after=5,
                )
                return
            account_key = _hashed_account_key(context)
            if account_key:
                quota_decision = await self._consume_quota(
                    scope=scope,
                    receive=receive,
                    send=send,
                    charges=(
                        QuotaCharge(
                            key=f"ingress:request:account:{account_key}",
                            units=1,
                            limit=self.policy.account_request_limit,
                            window_seconds=self.policy.window_seconds,
                            dimension="account",
                        ),
                    ),
                )
                if quota_decision is None:
                    return
                if not quota_decision.allowed:
                    await self._send_error(
                        scope=scope,
                        receive=receive,
                        send=send,
                        status_code=429,
                        code="ingress_rate_limit_exceeded",
                        message="account request rate limit exceeded",
                        dimension="account",
                        retry_after=quota_decision.retry_after_seconds,
                    )
                    return

        cost_units = int(rule.cost_units if rule is not None else 0)
        if content_length > 0:
            cost_units += max(0, int(math.ceil(content_length / 262_144)) - 1)
        if cost_units > 0:
            cost_charges = [
                QuotaCharge(
                    key=f"ingress:cost:ip:{client_ip}",
                    units=cost_units,
                    limit=self.policy.ip_cost_limit,
                    window_seconds=self.policy.window_seconds,
                    dimension="ip",
                )
            ]
            if account_key:
                cost_charges.append(
                    QuotaCharge(
                        key=f"ingress:cost:account:{account_key}",
                        units=cost_units,
                        limit=self.policy.account_cost_limit,
                        window_seconds=self.policy.window_seconds,
                        dimension="account",
                    )
                )
            quota_decision = await self._consume_quota(
                scope=scope,
                receive=receive,
                send=send,
                charges=tuple(cost_charges),
            )
            if quota_decision is None:
                return
            if not quota_decision.allowed:
                dimension = quota_decision.dimension or "ip"
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=429,
                    code="ingress_cost_quota_exceeded",
                    message=(
                        "account request cost quota exceeded"
                        if dimension == "account"
                        else "request cost quota exceeded"
                    ),
                    dimension=dimension,
                    retry_after=quota_decision.retry_after_seconds,
                )
                return
            app = scope.get("app")
            if app is not None:
                get_runtime_metrics(app).record_ingress_cost(
                    route_class=rule.name if rule is not None else "mutation",
                    cost_units=cost_units,
                )

        lease_id = ""
        if rule is not None and rule.high_cost:
            account_concurrency = (
                1
                if rule.active_search
                else self.policy.high_cost_account_concurrency
            )
            concurrency_dimensions = [
                ConcurrencyDimension(
                    key=f"ingress:concurrency:ip:{client_ip}",
                    limit=self.policy.high_cost_ip_concurrency,
                    dimension="ip",
                )
            ]
            if account_key:
                concurrency_dimensions.append(
                    ConcurrencyDimension(
                        key=f"ingress:concurrency:account:{account_key}",
                        limit=account_concurrency,
                        dimension="account",
                    )
                )
            concurrency_decision = await self._acquire_concurrency(
                scope=scope,
                receive=receive,
                send=send,
                dimensions=tuple(concurrency_dimensions),
            )
            if concurrency_decision is None:
                return
            if not concurrency_decision.allowed:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=429,
                    code="ingress_concurrency_limit_exceeded",
                    message="too many high-cost requests are already active",
                    dimension=concurrency_decision.dimension or "ip",
                    retry_after=1,
                )
                return
            lease_id = concurrency_decision.lease_id
            app = scope.get("app")
            if app is not None:
                get_runtime_metrics(app).adjust_ingress_inflight(
                    route_class=rule.name,
                    delta=1,
                )

        async def run_after_admission(
            response_send: Callable[..., Awaitable[None]],
        ) -> None:
            if (
                rule is not None
                and rule.active_search
                and context is not None
                and account_subject
            ):
                request_for_active_check = Request(scope, receive=limited_receive)
                try:
                    if self._active_search_counter is not None:
                        active_count = await asyncio.to_thread(
                            self._active_search_counter,
                            request_for_active_check,
                            context,
                            self.policy.active_search_limit,
                        )
                    else:
                        active_count = await asyncio.to_thread(
                            _active_property_search_count_sync,
                            request_for_active_check,
                            context,
                            limit=self.policy.active_search_limit,
                        )
                except Exception:
                    await self._send_error(
                        scope=scope,
                        receive=receive,
                        send=response_send,
                        status_code=503,
                        code="active_search_check_unavailable",
                        message="active search capacity could not be checked",
                        dimension="account",
                        retry_after=5,
                    )
                    return
                if active_count >= self.policy.active_search_limit:
                    await self._send_error(
                        scope=scope,
                        receive=receive,
                        send=response_send,
                        status_code=429,
                        code="active_search_limit_exceeded",
                        message="an active property search is already running",
                        dimension="account",
                        retry_after=max(5, self.policy.window_seconds),
                        details={"active_search_limit": self.policy.active_search_limit},
                    )
                    return
            await self.app(scope, limited_receive, response_send)

        try:
            try:
                if lease_token:
                    await self._run_with_lease(
                        scope=scope,
                        receive=limited_receive,
                        send=send,
                        lease_token=lease_token,
                        run_protected=run_after_admission,
                    )
                else:
                    await run_after_admission(send)
            except RequestBodyLimitExceeded:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=413,
                    code="request_body_too_large",
                    message="request body exceeds the configured limit",
                    dimension="body",
                    details={"max_body_bytes": body_limit},
                )
            except RequestBodyLengthMismatch:
                await self._send_error(
                    scope=scope,
                    receive=receive,
                    send=send,
                    status_code=400,
                    code="request_body_length_mismatch",
                    message="request body does not match its declared Content-Length",
                    dimension="body",
                )
        finally:
            if lease_id and rule is not None:
                backend = self._admission_backend
                try:
                    if backend is not None:
                        await asyncio.to_thread(backend.release, lease_id)
                    self._record_admission(scope, operation="release", outcome="released")
                except Exception:
                    # A bounded lease prevents a crashed or partitioned release
                    # from permanently consuming distributed capacity.
                    LOG.exception("distributed ingress admission lease release failed")
                    self._record_admission(scope, operation="release", outcome="unavailable")
                app = scope.get("app")
                if app is not None and rule is not None:
                    get_runtime_metrics(app).adjust_ingress_inflight(
                        route_class=rule.name,
                        delta=-1,
                    )
