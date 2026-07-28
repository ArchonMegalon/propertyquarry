from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from typing import Iterator, Mapping, Sequence

from app.services.admission_control import (
    ADMISSION_LEASE_ROW_LIMIT,
    ADMISSION_QUOTA_ROW_LIMIT,
)


_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_KNOWN_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_MAX_HEARTBEAT_BYTES = 64 * 1024
_MAX_PROPERTY_SEARCH_QUEUE_DEPTH = (2**63) - 1
_MAX_PROPERTY_SEARCH_QUEUE_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
_DELIVERY_OUTBOX_METRIC_OUTCOMES = (
    "queued",
    "claimed",
    "claim_conflicts",
    "sent",
    "retried",
    "dead_lettered",
    "failed",
)
_CONTENT_LEDGER_METRIC_OUTCOMES = (
    "claimed",
    "recovered",
    "duplicate",
    "replay_conflict",
    "completed",
    "failed",
    "corruption",
)
_INGRESS_LABEL_RE = re.compile(r"[^a-z0-9_]+")
_INGRESS_ADMISSION_BACKENDS = ("memory", "postgres")
_INGRESS_ADMISSION_OPERATIONS = (
    "ip_request",
    "admit",
    "renew",
    "release",
    "cleanup",
    "snapshot",
)
_INGRESS_ADMISSION_OUTCOMES = (
    "allowed",
    "quota_limited",
    "lease_limited",
    "capacity_exhausted",
    "backend_unavailable",
)
_INGRESS_ADMISSION_CAPACITY_LIMITS = {
    "lease": 100_000,
    "quota": 1_000_000,
}
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_TRACE_FLAGS_RE = re.compile(r"^[0-9a-f]{2}$")
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _label_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return format(float(value), ".9g")


def _ingress_label(value: str, *, fallback: str) -> str:
    normalized = _INGRESS_LABEL_RE.sub("_", str(value or "").strip().lower()).strip("_")
    return (normalized or fallback)[:64]


def route_template(request) -> str:  # type: ignore[no-untyped-def]
    route = request.scope.get("route")
    path = str(getattr(route, "path", "") or "").strip()
    return path if path.startswith("/") else "unmatched"


def normalized_method(value: str) -> str:
    method = str(value or "").strip().upper()
    return method if method in _KNOWN_METHODS else "OTHER"


def runtime_build_identity(
    environ: Mapping[str, str] | None = None,
    *,
    hostname: str | None = None,
) -> dict[str, str]:
    """Return the process-observed release and Docker replica identity.

    The capture lane independently checks these values against Docker inspect;
    they are runtime claims, not launch authority by themselves.
    """

    env = environ if environ is not None else os.environ
    commit_sha = str(env.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip().lower()
    image_digest = str(env.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST") or "").strip().lower()
    replica_id = str(hostname if hostname is not None else socket.gethostname()).strip()
    return {
        "release_commit_sha": commit_sha if _GIT_SHA_RE.fullmatch(commit_sha) else "",
        "release_image_digest": (
            image_digest if _IMAGE_DIGEST_RE.fullmatch(image_digest) else ""
        ),
        "replica_id": replica_id if _REPLICA_ID_RE.fullmatch(replica_id) else "",
    }


@dataclass(frozen=True)
class RuntimeTraceContext:
    """A bounded W3C trace context used for logs and service boundaries.

    This is propagation and correlation infrastructure, not proof that a trace
    backend received a span.  The protected launch gate separately requires a
    fresh query receipt from the configured observability authority.
    """

    trace_id: str
    span_id: str
    trace_flags: str
    parent_span_id: str = ""
    source: str = "generated"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def as_mapping(self) -> dict[str, str]:
        return {
            "traceparent": self.traceparent,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_flags": self.trace_flags,
            "source": self.source,
        }


_RUNTIME_TRACE_CONTEXT: ContextVar[RuntimeTraceContext | None] = ContextVar(
    "propertyquarry_runtime_trace_context",
    default=None,
)
_RUNTIME_CORRELATION_ID: ContextVar[str] = ContextVar(
    "propertyquarry_runtime_correlation_id",
    default="",
)


def _random_nonzero_hex(byte_count: int) -> str:
    for _attempt in range(4):
        candidate = secrets.token_hex(byte_count)
        if candidate and set(candidate) != {"0"}:
            return candidate
    # A cryptographically secure generator returning all-zero bytes four times
    # is not a usable tracing runtime. Fail rather than emit an invalid ID.
    raise RuntimeError("propertyquarry_trace_identifier_generation_failed")


def parse_traceparent(value: object) -> tuple[str, str, str] | None:
    """Parse the supported W3C traceparent v00 shape, or return ``None``.

    Invalid or unsupported caller input is ignored by the middleware and a new
    local trace is created. It is never reflected or written to logs verbatim.
    """

    raw = str(value or "").strip()
    if len(raw) != 55 or raw != raw.lower():
        return None
    parts = raw.split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id, parent_span_id, trace_flags = parts[1:]
    if (
        not _TRACE_ID_RE.fullmatch(trace_id)
        or trace_id == _ZERO_TRACE_ID
        or not _SPAN_ID_RE.fullmatch(parent_span_id)
        or parent_span_id == _ZERO_SPAN_ID
        or not _TRACE_FLAGS_RE.fullmatch(trace_flags)
    ):
        return None
    return trace_id, parent_span_id, trace_flags


def new_server_trace_context(traceparent: object = "") -> RuntimeTraceContext:
    parsed = parse_traceparent(traceparent)
    if parsed is None:
        return RuntimeTraceContext(
            trace_id=_random_nonzero_hex(16),
            span_id=_random_nonzero_hex(8),
            trace_flags="00",
            source="generated",
        )
    trace_id, parent_span_id, trace_flags = parsed
    return RuntimeTraceContext(
        trace_id=trace_id,
        span_id=_random_nonzero_hex(8),
        parent_span_id=parent_span_id,
        trace_flags=trace_flags,
        source="incoming",
    )


def child_trace_context(parent: RuntimeTraceContext) -> RuntimeTraceContext:
    return RuntimeTraceContext(
        trace_id=parent.trace_id,
        span_id=_random_nonzero_hex(8),
        parent_span_id=parent.span_id,
        trace_flags=parent.trace_flags,
        source="internal",
    )


def runtime_trace_context_from_mapping(value: object) -> RuntimeTraceContext | None:
    if not isinstance(value, dict):
        return None
    parsed = parse_traceparent(value.get("traceparent"))
    if parsed is None:
        return None
    trace_id, span_id, trace_flags = parsed
    if value.get("trace_id") not in {None, trace_id}:
        return None
    if value.get("span_id") not in {None, span_id}:
        return None
    return RuntimeTraceContext(
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=trace_flags,
        parent_span_id=(
            str(value.get("parent_span_id") or "")
            if _SPAN_ID_RE.fullmatch(str(value.get("parent_span_id") or ""))
            else ""
        ),
        source="boundary",
    )


@contextmanager
def bind_runtime_trace_context(
    trace_context: RuntimeTraceContext | None,
    *,
    correlation_id: str = "",
) -> Iterator[RuntimeTraceContext | None]:
    trace_token = _RUNTIME_TRACE_CONTEXT.set(trace_context)
    normalized_correlation = bounded_correlation_id(correlation_id, generate=False)
    correlation_token = _RUNTIME_CORRELATION_ID.set(normalized_correlation)
    try:
        yield trace_context
    finally:
        _RUNTIME_CORRELATION_ID.reset(correlation_token)
        _RUNTIME_TRACE_CONTEXT.reset(trace_token)


def current_runtime_trace_context() -> RuntimeTraceContext | None:
    return _RUNTIME_TRACE_CONTEXT.get()


def bounded_correlation_id(value: object, *, generate: bool = True) -> str:
    normalized = str(value or "").strip()
    if _CORRELATION_ID_RE.fullmatch(normalized):
        return normalized
    return _random_nonzero_hex(16) if generate else ""


def outbound_observability_headers(
    *,
    correlation_id: str = "",
    trace_context: RuntimeTraceContext | None = None,
) -> dict[str, str]:
    context = trace_context or current_runtime_trace_context()
    headers: dict[str, str] = {}
    if context is not None:
        headers["traceparent"] = context.traceparent
    normalized_correlation = bounded_correlation_id(
        correlation_id or _RUNTIME_CORRELATION_ID.get(),
        generate=False,
    )
    if normalized_correlation:
        headers["x-correlation-id"] = normalized_correlation
    return headers


def submit_with_runtime_context(executor, function, /, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Submit thread work with an isolated copy of the current trace context."""

    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


class RuntimeMetrics:
    """Small bounded-label Prometheus registry for one API process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._errors: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._duration_buckets: dict[tuple[str, str, float], int] = defaultdict(int)
        self._ingress_rejections: dict[tuple[str, str], int] = defaultdict(int)
        self._ingress_cost: dict[str, int] = defaultdict(int)
        self._ingress_inflight: dict[str, int] = defaultdict(int)
        self._ingress_admission: dict[tuple[str, str, str], int] = defaultdict(int)
        self._ingress_admission_capacity_backend = ""
        self._ingress_admission_capacity_contract_valid: bool | None = None
        self._ingress_admission_capacity_rows: dict[str, tuple[int, int]] = {}
        self._content_ledger_events: dict[str, int] = defaultdict(int)

    def record_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        safe_method = normalized_method(method)
        safe_route = route if str(route or "").startswith("/") else "unmatched"
        status = int(status_code or 500)
        status_class = f"{max(0, min(9, status // 100))}xx"
        duration = max(0.0, float(duration_seconds or 0.0))
        request_key = (safe_method, safe_route, status_class)
        duration_key = (safe_method, safe_route)
        with self._lock:
            self._requests[request_key] += 1
            if status >= 400:
                self._errors[request_key] += 1
            self._duration_count[duration_key] += 1
            self._duration_sum[duration_key] += duration
            for bucket in _LATENCY_BUCKETS:
                if duration <= bucket:
                    self._duration_buckets[(safe_method, safe_route, bucket)] += 1

    def record_ingress_rejection(self, *, reason: str, dimension: str) -> None:
        safe_reason = _ingress_label(reason, fallback="unknown")
        safe_dimension = _ingress_label(dimension, fallback="unknown")
        with self._lock:
            self._ingress_rejections[(safe_reason, safe_dimension)] += 1

    def record_ingress_cost(self, *, route_class: str, cost_units: int) -> None:
        safe_route_class = _ingress_label(route_class, fallback="other")
        with self._lock:
            self._ingress_cost[safe_route_class] += max(0, int(cost_units or 0))

    def adjust_ingress_inflight(self, *, route_class: str, delta: int) -> None:
        safe_route_class = _ingress_label(route_class, fallback="other")
        with self._lock:
            updated = self._ingress_inflight.get(safe_route_class, 0) + int(delta or 0)
            self._ingress_inflight[safe_route_class] = max(0, updated)

    def record_ingress_admission(
        self,
        *,
        backend: str,
        operation: str,
        outcome: str,
    ) -> None:
        safe_backend = _ingress_label(backend, fallback="unknown")
        safe_operation = _ingress_label(operation, fallback="unknown")
        safe_outcome = _ingress_label(outcome, fallback="unknown")
        with self._lock:
            self._ingress_admission[(safe_backend, safe_operation, safe_outcome)] += 1

    def record_ingress_admission_operation(
        self,
        *,
        backend: str,
        operation: str,
        outcome: str,
    ) -> None:
        normalized_backend = str(backend or "").strip().lower()
        normalized_operation = str(operation or "").strip().lower()
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_backend not in _INGRESS_ADMISSION_BACKENDS:
            raise ValueError("ingress_admission_metric_backend_invalid")
        if normalized_operation not in _INGRESS_ADMISSION_OPERATIONS:
            raise ValueError("ingress_admission_metric_operation_invalid")
        if normalized_outcome not in _INGRESS_ADMISSION_OUTCOMES:
            raise ValueError("ingress_admission_metric_outcome_invalid")
        self.record_ingress_admission(
            backend=normalized_backend,
            operation=normalized_operation,
            outcome=normalized_outcome,
        )

    def record_ingress_admission_capacity(
        self,
        *,
        backend: str,
        contract_valid: bool,
        rows: Mapping[str, tuple[int, int]],
    ) -> None:
        normalized_backend = str(backend or "").strip().lower()
        if normalized_backend not in _INGRESS_ADMISSION_BACKENDS:
            raise ValueError("ingress_admission_metric_backend_invalid")
        if not isinstance(contract_valid, bool):
            raise ValueError("ingress_admission_metric_contract_valid_invalid")
        normalized_rows: dict[str, tuple[int, int]] = {}
        for raw_key, raw_values in dict(rows).items():
            capacity_key = str(raw_key or "").strip().lower()
            if capacity_key not in _INGRESS_ADMISSION_CAPACITY_LIMITS:
                raise ValueError("ingress_admission_metric_capacity_key_invalid")
            if (
                not isinstance(raw_values, tuple)
                or len(raw_values) != 2
                or isinstance(raw_values[0], bool)
                or isinstance(raw_values[1], bool)
            ):
                raise ValueError("ingress_admission_metric_capacity_row_invalid")
            row_count = int(raw_values[0])
            hard_limit = int(raw_values[1])
            expected_limit = _INGRESS_ADMISSION_CAPACITY_LIMITS[capacity_key]
            if (
                row_count < 0
                or hard_limit < 1
                or row_count > hard_limit
                or (
                    normalized_backend == "postgres"
                    and hard_limit != expected_limit
                )
                or (
                    normalized_backend == "memory"
                    and hard_limit > expected_limit
                )
            ):
                raise ValueError("ingress_admission_metric_capacity_row_invalid")
            normalized_rows[capacity_key] = (row_count, hard_limit)
        if contract_valid and set(normalized_rows) != set(
            _INGRESS_ADMISSION_CAPACITY_LIMITS
        ):
            raise ValueError("ingress_admission_metric_capacity_contract_incomplete")
        with self._lock:
            self._ingress_admission_capacity_backend = normalized_backend
            self._ingress_admission_capacity_contract_valid = contract_valid
            self._ingress_admission_capacity_rows = normalized_rows

    def record_content_ledger_event(self, *, outcome: str) -> None:
        safe_outcome = _ingress_label(outcome, fallback="failed")
        if safe_outcome not in _CONTENT_LEDGER_METRIC_OUTCOMES:
            safe_outcome = "failed"
        with self._lock:
            self._content_ledger_events[safe_outcome] += 1

    def render_prometheus(
        self,
        *,
        readiness_ready: bool,
        admission_backend: str = "unknown",
        admission_capacity_rows: Sequence[tuple[str, int, int]] = (),
        admission_capacity_valid: bool = False,
        environ: Mapping[str, str] | None = None,
        now_epoch: float | None = None,
    ) -> str:
        with self._lock:
            requests = dict(self._requests)
            errors = dict(self._errors)
            duration_count = dict(self._duration_count)
            duration_sum = dict(self._duration_sum)
            duration_buckets = dict(self._duration_buckets)
            ingress_rejections = dict(self._ingress_rejections)
            ingress_cost = dict(self._ingress_cost)
            ingress_inflight = dict(self._ingress_inflight)
            ingress_admission = dict(self._ingress_admission)
            ingress_admission_capacity_backend = (
                self._ingress_admission_capacity_backend
            )
            ingress_admission_capacity_contract_valid = (
                self._ingress_admission_capacity_contract_valid
            )
            ingress_admission_capacity_rows = dict(
                self._ingress_admission_capacity_rows
            )
            content_ledger_events = dict(self._content_ledger_events)
        from app.telemetry import span_export_health_snapshot

        span_export_health = span_export_health_snapshot()

        lines = [
            "# HELP propertyquarry_http_requests_total HTTP requests completed by bounded route template.",
            "# TYPE propertyquarry_http_requests_total counter",
        ]
        for (method, route, status_class), count in sorted(requests.items()):
            lines.append(
                'propertyquarry_http_requests_total{method="%s",route="%s",status_class="%s"} %d'
                % (_label_value(method), _label_value(route), _label_value(status_class), count)
            )
        lines.extend(
            [
                "# HELP propertyquarry_http_request_errors_total HTTP responses with status 4xx or 5xx.",
                "# TYPE propertyquarry_http_request_errors_total counter",
            ]
        )
        for (method, route, status_class), count in sorted(errors.items()):
            lines.append(
                'propertyquarry_http_request_errors_total{method="%s",route="%s",status_class="%s"} %d'
                % (_label_value(method), _label_value(route), _label_value(status_class), count)
            )
        lines.extend(
            [
                "# HELP propertyquarry_http_request_duration_seconds HTTP request latency by bounded route template.",
                "# TYPE propertyquarry_http_request_duration_seconds histogram",
            ]
        )
        for method, route in sorted(duration_count):
            for bucket in _LATENCY_BUCKETS:
                count = duration_buckets.get((method, route, bucket), 0)
                lines.append(
                    'propertyquarry_http_request_duration_seconds_bucket{method="%s",route="%s",le="%s"} %d'
                    % (_label_value(method), _label_value(route), _metric_float(bucket), count)
                )
            count = duration_count[(method, route)]
            lines.append(
                'propertyquarry_http_request_duration_seconds_bucket{method="%s",route="%s",le="+Inf"} %d'
                % (_label_value(method), _label_value(route), count)
            )
            lines.append(
                'propertyquarry_http_request_duration_seconds_sum{method="%s",route="%s"} %s'
                % (_label_value(method), _label_value(route), _metric_float(duration_sum[(method, route)]))
            )
            lines.append(
                'propertyquarry_http_request_duration_seconds_count{method="%s",route="%s"} %d'
                % (_label_value(method), _label_value(route), count)
            )

        lines.extend(
            [
                "# HELP propertyquarry_ingress_rejections_total Requests rejected by bounded ingress abuse controls.",
                "# TYPE propertyquarry_ingress_rejections_total counter",
            ]
        )
        for (reason, dimension), count in sorted(ingress_rejections.items()):
            lines.append(
                'propertyquarry_ingress_rejections_total{reason="%s",dimension="%s"} %d'
                % (_label_value(reason), _label_value(dimension), count)
            )
        lines.extend(
            [
                "# HELP propertyquarry_ingress_cost_units_total Admitted ingress cost units by bounded route class.",
                "# TYPE propertyquarry_ingress_cost_units_total counter",
            ]
        )
        for route_class, cost_units in sorted(ingress_cost.items()):
            lines.append(
                'propertyquarry_ingress_cost_units_total{route_class="%s"} %d'
                % (_label_value(route_class), cost_units)
            )
        lines.extend(
            [
                "# HELP propertyquarry_ingress_high_cost_inflight High-cost requests currently admitted in this API process.",
                "# TYPE propertyquarry_ingress_high_cost_inflight gauge",
            ]
        )
        for route_class, count in sorted(ingress_inflight.items()):
            lines.append(
                'propertyquarry_ingress_high_cost_inflight{route_class="%s"} %d'
                % (_label_value(route_class), count)
            )
        lines.extend(
            [
                "# HELP propertyquarry_ingress_admission_operations_total Distributed admission backend outcomes.",
                "# TYPE propertyquarry_ingress_admission_operations_total counter",
            ]
        )
        admission_backends = {
            backend
            for backend, _operation, _outcome in ingress_admission
            if backend in _INGRESS_ADMISSION_BACKENDS
        }
        if ingress_admission_capacity_backend:
            admission_backends.add(ingress_admission_capacity_backend)
        for backend in sorted(admission_backends):
            for operation in _INGRESS_ADMISSION_OPERATIONS:
                for outcome in _INGRESS_ADMISSION_OUTCOMES:
                    count = ingress_admission.get(
                        (backend, operation, outcome),
                        0,
                    )
                    lines.append(
                        'propertyquarry_ingress_admission_operations_total{backend="%s",operation="%s",outcome="%s"} %d'
                        % (
                            _label_value(backend),
                            _label_value(operation),
                            _label_value(outcome),
                            count,
                        )
                    )
        for (backend, operation, outcome), count in sorted(ingress_admission.items()):
            if (
                backend in _INGRESS_ADMISSION_BACKENDS
                and operation in _INGRESS_ADMISSION_OPERATIONS
                and outcome in _INGRESS_ADMISSION_OUTCOMES
            ):
                continue
            lines.append(
                'propertyquarry_ingress_admission_operations_total{backend="%s",operation="%s",outcome="%s"} %d'
                % (
                    _label_value(backend),
                    _label_value(operation),
                    _label_value(outcome),
                    count,
                )
            )

        if ingress_admission_capacity_backend:
            admission_backend = ingress_admission_capacity_backend
            admission_capacity_rows = tuple(
                (capacity_key, row_count, hard_limit)
                for capacity_key, (row_count, hard_limit) in sorted(
                    ingress_admission_capacity_rows.items()
                )
            )
            admission_capacity_valid = bool(
                ingress_admission_capacity_contract_valid
            )
        safe_capacity_backend = _ingress_label(
            admission_backend,
            fallback="unknown",
        )
        capacity_rows: list[tuple[str, int, int]] = []
        for row in admission_capacity_rows:
            if len(row) != 3:
                continue
            capacity_key, row_count, row_limit = row
            safe_capacity_key = _ingress_label(
                capacity_key,
                fallback="unknown",
            )
            if (
                safe_capacity_key not in {"lease", "quota"}
                or type(row_count) is not int
                or type(row_limit) is not int
                or row_count < 0
                or row_limit < 1
                or row_count > row_limit
            ):
                continue
            capacity_rows.append((safe_capacity_key, row_count, row_limit))
        expected_capacity_limits = {
            "lease": ADMISSION_LEASE_ROW_LIMIT,
            "quota": ADMISSION_QUOTA_ROW_LIMIT,
        }
        capacity_contract_valid = (
            admission_capacity_valid
            and safe_capacity_backend in _INGRESS_ADMISSION_BACKENDS
            and len(capacity_rows) == 2
            and all(
                (
                    row_limit == expected_capacity_limits[capacity_key]
                    if safe_capacity_backend == "postgres"
                    else row_limit <= expected_capacity_limits[capacity_key]
                )
                for capacity_key, _row_count, row_limit in capacity_rows
            )
        )
        if not capacity_contract_valid:
            capacity_rows = []
        lines.extend(
            [
                "# HELP propertyquarry_admission_capacity_contract_valid Whether the shared PostgreSQL v17 capacity contract was read and validated.",
                "# TYPE propertyquarry_admission_capacity_contract_valid gauge",
                'propertyquarry_admission_capacity_contract_valid{backend="%s"} %d'
                % (
                    _label_value(safe_capacity_backend),
                    1 if capacity_contract_valid else 0,
                ),
                "# HELP propertyquarry_admission_capacity_row_count Rows charged to a bounded admission capacity key.",
                "# TYPE propertyquarry_admission_capacity_row_count gauge",
            ]
        )
        for capacity_key, row_count, _row_limit in sorted(capacity_rows):
            lines.append(
                'propertyquarry_admission_capacity_row_count{backend="%s",capacity_key="%s"} %d'
                % (
                    _label_value(safe_capacity_backend),
                    _label_value(capacity_key),
                    row_count,
                )
            )
        lines.extend(
            [
                "# HELP propertyquarry_admission_capacity_limit Hard row limit for a bounded admission capacity key.",
                "# TYPE propertyquarry_admission_capacity_limit gauge",
            ]
        )
        for capacity_key, _row_count, row_limit in sorted(capacity_rows):
            lines.append(
                'propertyquarry_admission_capacity_limit{backend="%s",capacity_key="%s"} %d'
                % (
                    _label_value(safe_capacity_backend),
                    _label_value(capacity_key),
                    row_limit,
                )
            )

        lines.extend(
            [
                "# HELP propertyquarry_content_ledger_events_total Governed property-content ledger outcomes.",
                "# TYPE propertyquarry_content_ledger_events_total counter",
            ]
        )
        for outcome in _CONTENT_LEDGER_METRIC_OUTCOMES:
            lines.append(
                'propertyquarry_content_ledger_events_total{outcome="%s"} %d'
                % (_label_value(outcome), max(0, int(content_ledger_events.get(outcome, 0))))
            )
        lines.extend(
            [
                "# HELP propertyquarry_local_span_export_failures_total Local span export failures observed by this process.",
                "# TYPE propertyquarry_local_span_export_failures_total counter",
                "propertyquarry_local_span_export_failures_total %d"
                % max(0, int(span_export_health["failure_count"])),
                "# HELP propertyquarry_local_span_export_recoveries_total Crash-truncated local span tails recovered by this process.",
                "# TYPE propertyquarry_local_span_export_recoveries_total counter",
                "propertyquarry_local_span_export_recoveries_total %d"
                % max(0, int(span_export_health["recovery_count"])),
            ]
        )

        lines.extend(
            [
                "# HELP propertyquarry_readiness Whether the API readiness gates currently pass.",
                "# TYPE propertyquarry_readiness gauge",
                f"propertyquarry_readiness {1 if readiness_ready else 0}",
            ]
        )
        heartbeat_env = environ if environ is not None else os.environ
        build_identity = runtime_build_identity(heartbeat_env)
        lines.extend(
            [
                "# HELP propertyquarry_runtime_build_info Process-observed release, image, and replica identity.",
                "# TYPE propertyquarry_runtime_build_info gauge",
                (
                    'propertyquarry_runtime_build_info{release_commit_sha="%s",'
                    'release_image_digest="%s",replica_id="%s"} 1'
                )
                % (
                    _label_value(build_identity["release_commit_sha"]),
                    _label_value(build_identity["release_image_digest"]),
                    _label_value(build_identity["replica_id"]),
                ),
            ]
        )
        expected_replicas = _expected_api_replicas(heartbeat_env)
        lines.extend(
            [
                "# HELP propertyquarry_expected_api_replicas Expected API replicas for per-target scrape coverage.",
                "# TYPE propertyquarry_expected_api_replicas gauge",
                f"propertyquarry_expected_api_replicas {expected_replicas}",
            ]
        )
        heartbeat_now = float(now_epoch if now_epoch is not None else time.time())
        samples = [_heartbeat_sample(role, heartbeat_env, heartbeat_now) for role in ("worker", "scheduler")]
        required_roles = {
            role: _heartbeat_required(role, heartbeat_env) for role in ("worker", "scheduler")
        }
        lines.extend(
            [
                "# HELP propertyquarry_runtime_heartbeat_required Whether the runtime role is required for this deployment.",
                "# TYPE propertyquarry_runtime_heartbeat_required gauge",
            ]
        )
        for role in ("worker", "scheduler"):
            lines.append(
                'propertyquarry_runtime_heartbeat_required{role="%s"} %d'
                % (_label_value(role), 1 if required_roles[role] else 0)
            )
        lines.extend(
            [
                "# HELP propertyquarry_runtime_heartbeat_age_seconds Age of the last role heartbeat; NaN when missing or invalid.",
                "# TYPE propertyquarry_runtime_heartbeat_age_seconds gauge",
            ]
        )
        for sample in samples:
            lines.append(
                'propertyquarry_runtime_heartbeat_age_seconds{role="%s"} %s'
                % (_label_value(sample.role), _metric_float(sample.age_seconds))
            )
        lines.extend(
            [
                "# HELP propertyquarry_runtime_heartbeat_present Whether a heartbeat file exists and parses.",
                "# TYPE propertyquarry_runtime_heartbeat_present gauge",
            ]
        )
        for sample in samples:
            lines.append(
                'propertyquarry_runtime_heartbeat_present{role="%s"} %d'
                % (_label_value(sample.role), 1 if sample.present else 0)
            )
        lines.extend(
            [
                "# HELP propertyquarry_runtime_heartbeat_stale Whether a heartbeat is missing, invalid, future-dated, or too old.",
                "# TYPE propertyquarry_runtime_heartbeat_stale gauge",
            ]
        )
        for sample in samples:
            lines.append(
                'propertyquarry_runtime_heartbeat_stale{role="%s"} %d'
                % (_label_value(sample.role), 1 if sample.stale else 0)
            )
        lines.extend(
            [
                "# HELP propertyquarry_queue_observed Whether a fresh valid bounded queue snapshot was observed.",
                "# TYPE propertyquarry_queue_observed gauge",
                "# HELP propertyquarry_queue_depth Current active work items in a bounded queue.",
                "# TYPE propertyquarry_queue_depth gauge",
                "# HELP propertyquarry_queue_oldest_item_age_seconds Age of the oldest active work item in a bounded queue.",
                "# TYPE propertyquarry_queue_oldest_item_age_seconds gauge",
            ]
        )
        worker_sample = next(sample for sample in samples if sample.role == "worker")
        property_search_queue_observed = (
            not worker_sample.stale
            and worker_sample.property_search_queue_depth is not None
            and worker_sample.property_search_queue_oldest_item_age_seconds is not None
        )
        lines.append(
            'propertyquarry_queue_observed{queue="property_search"} %d'
            % (1 if property_search_queue_observed else 0)
        )
        if property_search_queue_observed:
            oldest_item_age_seconds = (
                0.0
                if worker_sample.property_search_queue_depth == 0
                else (
                    worker_sample.property_search_queue_oldest_item_age_seconds
                    + worker_sample.age_seconds
                )
            )
            lines.append(
                'propertyquarry_queue_depth{queue="property_search"} %d'
                % worker_sample.property_search_queue_depth
            )
            lines.append(
                'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} %s'
                % _metric_float(oldest_item_age_seconds)
            )
        scheduler_sample = next(sample for sample in samples if sample.role == "scheduler")
        delivery_totals = dict(scheduler_sample.delivery_outbox_totals)
        lines.extend(
            [
                "# HELP propertyquarry_scheduler_delivery_outbox_events_total Scheduler delivery outbox outcomes reported by the latest heartbeat.",
                "# TYPE propertyquarry_scheduler_delivery_outbox_events_total counter",
            ]
        )
        for outcome in _DELIVERY_OUTBOX_METRIC_OUTCOMES:
            lines.append(
                'propertyquarry_scheduler_delivery_outbox_events_total{outcome="%s"} %d'
                % (_label_value(outcome), max(0, int(delivery_totals.get(outcome, 0))))
            )
        return "\n".join(lines) + "\n"


def get_runtime_metrics(app) -> RuntimeMetrics:  # type: ignore[no-untyped-def]
    registry = getattr(app.state, "runtime_metrics", None)
    if isinstance(registry, RuntimeMetrics):
        return registry
    registry = RuntimeMetrics()
    app.state.runtime_metrics = registry
    return registry


@dataclass(frozen=True)
class HeartbeatSample:
    role: str
    present: bool
    age_seconds: float
    stale: bool
    delivery_outbox_totals: tuple[tuple[str, int], ...] = ()
    property_search_queue_depth: int | None = None
    property_search_queue_oldest_item_age_seconds: float | None = None


def _positive_float(raw: str, default: float) -> float:
    try:
        value = float(str(raw or "").strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _heartbeat_required(role: str, environ: Mapping[str, str]) -> bool:
    normalized_role = str(role or "").strip().lower()
    default = normalized_role == "scheduler"
    key = f"PROPERTYQUARRY_{normalized_role.upper()}_HEARTBEAT_REQUIRED"
    value = str(environ.get(key) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _expected_api_replicas(environ: Mapping[str, str]) -> int:
    raw = str(environ.get("PROPERTYQUARRY_EXPECTED_API_REPLICAS") or "1").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if 1 <= value <= 100 else 0


def _heartbeat_sample(role: str, environ: Mapping[str, str], now_epoch: float) -> HeartbeatSample:
    normalized_role = str(role or "").strip().lower()
    prefix = "EA_SCHEDULER" if normalized_role == "scheduler" else "EA_WORKER"
    default_path = f"/data/artifacts/propertyquarry-{normalized_role}-heartbeat.json"
    path = Path(str(environ.get(f"{prefix}_HEARTBEAT_PATH") or default_path).strip())
    max_age = _positive_float(environ.get(f"{prefix}_HEARTBEAT_MAX_AGE_SECONDS", ""), 900.0)
    try:
        if not path.is_file() or path.stat().st_size > _MAX_HEARTBEAT_BYTES:
            return HeartbeatSample(normalized_role, False, math.nan, True)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return HeartbeatSample(normalized_role, False, math.nan, True)
        epoch = float(payload.get("epoch"))
        recorded_role = str(payload.get("role") or "").strip().lower()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return HeartbeatSample(normalized_role, False, math.nan, True)
    if not math.isfinite(epoch) or recorded_role != normalized_role:
        return HeartbeatSample(normalized_role, True, math.nan, True)
    raw_age = float(now_epoch) - epoch
    if raw_age < -5.0:
        return HeartbeatSample(normalized_role, True, math.nan, True)
    age = max(0.0, raw_age)
    raw_delivery_totals = payload.get("delivery_outbox")
    delivery_totals: list[tuple[str, int]] = []
    if normalized_role == "scheduler" and isinstance(raw_delivery_totals, dict):
        for outcome in _DELIVERY_OUTBOX_METRIC_OUTCOMES:
            try:
                value = max(0, int(raw_delivery_totals.get(outcome) or 0))
            except (TypeError, ValueError):
                value = 0
            delivery_totals.append((outcome, value))
    queue_depth: int | None = None
    queue_oldest_age: float | None = None
    raw_queue = payload.get("property_search_work_queue")
    if (
        normalized_role == "worker"
        and isinstance(raw_queue, dict)
        and raw_queue.get("observed") is True
    ):
        raw_depth = raw_queue.get("depth")
        raw_oldest_age = raw_queue.get("oldest_item_age_seconds")
        if type(raw_depth) is int and 0 <= raw_depth <= 2**63 - 1:
            if (
                isinstance(raw_oldest_age, (int, float))
                and not isinstance(raw_oldest_age, bool)
                and math.isfinite(float(raw_oldest_age))
                and 0 <= float(raw_oldest_age) <= 10 * 365 * 24 * 60 * 60
            ):
                queue_depth = raw_depth
                queue_oldest_age = float(raw_oldest_age)
    return HeartbeatSample(
        normalized_role,
        True,
        age,
        age > max_age,
        tuple(delivery_totals),
        queue_depth,
        queue_oldest_age,
    )


def runtime_heartbeat_readiness(
    role: str,
    environ: Mapping[str, str] | None = None,
    *,
    now_epoch: float | None = None,
) -> tuple[bool, str]:
    """Evaluate one required role heartbeat through the metrics parser."""

    normalized_role = str(role or "").strip().lower()
    if normalized_role not in {"worker", "scheduler"}:
        return False, "runtime_heartbeat_role_invalid"
    env = environ if environ is not None else os.environ
    if not _heartbeat_required(normalized_role, env):
        return True, f"{normalized_role}_heartbeat_not_required"
    sample = _heartbeat_sample(
        normalized_role,
        env,
        float(now_epoch if now_epoch is not None else time.time()),
    )
    if not sample.present:
        return False, f"{normalized_role}_heartbeat_missing_or_invalid"
    if sample.stale:
        return False, f"{normalized_role}_heartbeat_stale_or_invalid"
    return True, f"{normalized_role}_heartbeat_ready"
