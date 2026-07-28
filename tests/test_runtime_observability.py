from __future__ import annotations

import json
import logging
import sys
import concurrent.futures
from pathlib import Path

import pytest
from app.logging_utils import RedactingJsonFormatter
from app.observability import RuntimeMetrics, runtime_heartbeat_readiness
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.logging_utils import RedactingJsonFormatter
from app.observability import (
    RuntimeMetrics,
    bind_runtime_trace_context,
    child_trace_context,
    current_runtime_trace_context,
    new_server_trace_context,
    outbound_observability_headers,
    parse_traceparent,
    runtime_trace_context_from_mapping,
    submit_with_runtime_context,
)


def _app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "metrics-test-token")
    monkeypatch.setenv("EA_DEFAULT_PRINCIPAL_ID", "metrics-scraper")
    monkeypatch.setenv("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", "1")
    monkeypatch.setenv("EA_ALLOW_LOOPBACK_NO_AUTH", "0")
    monkeypatch.delenv("EA_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EA_CF_ACCESS_AUD", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "0")
    from app.api.app import create_app

    return create_app()


def _metrics_headers() -> dict[str, str]:
    return {
        "Authorization": " ".join(("Bearer", "metrics-test-token")),
        "X-EA-Principal-ID": "metrics-scraper",
    }


def test_json_logging_redacts_structured_fields_message_and_exception_stack() -> None:
    authorization_scheme = "Bearer"
    try:
        raise RuntimeError(
            f"Authorization: {authorization_scheme} top-secret "
            "DATABASE_URL=postgresql://user:db-pass@db/property"
        )
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="app.api.errors",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request failed api_token=plain-secret Cookie: session=private-cookie",
        args=(),
        exc_info=exc_info,
    )
    record.propertyquarry_fields = {
        "event": "unhandled_exception",
        "correlation_id": "corr-redaction-1",
        "error_detail": {
            "password": "hidden-password",
            "note": "Bearer another-secret",
        },
    }

    rendered = RedactingJsonFormatter().format(record)
    payload = json.loads(rendered)

    assert "top-secret" not in rendered
    assert "plain-secret" not in rendered
    assert "private-cookie" not in rendered
    assert "hidden-password" not in rendered
    assert "another-secret" not in rendered
    assert "db-pass" not in rendered
    assert payload["correlation_id"] == "corr-redaction-1"
    assert payload["error_detail"]["password"] == "***"
    assert payload["exception"]["type"] == "RuntimeError"
    assert "Traceback" in payload["exception"]["stack"]
    assert "Bearer ***" in rendered
    assert "DATABASE_URL=***" in rendered


def test_w3c_trace_context_is_strict_bounded_and_available_to_outbound_calls() -> None:
    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert parse_traceparent(incoming) == (
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "00f067aa0ba902b7",
        "01",
    )
    for invalid in (
        "",
        incoming.upper(),
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        incoming + "-extra",
    ):
        assert parse_traceparent(invalid) is None

    server = new_server_trace_context(incoming)
    assert server.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert server.parent_span_id == "00f067aa0ba902b7"
    assert server.span_id != server.parent_span_id
    assert server.source == "incoming"
    child = child_trace_context(server)
    assert child.trace_id == server.trace_id
    assert child.parent_span_id == server.span_id
    assert runtime_trace_context_from_mapping(server.as_mapping()) is not None

    assert current_runtime_trace_context() is None
    with bind_runtime_trace_context(child):
        assert current_runtime_trace_context() == child
        assert outbound_observability_headers(correlation_id="release-check-123") == {
            "traceparent": child.traceparent,
            "x-correlation-id": "release-check-123",
        }
    assert current_runtime_trace_context() is None

    from app.product.property_location_research import (
        _property_location_research_headers,
    )

    with bind_runtime_trace_context(child, correlation_id="search-boundary-1"):
        location_headers = _property_location_research_headers()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            threaded_headers = submit_with_runtime_context(
                executor,
                outbound_observability_headers,
            ).result(timeout=2)
    assert location_headers["traceparent"] == child.traceparent
    assert location_headers["x-correlation-id"] == "search-boundary-1"
    assert threaded_headers == {
        "traceparent": child.traceparent,
        "x-correlation-id": "search-boundary-1",
    }


def test_request_middleware_continues_valid_trace_and_never_reflects_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(monkeypatch)
    client = TestClient(app)
    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    with caplog.at_level(logging.INFO, logger="app.api.errors"):
        continued = client.get(
            "/health",
            headers={"Traceparent": incoming, "X-Correlation-ID": "trace-check-1"},
        )
    assert continued.status_code == 200
    parsed = parse_traceparent(continued.headers["traceparent"])
    assert parsed is not None
    assert parsed[0] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parsed[1] != "00f067aa0ba902b7"
    records = [record for record in caplog.records if record.getMessage() == "http_request_completed"]
    assert records
    rendered = json.loads(RedactingJsonFormatter().format(records[-1]))
    assert rendered["correlation_id"] == "trace-check-1"
    assert rendered["trace_id"] == parsed[0]
    assert rendered["span_id"] == parsed[1]
    assert rendered["parent_span_id"] == "00f067aa0ba902b7"
    assert rendered["trace_source"] == "incoming"
    assert set(rendered) >= {
        "release_commit_sha",
        "release_image_digest",
        "replica_id",
    }

    invalid = "00-not-a-valid-traceparent"
    regenerated = client.get("/health", headers={"Traceparent": invalid})
    assert regenerated.status_code == 200
    assert invalid not in regenerated.headers["traceparent"]
    assert parse_traceparent(regenerated.headers["traceparent"]) is not None


def test_registry_exports_bounded_request_error_latency_and_readiness_metrics(tmp_path: Path) -> None:
    registry = RuntimeMetrics()
    registry.record_request(method="GET", route="/items/{item_id}", status_code=200, duration_seconds=0.2)
    registry.record_request(method="GET", route="/items/{item_id}", status_code=500, duration_seconds=0.7)
    registry.record_content_ledger_event(outcome="claimed")
    registry.record_content_ledger_event(outcome="recovered")
    registry.record_content_ledger_event(outcome="unbounded-provider-value")
    metrics = registry.render_prometheus(
        readiness_ready=False,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(tmp_path / "missing-worker.json"),
            "EA_SCHEDULER_HEARTBEAT_PATH": str(tmp_path / "missing-scheduler.json"),
        },
        now_epoch=1_000.0,
    )

    assert 'propertyquarry_http_requests_total{method="GET",route="/items/{item_id}",status_class="2xx"} 1' in metrics
    assert 'propertyquarry_http_requests_total{method="GET",route="/items/{item_id}",status_class="5xx"} 1' in metrics
    assert 'propertyquarry_http_request_errors_total{method="GET",route="/items/{item_id}",status_class="5xx"} 1' in metrics
    assert 'propertyquarry_http_request_duration_seconds_bucket{method="GET",route="/items/{item_id}",le="1"} 2' in metrics
    assert 'propertyquarry_http_request_duration_seconds_count{method="GET",route="/items/{item_id}"} 2' in metrics
    assert 'propertyquarry_http_request_duration_seconds_sum{method="GET",route="/items/{item_id}"} 0.9' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="claimed"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="recovered"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="failed"} 1' in metrics
    assert 'propertyquarry_content_ledger_events_total{outcome="duplicate"} 0' in metrics
    assert "unbounded-provider-value" not in metrics
    assert "propertyquarry_readiness 0" in metrics
    assert "propertyquarry_expected_api_replicas 1" in metrics


def test_registry_exports_closed_authoritative_admission_metrics() -> None:
    registry = RuntimeMetrics()
    registry.record_ingress_admission_operation(
        backend="postgres",
        operation="admit",
        outcome="allowed",
    )
    registry.record_ingress_admission_capacity(
        backend="postgres",
        contract_valid=True,
        rows={
            "lease": (7, 100_000),
            "quota": (19, 1_000_000),
        },
    )

    metrics = registry.render_prometheus(readiness_ready=True)

    assert (
        "propertyquarry_ingress_admission_operations_total"
        '{backend="postgres",operation="admit",outcome="allowed"} 1'
    ) in metrics
    assert (
        "propertyquarry_ingress_admission_operations_total"
        '{backend="postgres",operation="renew",outcome="backend_unavailable"} 0'
    ) in metrics
    assert (
        'propertyquarry_admission_capacity_contract_valid{backend="postgres"} 1'
        in metrics
    )
    assert (
        "propertyquarry_admission_capacity_row_count"
        '{backend="postgres",capacity_key="lease"} 7'
    ) in metrics
    assert (
        "propertyquarry_admission_capacity_limit"
        '{backend="postgres",capacity_key="quota"} 1000000'
    ) in metrics
    with pytest.raises(
        ValueError,
        match="ingress_admission_metric_operation_invalid",
    ):
        registry.record_ingress_admission_operation(
            backend="postgres",
            operation="raw_route_name",
            outcome="allowed",
        )
    with pytest.raises(
        ValueError,
        match="ingress_admission_metric_capacity_contract_incomplete",
    ):
        registry.record_ingress_admission_capacity(
            backend="postgres",
            contract_valid=True,
            rows={"lease": (0, 100_000)},
        )


def test_metrics_endpoint_requires_system_auth_and_reuses_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    client = TestClient(app)

    health = client.get("/health", headers={"X-Correlation-ID": "release-check-123"})
    assert health.status_code == 200
    assert health.headers["x-correlation-id"] == "release-check-123"

    unauthenticated = client.get("/internal/metrics")
    assert unauthenticated.status_code == 401
    wrong_token = client.get(
        "/internal/metrics",
        headers={"Authorization": "Bearer wrong", "X-EA-Principal-ID": "metrics-scraper"},
    )
    assert wrong_token.status_code == 401

    scrape = client.get("/internal/metrics", headers=_metrics_headers())
    assert scrape.status_code == 200
    assert scrape.headers["cache-control"] == "no-store"
    assert scrape.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'propertyquarry_http_requests_total{method="GET",route="/health",status_class="2xx"} 1' in scrape.text
    assert "propertyquarry_readiness 1" in scrape.text
    assert "/internal/metrics" not in client.get("/openapi.json").json()["paths"]


def test_metrics_readiness_fails_closed_when_admission_snapshot_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.ingress_admission import (
        AdmissionBackend,
        AdmissionOperation,
        IngressAdmissionUnavailable,
    )

    class _UnavailableAdmissionStore:
        @staticmethod
        def capacity_snapshot():  # type: ignore[no-untyped-def]
            raise IngressAdmissionUnavailable(
                "test_admission_unavailable",
                backend=AdmissionBackend.POSTGRES,
                operation=AdmissionOperation.SNAPSHOT,
            )

    app = _app(monkeypatch)
    app.state.ingress_admission_store = _UnavailableAdmissionStore()

    scrape = TestClient(app).get(
        "/internal/metrics",
        headers=_metrics_headers(),
    )

    assert scrape.status_code == 200
    assert "propertyquarry_readiness 0" in scrape.text
    assert (
        "propertyquarry_ingress_admission_operations_total"
        '{backend="postgres",operation="snapshot",'
        'outcome="backend_unavailable"} 1'
    ) in scrape.text
    assert (
        'propertyquarry_admission_capacity_contract_valid{backend="postgres"} 0'
        in scrape.text
    )


def test_error_counter_and_latency_are_recorded_by_real_request_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)

    @app.get("/_test/observability-http-error")
    async def _http_error() -> None:
        raise HTTPException(status_code=500, detail="test_failure")

    client = TestClient(app)
    response = client.get("/_test/observability-http-error")
    assert response.status_code == 500

    scrape = client.get("/internal/metrics", headers=_metrics_headers())
    assert scrape.status_code == 200
    assert (
        'propertyquarry_http_request_errors_total{method="GET",route="/_test/observability-http-error",status_class="5xx"} 1'
        in scrape.text
    )
    assert (
        'propertyquarry_http_request_duration_seconds_count{method="GET",route="/_test/observability-http-error"} 1'
        in scrape.text
    )


def test_unhandled_exception_log_has_stack_and_correlation_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(monkeypatch)

    @app.get("/_test/observability-unhandled")
    async def _unhandled() -> None:
        raise RuntimeError(
            "api_token=never-log-me postgresql://user:never-log-db-password@db/property"
        )

    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/_test/observability-unhandled",
            headers={"X-Correlation-ID": "corr-unhandled-456"},
        )

    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == "corr-unhandled-456"
    records = [record for record in caplog.records if record.getMessage() == "unhandled_exception"]
    assert len(records) == 1
    rendered = RedactingJsonFormatter().format(records[0])
    payload = json.loads(rendered)
    assert payload["correlation_id"] == "corr-unhandled-456"
    assert payload["exception"]["type"] == "RuntimeError"
    assert "Traceback" in payload["exception"]["stack"]
    assert "never-log-me" not in rendered
    assert "never-log-db-password" not in rendered


def test_stale_and_missing_role_heartbeat_metrics_fail_closed(tmp_path: Path) -> None:
    worker_path = tmp_path / "worker.json"
    scheduler_path = tmp_path / "scheduler.json"
    worker_path.write_text(
        json.dumps(
            {
                "role": "worker",
                "epoch": 995.0,
                "pid": 10,
                "property_search_work_queue": {
                    "observed": True,
                    "depth": 7,
                    "oldest_item_age_seconds": 41.5,
                },
            }
        ),
        encoding="utf-8",
    )
    scheduler_path.write_text(
        json.dumps(
            {
                "role": "scheduler",
                "epoch": 800.0,
                "pid": 11,
                "delivery_outbox": {
                    "queued": 7,
                    "claimed": 6,
                    "sent": 5,
                    "retried": 1,
                    "dead_lettered": 1,
                    "claim_conflicts": 2,
                    "failed": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    registry = RuntimeMetrics()
    metrics = registry.render_prometheus(
        readiness_ready=True,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(worker_path),
            "EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS": "30",
            "EA_SCHEDULER_HEARTBEAT_PATH": str(scheduler_path),
            "EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS": "60",
        },
        now_epoch=1_000.0,
    )

    assert 'propertyquarry_runtime_heartbeat_age_seconds{role="worker"} 5' in metrics
    assert 'propertyquarry_runtime_heartbeat_required{role="worker"} 0' in metrics
    assert 'propertyquarry_runtime_heartbeat_required{role="scheduler"} 1' in metrics
    assert 'propertyquarry_runtime_heartbeat_stale{role="worker"} 0' in metrics
    assert 'propertyquarry_runtime_heartbeat_age_seconds{role="scheduler"} 200' in metrics
    assert 'propertyquarry_runtime_heartbeat_stale{role="scheduler"} 1' in metrics
    assert 'propertyquarry_scheduler_delivery_outbox_events_total{outcome="sent"} 5' in metrics
    assert 'propertyquarry_scheduler_delivery_outbox_events_total{outcome="dead_lettered"} 1' in metrics
    assert 'propertyquarry_scheduler_delivery_outbox_events_total{outcome="claim_conflicts"} 2' in metrics
    assert 'propertyquarry_queue_depth{queue="property_search"} 7' in metrics
    assert (
        'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} 46.5'
        in metrics
    )

    three_replicas = registry.render_prometheus(
        readiness_ready=True,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(worker_path),
            "EA_SCHEDULER_HEARTBEAT_PATH": str(scheduler_path),
            "PROPERTYQUARRY_EXPECTED_API_REPLICAS": "3",
        },
        now_epoch=1_000.0,
    )
    assert "propertyquarry_expected_api_replicas 3" in three_replicas

    explicitly_required = registry.render_prometheus(
        readiness_ready=True,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(worker_path),
            "EA_SCHEDULER_HEARTBEAT_PATH": str(scheduler_path),
            "PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED": "1",
        },
        now_epoch=1_000.0,
    )
    assert 'propertyquarry_runtime_heartbeat_required{role="worker"} 1' in explicitly_required

    scheduler_path.unlink()
    missing = registry.render_prometheus(
        readiness_ready=True,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(worker_path),
            "EA_SCHEDULER_HEARTBEAT_PATH": str(scheduler_path),
        },
        now_epoch=1_000.0,
    )
    assert 'propertyquarry_runtime_heartbeat_present{role="scheduler"} 0' in missing
    assert 'propertyquarry_runtime_heartbeat_age_seconds{role="scheduler"} NaN' in missing
    assert 'propertyquarry_runtime_heartbeat_stale{role="scheduler"} 1' in missing

    worker_path.write_text(
        json.dumps(
            {
                "role": "worker",
                "epoch": 995.0,
                "property_search_work_queue": {
                    "observed": True,
                    "depth": True,
                    "oldest_item_age_seconds": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    invalid_queue = registry.render_prometheus(
        readiness_ready=True,
        environ={"EA_WORKER_HEARTBEAT_PATH": str(worker_path)},
        now_epoch=1_000.0,
    )
    assert 'propertyquarry_queue_depth{queue="property_search"}' not in invalid_queue
    assert (
        'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"}'
        not in invalid_queue
    )
