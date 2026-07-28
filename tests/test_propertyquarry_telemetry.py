from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import stat
from types import SimpleNamespace
import urllib.request

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import Headers

from app.api.errors import install_error_handlers
from app.logging_utils import RedactingJsonFormatter, log_event
from app.observability import RuntimeMetrics, runtime_build_identity
from app.product import property_search_work_queue as queue_module
from app.product import service as product_service
from app.product.property_listing_extractors import _property_scout_download_bytes
from app.product.property_search_work_queue import (
    InMemoryPropertySearchWorkQueue,
    PostgresPropertySearchWorkQueue,
    PropertySearchWorkEnqueueResult,
    PropertySearchWorkJob,
)
from app.product.service import ProductService
from app.telemetry import (
    LOCAL_SPAN_EVIDENCE_SCOPE,
    LOCAL_SPAN_SCHEMA,
    TELEMETRY_PARENT_KEY,
    BoundedJsonlSpanExporter,
    InMemorySpanExporter,
    NullSpanExporter,
    SpanExportConfigurationError,
    SpanExportError,
    SpanRecord,
    TraceContextError,
    current_telemetry_context,
    extract_traceparent,
    format_traceparent,
    generate_span_id,
    generate_trace_id,
    inject_traceparent,
    normalize_persisted_trace_parent,
    parse_traceparent,
    persisted_trace_parent_from_payload,
    serialize_current_trace_parent,
    span_export_health_snapshot,
    span_exporter_from_environment,
    start_span,
    use_span_exporter,
)


_TRACE_ID = "1" * 32
_SPAN_ID = "2" * 16
_TRACEPARENT = f"00-{_TRACE_ID}-{_SPAN_ID}-01"
_COMMIT_SHA = "a" * 40
_IMAGE_DIGEST = "sha256:" + ("b" * 64)


def _set_release_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA", _COMMIT_SHA)
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", _IMAGE_DIGEST)


def _local_span_record(
    *,
    sequence: int = 1,
    boundary: str = "customer_api",
) -> SpanRecord:
    identity = runtime_build_identity()
    second = sequence % 60
    return SpanRecord(
        boundary=boundary,
        trace_id=f"{sequence + 1:032x}",
        span_id=f"{sequence + 1:016x}",
        parent_span_id="",
        release_commit_sha=identity["release_commit_sha"],
        release_image_digest=identity["release_image_digest"],
        replica_id=identity["replica_id"],
        started_at=f"2026-07-26T08:00:{second:02d}.000000Z",
        ended_at=f"2026-07-26T08:00:{second:02d}.000001Z",
    )


def _job(
    *,
    payload_json: dict[str, object],
    attempt_count: int = 1,
    status: str = "leased",
    run_id: str = "run-telemetry",
    principal_id: str = "principal-telemetry",
) -> PropertySearchWorkJob:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    return PropertySearchWorkJob(
        job_id="job-telemetry-1",
        principal_id=principal_id,
        run_id=run_id,
        idempotency_key="property-search:telemetry",
        payload_json=payload_json,
        status=status,
        attempt_count=attempt_count,
        max_attempts=3,
        available_at=now,
        lease_owner="worker:test",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        f"00-{'0' * 32}-{_SPAN_ID}-01",
        f"00-{_TRACE_ID}-{'0' * 16}-01",
        f"01-{_TRACE_ID}-{_SPAN_ID}-01",
        f"ff-{_TRACE_ID}-{_SPAN_ID}-01",
        f"00-{'A' + _TRACE_ID[1:]}-{_SPAN_ID}-01",
        f"00-{_TRACE_ID}-{'B' + _SPAN_ID[1:]}-01",
        f"00-{_TRACE_ID}-{_SPAN_ID}-0g",
        f"00-{_TRACE_ID}-{_SPAN_ID}-G0",
        f" 00-{_TRACE_ID}-{_SPAN_ID}-01",
        f"00-{_TRACE_ID}-{_SPAN_ID}-01 ",
        f"00-{_TRACE_ID}-{_SPAN_ID}-01-extra",
    ],
)
def test_traceparent_v00_parser_rejects_malformed_or_unsafe_values(
    value: object,
) -> None:
    with pytest.raises(TraceContextError):
        parse_traceparent(value)


def test_traceparent_v00_round_trip_and_secure_ids_are_nonzero() -> None:
    parsed = parse_traceparent(_TRACEPARENT)

    assert parsed.trace_id == _TRACE_ID
    assert parsed.span_id == _SPAN_ID
    assert parsed.trace_flags == "01"
    assert format_traceparent(parsed) == _TRACEPARENT

    trace_ids = {generate_trace_id() for _ in range(32)}
    span_ids = {generate_span_id() for _ in range(32)}
    assert len(trace_ids) == 32
    assert len(span_ids) == 32
    assert all(len(value) == 32 and value != "0" * 32 for value in trace_ids)
    assert all(len(value) == 16 and value != "0" * 16 for value in span_ids)


def test_traceparent_injection_replaces_case_variant_and_strips_extra_context() -> None:
    headers = {
        "TraceParent": f"00-{'3' * 32}-{'4' * 16}-00",
        "TraceState": "vendor=opaque",
        "BAGGAGE": "secret=unbounded",
        "Accept": "application/json",
    }

    injected = inject_traceparent(headers, parse_traceparent(_TRACEPARENT))

    assert injected == _TRACEPARENT
    assert headers == {
        "Accept": "application/json",
        "traceparent": _TRACEPARENT,
    }


def test_duplicate_traceparent_headers_fail_closed() -> None:
    headers = Headers(
        raw=[
            (b"traceparent", _TRACEPARENT.encode("ascii")),
            (
                b"traceparent",
                (
                    f"00-{'3' * 32}-{'4' * 16}-01"
                ).encode("ascii"),
            ),
        ]
    )

    with pytest.raises(TraceContextError, match="traceparent_multiple"):
        extract_traceparent(headers)


@pytest.mark.parametrize(
    ("trace_flags", "outgoing_flags"),
    [
        ("00", "00"),
        ("01", "01"),
        ("02", "00"),
        ("7f", "01"),
        ("ff", "01"),
    ],
)
def test_traceparent_zeros_reserved_v00_flags_on_output(
    trace_flags: str,
    outgoing_flags: str,
) -> None:
    value = f"00-{_TRACE_ID}-{_SPAN_ID}-{trace_flags}"
    parsed = parse_traceparent(value)

    assert parsed.trace_flags == trace_flags
    assert format_traceparent(parsed) == (
        f"00-{_TRACE_ID}-{_SPAN_ID}-{outgoing_flags}"
    )


def test_secure_id_generation_fails_boundedly_if_entropy_returns_only_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import telemetry

    monkeypatch.setattr(
        telemetry.secrets,
        "token_bytes",
        lambda byte_count: b"\0" * byte_count,
    )

    with pytest.raises(RuntimeError, match="trace_id_generation_failed"):
        generate_trace_id()


def test_persisted_and_outbound_context_rejects_baggage_and_tracestate() -> None:
    with pytest.raises(
        TraceContextError,
        match="persisted_trace_parent_fields_invalid",
    ):
        normalize_persisted_trace_parent(
            {
                "traceparent": _TRACEPARENT,
                "correlation_id": "corr-safe",
                "baggage": "private=value",
            }
        )

    with pytest.raises(
        TraceContextError,
        match="persisted_correlation_id_invalid",
    ):
        normalize_persisted_trace_parent(
            {
                "traceparent": _TRACEPARENT,
                "correlation_id": 123,
            }
        )

    headers = {
        "Baggage": "private=value",
        "tracestate": "vendor=opaque",
    }
    injected = inject_traceparent(headers, parse_traceparent(_TRACEPARENT))

    assert injected == _TRACEPARENT
    assert {key.lower() for key in headers} == {"traceparent"}


def test_parent_graph_survives_the_durable_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_identity(monkeypatch)
    sink = InMemorySpanExporter()

    with use_span_exporter(sink):
        with start_span(
            "customer_api",
            correlation_id="corr-parent-graph",
        ) as customer:
            carrier = serialize_current_trace_parent()
        persisted = persisted_trace_parent_from_payload(
            {TELEMETRY_PARENT_KEY: carrier}
        )
        with start_span("durable_search_worker", parent=persisted) as worker:
            with start_span("provider_or_render_boundary") as provider:
                assert current_telemetry_context() == provider

    spans = {span.boundary: span for span in sink.spans}
    assert set(spans) == {
        "customer_api",
        "durable_search_worker",
        "provider_or_render_boundary",
    }
    assert {span.trace_id for span in spans.values()} == {customer.trace_id}
    assert spans["customer_api"].parent_span_id == ""
    assert spans["durable_search_worker"].parent_span_id == customer.span_id
    assert spans["provider_or_render_boundary"].parent_span_id == worker.span_id
    assert len({span.span_id for span in spans.values()}) == 3
    assert all(span.release_commit_sha == _COMMIT_SHA for span in spans.values())
    assert all(
        span.release_image_digest == _IMAGE_DIGEST for span in spans.values()
    )


def test_structured_event_keeps_its_emission_context_when_formatted_later(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_release_identity(monkeypatch)
    logger = logging.getLogger("tests.propertyquarry.telemetry.deferred")
    with caplog.at_level(logging.INFO, logger=logger.name):
        with start_span(
            "customer_api",
            correlation_id="corr-original",
        ) as original:
            log_event(logger, logging.INFO, "deferred.telemetry.event")

    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "deferred.telemetry.event"
    )
    with start_span(
        "customer_api",
        correlation_id="corr-unrelated",
    ) as unrelated:
        payload = json.loads(RedactingJsonFormatter().format(record))

    assert payload["correlation_id"] == "corr-original"
    assert payload["trace_id"] == original.trace_id
    assert payload["span_id"] == original.span_id
    assert payload["trace_id"] != unrelated.trace_id


def test_api_middleware_restores_valid_parent_rejects_invalid_parent_and_binds_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_release_identity(monkeypatch)
    application = FastAPI()
    install_error_handlers(application)
    logger = logging.getLogger("tests.propertyquarry.telemetry")

    @application.get("/telemetry-probe")
    async def telemetry_probe(request: Request) -> dict[str, object]:
        log_event(
            logger,
            logging.INFO,
            "telemetry.probe.completed",
            trace_id="0" * 32,
            span_id="0" * 16,
            release_commit_sha="spoofed",
            error_detail={
                "password": "private-password",
                "note": "Bearer private-token",
            },
        )
        return {
            "traceparent_rejected": request.state.traceparent_rejected,
        }

    sink = InMemorySpanExporter()
    with use_span_exporter(sink), caplog.at_level(
        logging.INFO,
        logger=logger.name,
    ):
        valid_response = TestClient(application).get(
            "/telemetry-probe",
            headers={
                "traceparent": _TRACEPARENT,
                "x-correlation-id": "corr-api-valid",
                "baggage": "must-not-propagate=private",
            },
        )
        invalid_response = TestClient(application).get(
            "/telemetry-probe",
            headers={
                "traceparent": f"00-{'0' * 32}-{_SPAN_ID}-01",
                "x-correlation-id": "corr-api-invalid",
            },
        )

    assert valid_response.status_code == 200
    assert valid_response.json()["traceparent_rejected"] is False
    valid_response_parent = parse_traceparent(
        valid_response.headers["traceparent"]
    )
    assert valid_response_parent.trace_id == _TRACE_ID
    assert valid_response_parent.span_id != _SPAN_ID
    assert "baggage" not in valid_response.headers

    assert invalid_response.status_code == 200
    assert invalid_response.json()["traceparent_rejected"] is True
    invalid_response_parent = parse_traceparent(
        invalid_response.headers["traceparent"]
    )
    assert invalid_response_parent.trace_id != "0" * 32
    assert invalid_response_parent.trace_id != _TRACE_ID

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "telemetry.probe.completed"
    ]
    assert len(records) == 2
    payload = json.loads(RedactingJsonFormatter().format(records[0]))
    required_fields = {
        "timestamp",
        "service",
        "event",
        "correlation_id",
        "trace_id",
        "span_id",
        "release_commit_sha",
        "release_image_digest",
        "replica_id",
    }
    assert required_fields <= set(payload)
    assert payload["event"] == "telemetry.probe.completed"
    assert payload["correlation_id"] == "corr-api-valid"
    assert payload["trace_id"] == _TRACE_ID
    assert payload["span_id"] == valid_response_parent.span_id
    assert payload["release_commit_sha"] == _COMMIT_SHA
    assert payload["release_image_digest"] == _IMAGE_DIGEST
    rendered = json.dumps(payload, sort_keys=True)
    assert "private-password" not in rendered
    assert "private-token" not in rendered
    assert payload["error_detail"]["password"] == "***"

    customer_spans = [
        span for span in sink.spans if span.boundary == "customer_api"
    ]
    assert len(customer_spans) == 2
    assert customer_spans[0].trace_id == _TRACE_ID
    assert customer_spans[0].parent_span_id == _SPAN_ID
    assert customer_spans[1].parent_span_id == ""


def test_unhandled_error_response_keeps_customer_trace_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_identity(monkeypatch)
    application = FastAPI()
    install_error_handlers(application)

    @application.get("/telemetry-error")
    async def telemetry_error() -> None:
        raise RuntimeError("telemetry failure")

    response = TestClient(
        application,
        raise_server_exceptions=False,
    ).get(
        "/telemetry-error",
        headers={
            "traceparent": _TRACEPARENT,
            "x-correlation-id": "corr-api-error",
        },
    )

    propagated = parse_traceparent(response.headers["traceparent"])
    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == "corr-api-error"
    assert propagated.trace_id == _TRACE_ID
    assert propagated.span_id != _SPAN_ID


def test_service_enqueues_only_the_minimal_active_trace_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_identity(monkeypatch)
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    service = object.__new__(ProductService)
    monkeypatch.setattr(
        ProductService,
        "_open_property_market_bootstrap",
        lambda self, **_kwargs: None,
    )
    monkeypatch.setattr(
        ProductService,
        "_resolve_property_search_run_preferences",
        lambda self, **kwargs: (
            tuple(kwargs.get("selected_platforms") or ()),
            dict(kwargs.get("property_preferences") or {}),
            kwargs.get("max_results_per_source"),
        ),
    )
    monkeypatch.setattr(
        ProductService,
        "_best_effort_propertyquarry_teable_sync",
        lambda self, **_kwargs: None,
    )
    monkeypatch.setattr(
        product_service,
        "enforce_property_plan_limits",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(product_service, "_prune_property_search_runs", lambda: None)
    captured: dict[str, object] = {}

    class _Repository:
        def enqueue_run(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            record = dict(kwargs["run_record"])
            return PropertySearchWorkEnqueueResult(
                job=_job(
                    payload_json=dict(kwargs["payload_json"]),
                    status="queued",
                    run_id=str(record["run_id"]),
                    principal_id=str(record["principal_id"]),
                ),
                created=True,
            )

    monkeypatch.setattr(
        product_service,
        "_property_search_work_queue_repository",
        lambda: _Repository(),
    )

    with start_span(
        "customer_api",
        parent=parse_traceparent(_TRACEPARENT),
        correlation_id="corr-service-enqueue",
    ) as customer:
        result = service.start_property_search_run(
            principal_id="principal-telemetry",
            actor="api",
            selected_platforms=("willhaben",),
            property_search_preferences={"country_code": "AT"},
            dispatch_only=True,
            idempotency_key="telemetry-request",
        )

    payload = dict(captured["payload_json"])
    assert payload["run_id"] == result["run_id"]
    assert set(payload[TELEMETRY_PARENT_KEY]) == {
        "traceparent",
        "correlation_id",
    }
    persisted = persisted_trace_parent_from_payload(payload)
    assert persisted is not None
    assert persisted.trace_parent.trace_id == customer.trace_id
    assert persisted.trace_parent.span_id == customer.span_id
    assert persisted.correlation_id == "corr-service-enqueue"
    assert "baggage" not in payload[TELEMETRY_PARENT_KEY]
    assert "tracestate" not in payload[TELEMETRY_PARENT_KEY]


def test_queue_validates_carrier_and_preserves_it_across_row_reload_and_retry() -> None:
    carrier = {
        "traceparent": _TRACEPARENT,
        "correlation_id": "corr-queue-restart",
    }
    payload = {
        "run_id": "run-telemetry",
        TELEMETRY_PARENT_KEY: carrier,
    }
    queue = InMemoryPropertySearchWorkQueue(backoff_seconds=lambda _attempt: 0)
    enqueue = queue.enqueue_run(
        run_record={
            "principal_id": "principal-telemetry",
            "run_id": "run-telemetry",
        },
        payload_json=payload,
        idempotency_key="property-search:telemetry",
    )
    first = queue.claim(lease_owner="worker:one", lease_seconds=30)
    assert first is not None
    queue.fail(
        job_id=first.job_id,
        lease_owner="worker:one",
        error="retry",
    )
    second = queue.claim(lease_owner="worker:two", lease_seconds=30)
    assert second is not None
    assert first.payload_json == second.payload_json == enqueue.job.payload_json

    reloaded_payload = json.loads(json.dumps(second.payload_json))
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    row = (
        second.job_id,
        second.principal_id,
        second.run_id,
        second.idempotency_key,
        reloaded_payload,
        second.status,
        second.attempt_count,
        second.max_attempts,
        now,
        second.lease_owner,
        now,
        now,
        "",
        now,
        now,
        None,
    )
    reloaded = PostgresPropertySearchWorkQueue._from_row(row)
    persisted = persisted_trace_parent_from_payload(reloaded.payload_json)
    assert persisted is not None
    assert format_traceparent(persisted) == _TRACEPARENT
    assert persisted.correlation_id == "corr-queue-restart"

    with pytest.raises(
        ValueError,
        match="property_search_trace_context_invalid",
    ):
        queue.enqueue_run(
            run_record={
                "principal_id": "principal-other",
                "run_id": "run-invalid",
            },
            payload_json={
                TELEMETRY_PARENT_KEY: {
                    "traceparent": _TRACEPARENT,
                    "baggage": "private=value",
                }
            },
            idempotency_key="property-search:invalid",
        )


def test_postgres_claim_quarantines_invalid_carrier_and_continues() -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    payloads: dict[str, dict[str, object]] = {
        "job-poison": {
            "run_id": "run-poison",
            TELEMETRY_PARENT_KEY: {
                "traceparent": _TRACEPARENT,
                "baggage": "private=value",
            },
        },
        "job-valid": {
            "run_id": "run-valid",
            TELEMETRY_PARENT_KEY: {
                "traceparent": _TRACEPARENT,
                "correlation_id": "corr-valid",
            },
        },
    }
    statuses = {job_id: "queued" for job_id in payloads}

    class _ClaimCursor:
        def __init__(self) -> None:
            self.row: tuple[object, ...] | None = None
            self.rowcount = 0

        def __enter__(self) -> "_ClaimCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            normalized = " ".join(sql.split())
            values = tuple(params or ())
            self.row = None
            self.rowcount = 0
            if "UPDATE property_search_work_jobs AS jobs" in normalized:
                owner = str(values[0])
                job_id = str(values[2])
                run_id = f"run-{job_id.removeprefix('job-')}"
                statuses[job_id] = "leased"
                self.row = (
                    job_id,
                    "principal-telemetry",
                    run_id,
                    f"property-search:{job_id}",
                    payloads[job_id],
                    "leased",
                    1,
                    3,
                    now,
                    owner,
                    now,
                    now,
                    "",
                    now,
                    now,
                    None,
                )
                self.rowcount = 1
            elif (
                "last_error = 'property_search_trace_context_invalid'"
                in normalized
            ):
                job_id = str(values[0])
                statuses[job_id] = "failed"
                self.rowcount = 1

        def fetchone(self) -> tuple[object, ...] | None:
            row = self.row
            self.row = None
            return row

    class _ClaimConnection:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self) -> "_ClaimConnection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _ClaimCursor:
            return _ClaimCursor()

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    connection = _ClaimConnection()
    repository = object.__new__(PostgresPropertySearchWorkQueue)
    repository._connect = lambda: connection  # type: ignore[method-assign]
    repository._nonlocking_claim_candidate_job_ids = (  # type: ignore[method-assign]
        lambda _cursor, *, exhausted: (
            () if exhausted else ("job-poison", "job-valid")
        )
    )
    repository._set_writer_contract = lambda _cursor: None  # type: ignore[method-assign]
    repository._nonlocking_job_identity = (  # type: ignore[method-assign]
        lambda _cursor, *, job_id: (
            "principal-telemetry",
            f"run-{job_id.removeprefix('job-')}",
        )
    )
    repository._acquire_principal_write_authority = (  # type: ignore[method-assign]
        lambda _cursor, *, principal_id, run_id: None
    )

    claimed = repository.claim(lease_owner="worker:test", lease_seconds=30)

    assert claimed is not None
    assert claimed.job_id == "job-valid"
    assert statuses == {
        "job-poison": "failed",
        "job-valid": "leased",
    }
    assert connection.rollbacks == 0


def test_worker_retry_restores_parent_and_creates_distinct_attempt_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner

    _set_release_identity(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/test")
    with start_span(
        "customer_api",
        correlation_id="corr-worker-retry",
    ) as customer:
        carrier = serialize_current_trace_parent()
    base_job = _job(
        payload_json={
            "run_id": "run-telemetry",
            TELEMETRY_PARENT_KEY: carrier,
        },
    )

    class _Repository:
        def __init__(self) -> None:
            self.attempt = 0
            self.current = base_job

        def claim(self, **kwargs):  # type: ignore[no-untyped-def]
            self.attempt += 1
            self.current = replace(
                base_job,
                attempt_count=self.attempt,
                lease_owner=str(kwargs["lease_owner"]),
                status="leased",
            )
            return self.current

        def heartbeat(self, **_kwargs: object) -> bool:
            return True

        def fail(self, **_kwargs: object) -> PropertySearchWorkJob:
            return replace(
                self.current,
                status="queued",
                lease_owner="",
            )

        def complete(self, **_kwargs: object) -> PropertySearchWorkJob:
            return replace(
                self.current,
                status="completed",
                lease_owner="",
            )

    repository = _Repository()
    monkeypatch.setattr(
        queue_module,
        "PostgresPropertySearchWorkQueue",
        lambda _database_url: repository,
    )
    monkeypatch.setattr(
        queue_module,
        "property_search_work_lease_seconds",
        lambda: 30,
    )
    monkeypatch.setattr(
        queue_module,
        "property_search_work_heartbeat_seconds",
        lambda: 1,
    )
    executions: list[str] = []

    class _Service:
        def execute_property_search_work_job(
            self,
            job: PropertySearchWorkJob,
        ) -> dict[str, object]:
            context = current_telemetry_context()
            assert context is not None
            assert context.boundary == "durable_search_worker"
            executions.append(context.span_id)
            if job.attempt_count == 1:
                raise RuntimeError("retry-once")
            with start_span("provider_or_render_boundary"):
                return {"status": "completed"}

    monkeypatch.setattr(
        product_service,
        "build_product_service",
        lambda _container: _Service(),
    )
    sink = InMemorySpanExporter()

    with use_span_exporter(sink):
        first = runner._run_property_search_work_once(
            SimpleNamespace(),
            role="worker",
            log=logging.getLogger("tests.telemetry.worker"),
        )
        second = runner._run_property_search_work_once(
            SimpleNamespace(),
            role="worker",
            log=logging.getLogger("tests.telemetry.worker"),
        )

    assert first["status"] == "queued"
    assert second["completed"] is True
    assert len(executions) == 2
    assert len(set(executions)) == 2
    worker_spans = [
        span for span in sink.spans if span.boundary == "durable_search_worker"
    ]
    provider_spans = [
        span
        for span in sink.spans
        if span.boundary == "provider_or_render_boundary"
    ]
    assert len(worker_spans) == 2
    assert len(provider_spans) == 1
    assert {span.trace_id for span in worker_spans} == {customer.trace_id}
    assert {span.parent_span_id for span in worker_spans} == {customer.span_id}
    assert provider_spans[0].parent_span_id in {
        span.span_id for span in worker_spans
    }
    assert provider_spans[0].trace_id == customer.trace_id


def test_property_scout_outbound_request_injects_provider_child_traceparent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_identity(monkeypatch)
    captured: dict[str, object] = {}

    class _Response:
        headers = {
            "Content-Length": "3",
            "Content-Type": "application/octet-stream",
        }

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"zip"

    def _urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    sink = InMemorySpanExporter()

    with use_span_exporter(sink):
        with start_span(
            "durable_search_worker",
            parent=parse_traceparent(_TRACEPARENT),
            correlation_id="corr-provider",
        ) as worker:
            payload, content_type = _property_scout_download_bytes(
                "https://example.test/archive.zip",
                timeout_seconds=4.5,
                max_bytes=10,
            )

    assert payload == b"zip"
    assert content_type == "application/octet-stream"
    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    headers = {
        key.lower(): value
        for key, value in request.header_items()
    }
    outbound_parent = parse_traceparent(headers["traceparent"])
    provider_span = next(
        span
        for span in sink.spans
        if span.boundary == "provider_or_render_boundary"
    )
    assert outbound_parent.trace_id == worker.trace_id
    assert outbound_parent.span_id == provider_span.span_id
    assert provider_span.parent_span_id == worker.span_id
    assert "baggage" not in headers
    assert "tracestate" not in headers


def test_local_span_exporter_is_opt_in_and_disabled_mode_performs_no_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "disabled-spans" / "spans.jsonl"
    monkeypatch.delenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_ENABLED", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH", str(path))

    exporter = span_exporter_from_environment()

    assert isinstance(exporter, NullSpanExporter)
    assert not path.parent.exists()


def test_local_span_exporter_writes_private_canonical_queryable_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _set_release_identity(monkeypatch)
    path = tmp_path / "private-spans" / "spans.jsonl"
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH", str(path))
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_MAX_BYTES", "4096")
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_BACKUP_COUNT", "2")
    exporter = span_exporter_from_environment()
    assert isinstance(exporter, BoundedJsonlSpanExporter)

    with use_span_exporter(exporter):
        with start_span(
            "customer_api",
            correlation_id="corr-local-evidence",
        ) as customer:
            with start_span("durable_search_worker") as worker:
                with start_span("provider_or_render_boundary") as provider:
                    pass

    spans = exporter.query_spans(trace_id=customer.trace_id)
    assert [span.boundary for span in spans] == [
        "customer_api",
        "durable_search_worker",
        "provider_or_render_boundary",
    ]
    by_boundary = {span.boundary: span for span in spans}
    assert by_boundary["customer_api"].parent_span_id == ""
    assert by_boundary["durable_search_worker"].parent_span_id == customer.span_id
    assert (
        by_boundary["provider_or_render_boundary"].parent_span_id
        == worker.span_id
    )
    assert provider.trace_id == customer.trace_id

    envelopes = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(
        set(envelope)
        == {
            "schema",
            "evidence_scope",
            "live_receipt_eligible",
            "span",
        }
        for envelope in envelopes
    )
    assert all(envelope["schema"] == LOCAL_SPAN_SCHEMA for envelope in envelopes)
    assert all(
        envelope["evidence_scope"] == LOCAL_SPAN_EVIDENCE_SCOPE
        for envelope in envelopes
    )
    assert all(envelope["live_receipt_eligible"] is False for envelope in envelopes)
    root = next(
        envelope["span"]
        for envelope in envelopes
        if envelope["span"]["boundary"] == "customer_api"
    )
    assert root["parent_span_id"] is None
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE((path.parent / ".spans.jsonl.lock").stat().st_mode)
        == 0o600
    )


def test_local_span_exporter_configuration_and_identity_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "missing-identity" / "spans.jsonl"
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH", str(path))
    monkeypatch.delenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", raising=False)

    with pytest.raises(
        SpanExportConfigurationError,
        match="local_span_export_release_identity_required",
    ):
        span_exporter_from_environment()
    assert not path.parent.exists()

    _set_release_identity(monkeypatch)
    monkeypatch.setenv(
        "PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH",
        "relative/spans.jsonl",
    )
    with pytest.raises(
        SpanExportConfigurationError,
        match="local_span_export_path_invalid",
    ):
        span_exporter_from_environment()


def test_local_span_exporter_rejects_caller_spoofing_and_symlink_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _set_release_identity(monkeypatch)
    path = tmp_path / "spoof-spans" / "spans.jsonl"
    exporter = BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=1)
    spoofed = replace(
        _local_span_record(),
        release_commit_sha="c" * 40,
    )

    with pytest.raises(
        SpanExportError,
        match="local_span_runtime_identity_mismatch",
    ):
        exporter.export(spoofed)
    assert not path.exists()

    target = tmp_path / "outside-target.jsonl"
    target.write_text("do-not-touch\n", encoding="utf-8")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(
        SpanExportError,
        match="local_span_export_file_open_failed",
    ):
        exporter.export(_local_span_record(sequence=2))
    assert target.read_text(encoding="utf-8") == "do-not-touch\n"


def test_local_span_exporter_recovers_partial_tail_and_rejects_noncanonical_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _set_release_identity(monkeypatch)
    path = tmp_path / "partial-spans" / "spans.jsonl"
    exporter = BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=1)
    first = _local_span_record()
    exporter.export(first)
    with path.open("ab") as stream:
        stream.write(b'{"partial":true')
    before = span_export_health_snapshot()

    restarted = BoundedJsonlSpanExporter(
        path,
        max_bytes=4096,
        backup_count=1,
    )

    after = span_export_health_snapshot()
    assert after["recovery_count"] == before["recovery_count"] + 1
    assert restarted.query_spans() == (first,)
    assert path.read_bytes().endswith(b"\n")
    assert b'{"partial":true' not in path.read_bytes()

    noncanonical = replace(
        _local_span_record(),
        started_at="2026-07-26T08:00:01Z",
    )
    with pytest.raises(
        SpanExportError,
        match="local_span_started_at_invalid",
    ):
        restarted.export(noncanonical)
    assert restarted.query_spans() == (first,)


def test_local_span_exporter_rolls_back_short_failed_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app import telemetry

    _set_release_identity(monkeypatch)
    path = tmp_path / "short-write-spans" / "spans.jsonl"
    exporter = BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=1)
    original_write_all = BoundedJsonlSpanExporter._write_all

    def _partial_then_full(fd: int, raw: bytes) -> None:
        telemetry.os.write(fd, raw[:17])
        raise OSError(28, "simulated disk full")

    monkeypatch.setattr(
        BoundedJsonlSpanExporter,
        "_write_all",
        staticmethod(_partial_then_full),
    )
    with pytest.raises(
        SpanExportError,
        match="local_span_export_write_failed",
    ):
        exporter.export(_local_span_record())
    assert path.read_bytes() == b""

    monkeypatch.setattr(
        BoundedJsonlSpanExporter,
        "_write_all",
        staticmethod(original_write_all),
    )
    expected = _local_span_record(sequence=2)
    exporter.export(expected)
    assert exporter.query_spans() == (expected,)


def test_local_span_exporter_queries_history_across_runtime_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app import telemetry

    path = tmp_path / "rolling-spans" / "spans.jsonl"
    identity_a = {
        "release_commit_sha": "a" * 40,
        "release_image_digest": "sha256:" + ("b" * 64),
        "replica_id": "api-a",
    }
    identity_b = {
        "release_commit_sha": "c" * 40,
        "release_image_digest": "sha256:" + ("d" * 64),
        "replica_id": "api-b",
    }
    monkeypatch.setattr(
        telemetry,
        "runtime_build_identity",
        lambda: dict(identity_a),
    )
    exporter_a = BoundedJsonlSpanExporter(
        path,
        max_bytes=4096,
        backup_count=1,
    )
    span_a = replace(_local_span_record(), **identity_a)
    exporter_a.export(span_a)

    monkeypatch.setattr(
        telemetry,
        "runtime_build_identity",
        lambda: dict(identity_b),
    )
    exporter_b = BoundedJsonlSpanExporter(
        path,
        max_bytes=4096,
        backup_count=1,
    )
    span_b = replace(_local_span_record(sequence=2), **identity_b)
    exporter_b.export(span_b)

    assert exporter_b.query_spans() == (span_a, span_b)


def test_start_span_surfaces_export_failure_without_changing_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingExporter:
        def export(self, span: SpanRecord) -> None:
            del span
            raise SpanExportError("local_span_export_test_failure")

    before = span_export_health_snapshot()
    with caplog.at_level(logging.ERROR, logger="ea.telemetry"):
        with use_span_exporter(_FailingExporter()):
            with start_span("customer_api"):
                pass
    after = span_export_health_snapshot()

    assert after["failure_count"] == before["failure_count"] + 1
    assert (
        after["last_failure_reason"]
        == "local_span_export_test_failure"
    )
    assert after["last_failure_at"]
    assert "span export failed exporter=custom" in caplog.text
    rendered = RuntimeMetrics().render_prometheus(readiness_ready=True)
    assert (
        f"propertyquarry_local_span_export_failures_total "
        f"{after['failure_count']}"
    ) in rendered
    assert (
        f"propertyquarry_local_span_export_recoveries_total "
        f"{after['recovery_count']}"
    ) in rendered


def test_local_span_exporter_query_rejects_noncanonical_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _set_release_identity(monkeypatch)
    path = tmp_path / "noncanonical-spans" / "spans.jsonl"
    exporter = BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=1)
    exporter.export(_local_span_record())
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(payload, sort_keys=False, separators=(", ", ": ")) + "\r\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(
        SpanExportError,
        match="local_span_export_json_not_canonical",
    ):
        exporter.query_spans()


def test_local_span_exporter_rotates_safely_under_concurrent_writers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _set_release_identity(monkeypatch)
    path = tmp_path / "concurrent-spans" / "spans.jsonl"
    exporters = [
        BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=2),
        BoundedJsonlSpanExporter(path, max_bytes=4096, backup_count=2),
    ]
    boundaries = (
        "customer_api",
        "durable_search_worker",
        "provider_or_render_boundary",
    )
    records = [
        _local_span_record(
            sequence=index + 1,
            boundary=boundaries[index % len(boundaries)],
        )
        for index in range(36)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(exporters[index % 2].export, record)
            for index, record in enumerate(records)
        ]
        for future in futures:
            future.result()

    files = [path, path.with_name("spans.jsonl.1"), path.with_name("spans.jsonl.2")]
    for candidate in files:
        assert candidate.exists()
        raw = candidate.read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw) <= 4096
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
        for line in raw.splitlines():
            assert json.loads(line)["live_receipt_eligible"] is False

    queried = exporters[0].query_spans()
    assert queried
    assert len({span.span_id for span in queried}) == len(queried)
    assert all(span.release_commit_sha == _COMMIT_SHA for span in queried)
    with pytest.raises(
        SpanExportError,
        match="local_span_query_trace_id_invalid",
    ):
        exporters[0].query_spans(trace_id=0)  # type: ignore[arg-type]
