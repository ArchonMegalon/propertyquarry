from __future__ import annotations

import json
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pytest
import yaml
from propertyquarry_evidence_test_support import (
    CanonicalMonitoringTestIdentity,
    EvidenceTestAuthority,
    OperatorGatewayTestAuthority,
    install_test_authority,
    install_test_canonical_monitoring_identity,
    install_test_operator_gateway,
)

from ea.app import observability
from ea.app.observability import RuntimeMetrics, runtime_build_identity
from scripts import propertyquarry_evidence_contract as evidence_contract
from scripts import propertyquarry_gold_status as gold_status
from scripts import propertyquarry_observability_receipts as receipts
from scripts import propertyquarry_slo_evidence as evidence

RELEASE_SHA = "d" * 40
IMAGE_DIGEST = "sha256:" + "e" * 64
CONTAINER_IMAGE_ID = "sha256:" + "f" * 64
CONTAINER_ID = "1" * 64
REPLICA_ID = "propertyquarry-api-1"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
BUCKETS = ("0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "+Inf")
AUTHORITY: EvidenceTestAuthority
GATEWAY: OperatorGatewayTestAuthority
CANONICAL: CanonicalMonitoringTestIdentity


@pytest.fixture(autouse=True)
def _authenticated_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    global AUTHORITY
    AUTHORITY = install_test_authority(
        monkeypatch,
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        now=NOW,
    )
    global GATEWAY
    GATEWAY = install_test_operator_gateway(
        monkeypatch,
        evidence_authority=AUTHORITY,
    )
    global CANONICAL
    CANONICAL = install_test_canonical_monitoring_identity(
        monkeypatch,
        directory=tmp_path / "canonical-monitoring",
    )


def _metrics(multiplier: int) -> str:
    count = 100 * multiplier
    bucket_base = (0, 0, 20, 60, 90, 100, 100, 100, 100, 100, 100, 100)
    bucket_rows = "\n".join(
        "propertyquarry_http_request_duration_seconds_bucket"
        f'{{method="GET",route="/health",le="{bound}"}} {value * multiplier}'
        for bound, value in zip(BUCKETS, bucket_base, strict=True)
    )
    admission_rows = "\n".join(
        (
            "propertyquarry_ingress_admission_operations_total"
            f'{{backend="postgres",operation="{operation}",outcome="{outcome}"}} 0'
        )
        for operation in (
            "ip_request",
            "admit",
            "renew",
            "release",
            "cleanup",
            "snapshot",
        )
        for outcome in (
            "allowed",
            "quota_limited",
            "lease_limited",
            "capacity_exhausted",
            "backend_unavailable",
        )
    )
    return f"""\
# HELP propertyquarry_http_requests_total HTTP requests.
# TYPE propertyquarry_http_requests_total counter
propertyquarry_http_requests_total{{method="GET",route="/health",status_class="2xx"}} {count}
# HELP propertyquarry_http_request_errors_total HTTP errors.
# TYPE propertyquarry_http_request_errors_total counter
propertyquarry_http_request_errors_total{{method="GET",route="/providers/quota",status_class="5xx"}} 0
# HELP propertyquarry_http_request_duration_seconds HTTP latency.
# TYPE propertyquarry_http_request_duration_seconds histogram
{bucket_rows}
propertyquarry_http_request_duration_seconds_sum{{method="GET",route="/health"}} {10 * multiplier}
propertyquarry_http_request_duration_seconds_count{{method="GET",route="/health"}} {count}
# TYPE propertyquarry_runtime_build_info gauge
propertyquarry_runtime_build_info{{release_commit_sha="{RELEASE_SHA}",release_image_digest="{IMAGE_DIGEST}",replica_id="{REPLICA_ID}"}} 1
# TYPE propertyquarry_readiness gauge
propertyquarry_readiness 1
# TYPE propertyquarry_expected_api_replicas gauge
propertyquarry_expected_api_replicas 1
# TYPE propertyquarry_runtime_heartbeat_required gauge
propertyquarry_runtime_heartbeat_required{{role="worker"}} 1
propertyquarry_runtime_heartbeat_required{{role="scheduler"}} 1
# TYPE propertyquarry_runtime_heartbeat_age_seconds gauge
propertyquarry_runtime_heartbeat_age_seconds{{role="worker"}} 5
propertyquarry_runtime_heartbeat_age_seconds{{role="scheduler"}} 5
# TYPE propertyquarry_runtime_heartbeat_present gauge
propertyquarry_runtime_heartbeat_present{{role="worker"}} 1
propertyquarry_runtime_heartbeat_present{{role="scheduler"}} 1
# TYPE propertyquarry_runtime_heartbeat_stale gauge
propertyquarry_runtime_heartbeat_stale{{role="worker"}} 0
propertyquarry_runtime_heartbeat_stale{{role="scheduler"}} 0
# TYPE propertyquarry_ingress_rejections_total counter
# TYPE propertyquarry_ingress_cost_units_total counter
# TYPE propertyquarry_ingress_high_cost_inflight gauge
# TYPE propertyquarry_ingress_admission_operations_total counter
{admission_rows}
# TYPE propertyquarry_admission_capacity_contract_valid gauge
propertyquarry_admission_capacity_contract_valid{{backend="postgres"}} 1
# TYPE propertyquarry_admission_capacity_row_count gauge
propertyquarry_admission_capacity_row_count{{backend="postgres",capacity_key="lease"}} 0
propertyquarry_admission_capacity_row_count{{backend="postgres",capacity_key="quota"}} 0
# TYPE propertyquarry_admission_capacity_limit gauge
propertyquarry_admission_capacity_limit{{backend="postgres",capacity_key="lease"}} 100000
propertyquarry_admission_capacity_limit{{backend="postgres",capacity_key="quota"}} 1000000
# TYPE propertyquarry_queue_observed gauge
propertyquarry_queue_observed{{queue="property_search"}} 1
# TYPE propertyquarry_queue_depth gauge
propertyquarry_queue_depth{{queue="property_search"}} 0
# TYPE propertyquarry_queue_oldest_item_age_seconds gauge
propertyquarry_queue_oldest_item_age_seconds{{queue="property_search"}} 0
# TYPE propertyquarry_scheduler_delivery_outbox_events_total counter
propertyquarry_scheduler_delivery_outbox_events_total{{outcome="dead_lettered"}} 0
propertyquarry_scheduler_delivery_outbox_events_total{{outcome="failed"}} 0
propertyquarry_scheduler_delivery_outbox_events_total{{outcome="claim_conflicts"}} 0
# TYPE propertyquarry_content_ledger_events_total counter
propertyquarry_content_ledger_events_total{{outcome="replay_conflict"}} 0
propertyquarry_content_ledger_events_total{{outcome="failed"}} 0
propertyquarry_content_ledger_events_total{{outcome="corruption"}} 0
"""


BASE_START_METRICS = _metrics(1)
BASE_END_METRICS = _metrics(2)


class FakePromtoolRunner:
    def __init__(
        self,
        *,
        available: bool = True,
        results: Mapping[str, evidence.CommandResult] | None = None,
    ) -> None:
        self.is_available = available
        self.results = dict(results or {})
        self.calls: list[tuple[str, ...]] = []

    def available(self, tool: str = "promtool") -> bool:
        return self.is_available

    def run(
        self, argv: Sequence[str], *, timeout_seconds: int
    ) -> evidence.CommandResult:
        del timeout_seconds
        command = tuple(argv)
        self.calls.append(command)
        if command == ("promtool", "--version"):
            step = "promtool_version"
            default = evidence.CommandResult(0, stdout="promtool, version 3.5.0")
        elif command == ("amtool", "--version"):
            step = "amtool_version"
            default = evidence.CommandResult(0, stdout="amtool, version 0.28.1")
        elif command[1:3] == ("check", "rules"):
            step = "check"
            default = evidence.CommandResult(0, stdout="SUCCESS: rules found")
        elif command[1:3] == ("check", "config"):
            step = "config"
            default = evidence.CommandResult(0, stdout="SUCCESS: config is valid")
        elif command[1:3] == ("test", "rules"):
            step = "test"
            default = evidence.CommandResult(0, stdout="SUCCESS")
        elif command[0:2] == ("amtool", "check-config"):
            step = "routing"
            default = evidence.CommandResult(0, stdout="SUCCESS")
        else:
            raise AssertionError(f"unexpected monitoring command: {command}")
        return self.results.get(step, default)


def _canonical_payload(payload: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized["payload_sha256"] = evidence.canonical_json_sha256(normalized)
    return normalized


def _write_json(path: Path, payload: object) -> bytes:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _transport(path: str, *, authenticated: bool) -> dict[str, object]:
    return {
        "endpoint_path": path,
        "authenticated": authenticated,
        "private_route": True,
        "credential_persisted": False,
        "http_status": 200,
        "content_type": (
            "text/plain; version=0.0.4" if path == "/internal/metrics" else "application/json"
        ),
        "cache_control": "private, no-store",
        "connected_peer_ip": "127.0.0.1",
        "tls_verified": False,
    }


def _write_inputs(
    tmp_path: Path,
    *,
    start_metrics: str = BASE_START_METRICS,
    end_metrics: str = BASE_END_METRICS,
    captured_at: datetime = NOW - timedelta(minutes=1),
    range_window_seconds: int = evidence.PROMETHEUS_RANGE_WINDOW_SECONDS,
) -> tuple[Path, Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    start_at = captured_at - timedelta(seconds=60)
    start_path = tmp_path / "metrics.111111111111.start.prom"
    end_path = tmp_path / "metrics.111111111111.end.prom"
    start_raw = start_metrics.encode()
    end_raw = end_metrics.encode()
    start_path.write_bytes(start_raw)
    end_path.write_bytes(end_raw)
    inspect_hash = "2" * 64
    start_ref = {
        "captured_at": evidence.isoformat(start_at),
        "path": start_path.name,
        "sha256": evidence.sha256_bytes(start_raw),
        "bytes": len(start_raw),
    }
    end_ref = {
        "captured_at": evidence.isoformat(captured_at),
        "path": end_path.name,
        "sha256": evidence.sha256_bytes(end_raw),
        "bytes": len(end_raw),
    }
    probe_path = tmp_path / "probe.111111111111.json"
    replica_probe = _canonical_payload(
        {
            "schema": evidence.PROBE_SCHEMA,
            "capture_tool": "propertyquarry.slo_metrics_capture.v2",
            "captured_at": evidence.isoformat(captured_at),
            "container_id": CONTAINER_ID,
            "container_image_id": CONTAINER_IMAGE_ID,
            "replica_id": REPLICA_ID,
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "docker_inspect_sha256": inspect_hash,
            "version": {
                "release_commit_sha": RELEASE_SHA,
                "release_image_digest": IMAGE_DIGEST,
                "replica_id": REPLICA_ID,
                "role": "api",
                "response_sha256": "3" * 64,
            },
            "version_transport": _transport("/version", authenticated=False),
            "snapshots": [
                {**start_ref, **_transport("/internal/metrics", authenticated=True)},
                {**end_ref, **_transport("/internal/metrics", authenticated=True)},
            ],
        }
    )
    probe_raw = _write_json(probe_path, replica_probe)
    snapshot_bundle_path = tmp_path / "metrics.json"
    snapshot_bundle = _canonical_payload(
        {
            "schema": evidence.SNAPSHOT_BUNDLE_SCHEMA,
            "capture_tool": "propertyquarry.slo_metrics_capture.v2",
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "window_start": evidence.isoformat(start_at),
            "window_end": evidence.isoformat(captured_at),
            "window_seconds": 60.0,
            "replica_count": 1,
            "replicas": [
                {
                    "container_id": CONTAINER_ID,
                    "container_image_id": CONTAINER_IMAGE_ID,
                    "replica_id": REPLICA_ID,
                    "release_commit_sha": RELEASE_SHA,
                    "release_image_digest": IMAGE_DIGEST,
                    "docker_inspect_sha256": inspect_hash,
                    "start": start_ref,
                    "end": end_ref,
                }
            ],
        }
    )
    snapshot_bundle_raw = _write_json(snapshot_bundle_path, snapshot_bundle)
    probe_bundle_path = tmp_path / "probe.json"
    probe_bundle = _canonical_payload(
        {
            "schema": evidence.PROBE_BUNDLE_SCHEMA,
            "capture_tool": "propertyquarry.slo_metrics_capture.v2",
            "captured_at": evidence.isoformat(captured_at),
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "replica_count": 1,
            "snapshot_bundle_sha256": evidence.sha256_bytes(snapshot_bundle_raw),
            "snapshot_bundle_bytes": len(snapshot_bundle_raw),
            "replicas": [
                {
                    "replica_id": REPLICA_ID,
                    "container_id": CONTAINER_ID,
                    "path": probe_path.name,
                    "sha256": evidence.sha256_bytes(probe_raw),
                    "bytes": len(probe_raw),
                }
            ],
            "credential_persisted": False,
        }
    )
    _write_json(probe_bundle_path, probe_bundle)

    range_end = NOW - timedelta(minutes=1)
    range_start = range_end - timedelta(seconds=range_window_seconds)
    # Range proof is an independent Prometheus artifact; short-window mutation
    # tests intentionally leave it valid so the first failing contract is clear.
    start_families, start_samples = evidence.parse_metrics_snapshot(BASE_START_METRICS)
    end_families, end_samples = evidence.parse_metrics_snapshot(BASE_END_METRICS)
    del start_families, end_families
    end_by_key = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample for sample in end_samples
    }
    contracted = {
        "propertyquarry_http_requests_total",
        "propertyquarry_http_request_errors_total",
        "propertyquarry_http_request_duration_seconds_bucket",
        "propertyquarry_http_request_duration_seconds_sum",
        "propertyquarry_http_request_duration_seconds_count",
        "propertyquarry_ingress_admission_operations_total",
        "propertyquarry_runtime_build_info",
    }
    matrix: list[dict[str, object]] = []
    for sample in start_samples:
        if sample.name not in contracted:
            continue
        key = (sample.name, tuple(sorted(sample.labels.items())))
        end_sample = end_by_key[key]
        metric = {
            "__name__": sample.name,
            **dict(sample.labels),
            "replica_id": REPLICA_ID,
            "container_id": CONTAINER_ID,
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "instance": "127.0.0.1:8090",
            "job": "propertyquarry",
            "service": "propertyquarry",
        }
        sample_count = range_window_seconds // evidence_contract.RANGE_STEP_SECONDS + 1
        values = []
        for sample_index in range(sample_count):
            fraction = sample_index / (sample_count - 1)
            value = sample.value + (end_sample.value - sample.value) * fraction
            values.append(
                [
                    range_start.timestamp()
                    + sample_index * evidence_contract.RANGE_STEP_SECONDS,
                    str(value),
                ]
            )
        matrix.append({"metric": metric, "values": values})
    matrix = evidence._normalized_matrix(matrix)
    range_response_path = tmp_path / "prometheus-range.json"
    range_response = {
        "status": "success",
        "data": {"resultType": "matrix", "result": matrix},
    }
    range_raw = json.dumps(range_response, sort_keys=True, separators=(",", ":")).encode()
    range_response_path.write_bytes(range_raw)
    binding = {
        "replica_id": REPLICA_ID,
        "container_id": CONTAINER_ID,
        "container_image_id": CONTAINER_IMAGE_ID,
        "release_commit_sha": RELEASE_SHA,
        "release_image_digest": IMAGE_DIGEST,
        "start_snapshot_sha256": evidence.sha256_bytes(start_raw),
        "end_snapshot_sha256": evidence.sha256_bytes(end_raw),
    }
    query = {
        "expression": evidence.PROMETHEUS_RANGE_QUERY,
        "start": evidence.isoformat(range_start),
        "end": evidence.isoformat(range_end),
        "step_seconds": evidence_contract.RANGE_STEP_SECONDS,
    }
    query["contract_sha256"] = evidence.canonical_json_sha256(query)
    range_receipt = AUTHORITY.authenticate(
        {
            "schema": evidence.RANGE_RECEIPT_SCHEMA,
            "producer": evidence.RANGE_RECEIPT_PRODUCER,
            "captured_at": evidence.isoformat(NOW),
            "release": {"commit_sha": RELEASE_SHA, "image_digest": IMAGE_DIGEST},
            "snapshot_bundle_sha256": evidence.sha256_bytes(snapshot_bundle_raw),
            "query": query,
            "transport": {
                "endpoint_path": "/api/v1/query_range",
                "authenticated": True,
                "credential_persisted": False,
                "http_status": 200,
                "tls_verified": True,
                "connected_peer_ip": "10.0.0.5",
            },
            "prometheus_config_sha256": evidence.sha256_bytes(
                evidence.DEFAULT_PROMETHEUS_CONFIG_PATH.read_bytes()
            ),
            "expected_replica_ids": [REPLICA_ID],
            "replicas": [binding],
            "series": {
                "result_type": "matrix",
                "count": len(matrix),
                "sha256": evidence.canonical_json_sha256(matrix),
            },
            "range_response_sha256": evidence.sha256_bytes(range_raw),
            "range_response_bytes": len(range_raw),
        },
        domain=evidence_contract.RANGE_DOMAIN,
    )
    range_receipt_path = tmp_path / "prometheus-range.receipt.json"
    _write_json(range_receipt_path, range_receipt)
    return snapshot_bundle_path, probe_bundle_path, range_response_path, range_receipt_path


def _config(
    tmp_path: Path,
    *,
    flagship: bool = True,
    start_metrics: str = BASE_START_METRICS,
    end_metrics: str = BASE_END_METRICS,
    captured_at: datetime = NOW - timedelta(minutes=1),
    range_window_seconds: int = evidence.PROMETHEUS_RANGE_WINDOW_SECONDS,
    rules_path: Path = evidence.DEFAULT_RULES_PATH,
    rule_tests_path: Path = evidence.DEFAULT_RULE_TESTS_PATH,
) -> evidence.EvidenceConfig:
    snapshot, probe, range_response, range_receipt = _write_inputs(
        tmp_path,
        start_metrics=start_metrics,
        end_metrics=end_metrics,
        captured_at=captured_at,
        range_window_seconds=range_window_seconds,
    )
    return evidence.EvidenceConfig(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        metrics_snapshot_path=snapshot,
        metrics_probe_path=probe,
        prometheus_range_path=range_response,
        prometheus_range_receipt_path=range_receipt,
        slo_path=evidence.DEFAULT_SLO_PATH,
        rules_path=rules_path,
        rule_tests_path=rule_tests_path,
        receipt_path=tmp_path / "receipt.json",
        flagship=flagship,
        timeout_seconds=30,
    )


def _rewrite_hashed_json(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("payload_sha256", None)
    mutate(payload)
    if payload.get("schema") == evidence.RANGE_RECEIPT_SCHEMA:
        payload = AUTHORITY.resign(payload, domain=evidence_contract.RANGE_DOMAIN)
    else:
        payload["payload_sha256"] = evidence.canonical_json_sha256(payload)
    _write_json(path, payload)


@pytest.mark.parametrize(
    "objective_id",
    ("ingress_admission_backend", "ingress_admission_capacity"),
)
def test_slo_requires_authoritative_ingress_objectives(
    objective_id: str,
) -> None:
    payload = json.loads(
        evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8")
    )
    payload["objectives"] = [
        row for row in payload["objectives"] if row["id"] != objective_id
    ]

    with pytest.raises(
        evidence.SloValidationError,
        match="missing a launch-evaluated objective",
    ):
        evidence.validate_slo_document(payload)


@pytest.mark.parametrize(
    ("collection", "removed", "error"),
    (
        (
            "required_metric_families",
            "propertyquarry_admission_capacity_contract_valid",
            "missing an authoritative admission metric",
        ),
        (
            "required_alerts",
            "PropertyQuarryAdmissionCapacityContractInvalid",
            "missing an authoritative admission alert",
        ),
    ),
)
def test_slo_requires_authoritative_ingress_metrics_and_alerts(
    collection: str,
    removed: str,
    error: str,
) -> None:
    payload = json.loads(
        evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8")
    )
    payload[collection].remove(removed)

    with pytest.raises(evidence.SloValidationError, match=error):
        evidence.validate_slo_document(payload)


@pytest.mark.parametrize(
    ("collection", "removed", "error"),
    (
        (
            "required_metric_families",
            "propertyquarry_queue_observed",
            "missing an authoritative queue metric",
        ),
        (
            "required_alerts",
            "PropertyQuarryQueueTelemetryUnobserved",
            "missing an authoritative queue alert",
        ),
    ),
)
def test_slo_requires_authoritative_queue_metrics_and_alerts(
    collection: str,
    removed: str,
    error: str,
) -> None:
    payload = json.loads(
        evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8")
    )
    payload[collection].remove(removed)

    with pytest.raises(evidence.SloValidationError, match=error):
        evidence.validate_slo_document(payload)


@pytest.mark.parametrize("target_observed", (0, False, 2))
def test_slo_rejects_noncanonical_queue_observation_target(
    target_observed: object,
) -> None:
    payload = json.loads(
        evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8")
    )
    payload["conditional_capabilities"]["queue_backlog"][
        "target_observed"
    ] = target_observed

    with pytest.raises(
        evidence.SloValidationError,
        match="queue-backlog capability is not canonical",
    ):
        evidence.validate_slo_document(payload)


@pytest.mark.parametrize(
    ("objective_id", "field", "value", "error"),
    (
        (
            "ingress_admission_backend",
            "target_rate",
            1,
            "ingress-admission-backend objective is not canonical",
        ),
        (
            "ingress_admission_backend",
            "target_rate",
            False,
            "ingress-admission-backend objective is not canonical",
        ),
        (
            "ingress_admission_backend",
            "indicator",
            "sum(rate(unbounded[1m]))",
            "ingress-admission-backend objective is not canonical",
        ),
        (
            "ingress_admission_capacity",
            "warning_ratio",
            9,
            "ingress-admission-capacity objective is not canonical",
        ),
        (
            "ingress_admission_capacity",
            "target_contract_valid",
            True,
            "ingress-admission-capacity objective is not canonical",
        ),
        (
            "ingress_admission_capacity",
            "critical_ratio",
            -1,
            "ingress-admission-capacity objective is not canonical",
        ),
        (
            "ingress_admission_capacity",
            "hard_limits",
            {"lease": 1, "quota": 2},
            "ingress-admission-capacity objective is not canonical",
        ),
    ),
)
def test_slo_rejects_mutated_authoritative_ingress_objectives(
    objective_id: str,
    field: str,
    value: object,
    error: str,
) -> None:
    payload = json.loads(
        evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8")
    )
    objective = next(
        row for row in payload["objectives"] if row["id"] == objective_id
    )
    objective[field] = value

    with pytest.raises(evidence.SloValidationError, match=error):
        evidence.validate_slo_document(payload)


@pytest.mark.parametrize("declared_bytes", [True, 1.0, "1", -1])
def test_file_reference_bytes_require_exact_nonnegative_json_integer(
    tmp_path: Path,
    declared_bytes: object,
) -> None:
    artifact = tmp_path / "artifact.prom"
    artifact.write_bytes(b"x")
    with pytest.raises(evidence.SloValidationError, match="JSON integer"):
        evidence._validated_file_reference(
            tmp_path,
            {
                "path": artifact.name,
                "sha256": evidence.sha256_bytes(b"x"),
                "bytes": declared_bytes,
            },
            label="artifact",
        )


def test_file_reference_rejects_symlink_and_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.prom"
    artifact.write_bytes(b"original")
    linked = tmp_path / "linked.prom"
    linked.symlink_to(artifact)
    reference = {
        "path": linked.name,
        "sha256": evidence.sha256_bytes(b"original"),
        "bytes": len(b"original"),
    }
    with pytest.raises(evidence.SloValidationError, match="unsafe"):
        evidence._validated_file_reference(tmp_path, reference, label="artifact")

    replacement = tmp_path / "replacement.prom"
    replacement.write_bytes(b"attacker")
    original_read = evidence.os.read
    replaced = False

    def replacing_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        raw = original_read(fd, size)
        if not replaced:
            replaced = True
            artifact.unlink()
            replacement.rename(artifact)
        return raw

    monkeypatch.setattr(evidence.os, "read", replacing_read)
    with pytest.raises(evidence.SloValidationError, match="changed while it was read"):
        evidence._validated_file_reference(
            tmp_path,
            {
                "path": artifact.name,
                "sha256": evidence.sha256_bytes(b"original"),
                "bytes": len(b"original"),
            },
            label="artifact",
        )


def test_flagship_evidence_passes_two_snapshot_and_authenticated_range_contract(
    tmp_path: Path,
) -> None:
    runner = FakePromtoolRunner()
    config = _config(tmp_path)
    receipt, exit_code = evidence.run_evidence_gate(config=config, runner=runner, now=NOW)

    assert exit_code == 0
    assert receipt["status"] == "pass"
    assert receipt["gate_passed"] is True
    assert receipt["live_monitoring_contacted"] is False
    assert receipt["probe"]["replica_count"] == 1
    assert receipt["probe"]["release_commit_sha"] == RELEASE_SHA
    assert receipt["metrics"]["short_window_slos"]["status"] == "pass"
    assert receipt["metrics"]["short_window_slos"]["values"]["request_delta"] == 100
    assert receipt["metrics"]["replicas"][0]["container_id"] == CONTAINER_ID
    assert receipt["metrics"]["replicas"][0]["container_image_id"] == CONTAINER_IMAGE_ID
    assert receipt["prometheus_range"]["schema"] == evidence.RANGE_RECEIPT_SCHEMA
    assert receipt["prometheus_range"]["window_seconds"] >= 30 * 24 * 60 * 60
    assert receipt["prometheus_range"]["slo"]["status"] == "pass"
    assert receipt["promtool"]["injection_test_passed"] is True
    assert receipt["amtool"]["routing_check_passed"] is True
    assert receipt["monitoring_config"]["per_replica_discovery"] is True
    assert stat.S_IMODE(config.receipt_path.stat().st_mode) == 0o600


def test_ingress_backend_failure_fails_short_window_slo(
    tmp_path: Path,
) -> None:
    end_metrics = BASE_END_METRICS.replace(
        (
            "propertyquarry_ingress_admission_operations_total"
            '{backend="postgres",operation="admit",'
            'outcome="backend_unavailable"} 0'
        ),
        (
            "propertyquarry_ingress_admission_operations_total"
            '{backend="postgres",operation="admit",'
            'outcome="backend_unavailable"} 1'
        ),
    )
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=end_metrics),
        runner=FakePromtoolRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert "ingress_admission_backend" in receipt["error"]["message"]


def test_unobserved_queue_fails_launch_snapshot(
    tmp_path: Path,
) -> None:
    end_metrics = BASE_END_METRICS.replace(
        'propertyquarry_queue_observed{queue="property_search"} 1',
        'propertyquarry_queue_observed{queue="property_search"} 0',
    )

    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=end_metrics),
        runner=FakePromtoolRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert "queue telemetry is unobserved" in receipt["error"]["message"]


def test_empty_queue_with_nonzero_age_fails_launch_snapshot(
    tmp_path: Path,
) -> None:
    end_metrics = BASE_END_METRICS.replace(
        'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} 0',
        'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} 1',
    )

    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=end_metrics),
        runner=FakePromtoolRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert "empty property search queue" in receipt["error"]["message"]


def test_ingress_backend_failure_fails_authenticated_range_slo(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.prometheus_range_path is not None
    assert config.prometheus_range_receipt_path is not None
    response = json.loads(
        config.prometheus_range_path.read_text(encoding="utf-8")
    )
    target = next(
        row
        for row in response["data"]["result"]
        if (
            row["metric"]["__name__"]
            == "propertyquarry_ingress_admission_operations_total"
            and row["metric"]["operation"] == "admit"
            and row["metric"]["outcome"] == "backend_unavailable"
        )
    )
    target["values"][-1][1] = "1"
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config.prometheus_range_path.write_bytes(raw)

    def rebind(payload: dict[str, object]) -> None:
        matrix = evidence._normalized_matrix(response["data"]["result"])
        payload["range_response_sha256"] = evidence.sha256_bytes(raw)
        payload["range_response_bytes"] = len(raw)
        series = payload["series"]
        assert isinstance(series, dict)
        series["sha256"] = evidence.canonical_json_sha256(matrix)

    _rewrite_hashed_json(config.prometheus_range_receipt_path, rebind)
    receipt, exit_code = evidence.run_evidence_gate(
        config=config,
        runner=FakePromtoolRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert "ingress_admission_backend" in receipt["error"]["message"]


def test_authenticated_range_requires_complete_ingress_admission_matrix(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.prometheus_range_path is not None
    assert config.prometheus_range_receipt_path is not None
    response = json.loads(
        config.prometheus_range_path.read_text(encoding="utf-8")
    )
    result = response["data"]["result"]
    removed = next(
        row
        for row in result
        if (
            row["metric"]["__name__"]
            == "propertyquarry_ingress_admission_operations_total"
            and row["metric"]["operation"] == "cleanup"
            and row["metric"]["outcome"] == "allowed"
        )
    )
    result.remove(removed)
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config.prometheus_range_path.write_bytes(raw)

    def rebind(payload: dict[str, object]) -> None:
        matrix = evidence._normalized_matrix(result)
        payload["range_response_sha256"] = evidence.sha256_bytes(raw)
        payload["range_response_bytes"] = len(raw)
        series = payload["series"]
        assert isinstance(series, dict)
        series["count"] = len(matrix)
        series["sha256"] = evidence.canonical_json_sha256(matrix)

    _rewrite_hashed_json(config.prometheus_range_receipt_path, rebind)
    receipt, exit_code = evidence.run_evidence_gate(
        config=config,
        runner=FakePromtoolRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert (
        "ingress admission operation matrix"
        in receipt["error"]["message"]
    )


def test_missing_range_proof_fails_before_monitoring_tools(tmp_path: Path) -> None:
    runner = FakePromtoolRunner()
    config = _config(tmp_path)
    config = evidence.EvidenceConfig(
        **{
            **config.__dict__,
            "prometheus_range_path": None,
            "prometheus_range_receipt_path": None,
        }
    )
    receipt, exit_code = evidence.run_evidence_gate(config=config, runner=runner, now=NOW)
    assert exit_code == 2
    assert "30-day Prometheus range" in receipt["error"]["message"]
    assert runner.calls == []


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("propertyquarry_readiness 1", "propertyquarry_readiness 0", "readiness"),
        (
            'propertyquarry_runtime_heartbeat_stale{role="scheduler"} 0',
            'propertyquarry_runtime_heartbeat_stale{role="scheduler"} 1',
            "required scheduler heartbeat",
        ),
    ],
)
def test_nonpassing_end_state_fails_before_tools(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    runner = FakePromtoolRunner()
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=BASE_END_METRICS.replace(old, new)),
        runner=runner,
        now=NOW,
    )
    assert exit_code == 2
    assert message in receipt["error"]["message"]
    assert runner.calls == []


def test_counter_reset_is_rejected(tmp_path: Path) -> None:
    end = BASE_END_METRICS.replace(
        'propertyquarry_http_requests_total{method="GET",route="/health",status_class="2xx"} 200',
        'propertyquarry_http_requests_total{method="GET",route="/health",status_class="2xx"} 99',
    )
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=end), runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "reset" in receipt["error"]["message"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda text: text.replace(
                'propertyquarry_http_request_duration_seconds_bucket{method="GET",route="/health",le="0.005"} 0\n',
                "",
            ),
            "exact finite buckets",
        ),
        (
            lambda text: text.replace(
                'propertyquarry_http_request_duration_seconds_count{method="GET",route="/health"} 200',
                'propertyquarry_http_request_duration_seconds_count{method="GET",route="/health"} 199',
            ),
            "count/sum is inconsistent",
        ),
        (
            lambda text: text.replace(
                'propertyquarry_http_request_duration_seconds_sum{method="GET",route="/health"} 20',
                'propertyquarry_http_request_duration_seconds_sum{method="GET",route="/health"} NaN',
            ),
            "count/sum is inconsistent",
        ),
    ],
)
def test_histograms_require_exact_finite_monotonic_bucket_count_sum_contract(
    tmp_path: Path, mutation: Callable[[str], str], message: str
) -> None:
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=mutation(BASE_END_METRICS)),
        runner=FakePromtoolRunner(),
        now=NOW,
    )
    assert exit_code == 2
    assert message in receipt["error"]["message"]


def test_runtime_build_identity_must_match_container_bound_probe(tmp_path: Path) -> None:
    end = BASE_END_METRICS.replace(f'replica_id="{REPLICA_ID}"', 'replica_id="forged"')
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path, end_metrics=end), runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "runtime build info diverges" in receipt["error"]["message"]


def test_short_snapshot_artifacts_must_be_distinct_and_hash_bound(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        replicas = payload["replicas"]
        assert isinstance(replicas, list) and isinstance(replicas[0], dict)
        replicas[0]["end"] = dict(replicas[0]["start"])

    _rewrite_hashed_json(config.metrics_snapshot_path, mutate)
    receipt, exit_code = evidence.run_evidence_gate(
        config=config, runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "bind the snapshot bundle" in receipt["error"]["message"]


def test_range_receipt_requires_30_days_tls_auth_and_exact_bindings(tmp_path: Path) -> None:
    short_config = _config(
        tmp_path / "short", range_window_seconds=evidence.PROMETHEUS_RANGE_WINDOW_SECONDS - 1
    )
    receipt, exit_code = evidence.run_evidence_gate(
        config=short_config, runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "30 days" in receipt["error"]["message"]

    tls_config = _config(tmp_path / "tls")

    def disable_tls(payload: dict[str, object]) -> None:
        transport = payload["transport"]
        assert isinstance(transport, dict)
        transport["tls_verified"] = False

    assert tls_config.prometheus_range_receipt_path is not None
    _rewrite_hashed_json(tls_config.prometheus_range_receipt_path, disable_tls)
    receipt, exit_code = evidence.run_evidence_gate(
        config=tls_config, runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "transport proof" in receipt["error"]["message"]

    binding_config = _config(tmp_path / "binding")

    def forge_binding(payload: dict[str, object]) -> None:
        replicas = payload["replicas"]
        assert isinstance(replicas, list) and isinstance(replicas[0], dict)
        replicas[0]["container_image_id"] = "sha256:" + "9" * 64

    assert binding_config.prometheus_range_receipt_path is not None
    _rewrite_hashed_json(binding_config.prometheus_range_receipt_path, forge_binding)
    receipt, exit_code = evidence.run_evidence_gate(
        config=binding_config, runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "bindings diverge" in receipt["error"]["message"]


def test_range_rejects_nan_and_counter_resets_even_with_rehashed_receipt(tmp_path: Path) -> None:
    for index, replacement in enumerate(("NaN", "-1")):
        config = _config(tmp_path / str(index))
        assert config.prometheus_range_path is not None
        assert config.prometheus_range_receipt_path is not None
        response = json.loads(config.prometheus_range_path.read_text(encoding="utf-8"))
        result = response["data"]["result"]
        target = next(
            row for row in result if row["metric"]["__name__"] == "propertyquarry_http_requests_total"
        )
        target["values"][-1][1] = replacement
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        config.prometheus_range_path.write_bytes(raw)

        def rebind(payload: dict[str, object]) -> None:
            matrix = evidence._normalized_matrix(response["data"]["result"])
            payload["range_response_sha256"] = evidence.sha256_bytes(raw)
            payload["range_response_bytes"] = len(raw)
            series = payload["series"]
            assert isinstance(series, dict)
            series["sha256"] = evidence.canonical_json_sha256(matrix)

        _rewrite_hashed_json(config.prometheus_range_receipt_path, rebind)
        receipt, exit_code = evidence.run_evidence_gate(
            config=config, runner=FakePromtoolRunner(), now=NOW
        )
        assert exit_code == 2
        assert "NaN, negative, or unordered" in receipt["error"]["message"]


def test_range_series_must_cover_full_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.prometheus_range_path is not None
    assert config.prometheus_range_receipt_path is not None
    response = json.loads(config.prometheus_range_path.read_text(encoding="utf-8"))
    for row in response["data"]["result"]:
        row["values"][0][0] += 86_400
    raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    config.prometheus_range_path.write_bytes(raw)

    def rebind(payload: dict[str, object]) -> None:
        matrix = evidence._normalized_matrix(response["data"]["result"])
        payload["range_response_sha256"] = evidence.sha256_bytes(raw)
        payload["range_response_bytes"] = len(raw)
        series = payload["series"]
        assert isinstance(series, dict)
        series["sha256"] = evidence.canonical_json_sha256(matrix)

    _rewrite_hashed_json(config.prometheus_range_receipt_path, rebind)
    receipt, exit_code = evidence.run_evidence_gate(
        config=config, runner=FakePromtoolRunner(), now=NOW
    )
    assert exit_code == 2
    assert "canonical query cadence" in receipt["error"]["message"]


def test_missing_tools_fail_flagship_but_remain_advisory_locally(tmp_path: Path) -> None:
    runner = FakePromtoolRunner(available=False)
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path / "flagship"), runner=runner, now=NOW
    )
    assert exit_code == 2
    assert "pinned monitoring tools are required" in receipt["error"]["message"]
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path / "advisory", flagship=False), runner=runner, now=NOW
    )
    assert exit_code == 0
    assert receipt["status"] == "advisory_unavailable"


def test_monitoring_tool_failure_redacts_raw_output(tmp_path: Path) -> None:
    secret = "monitoring-token-never-record"
    runner = FakePromtoolRunner(
        results={"test": evidence.CommandResult(7, stdout=secret, stderr=secret)}
    )
    receipt, exit_code = evidence.run_evidence_gate(
        config=_config(tmp_path), runner=runner, now=NOW
    )
    assert exit_code == 2
    assert secret not in json.dumps(receipt)
    assert "raw output was withheld" in receipt["error"]["message"]


def test_pinned_monitoring_runner_ignores_malicious_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted-promtool"
    trusted.write_bytes(b"trusted")
    trusted.chmod(0o500)
    metadata = trusted.lstat()
    identity = evidence.monitoring_proof.ToolIdentity(
        name="promtool",
        path=trusted,
        version="3.5.0",
        sha256=evidence.sha256_bytes(trusted.read_bytes()),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )
    malicious = tmp_path / "malicious"
    malicious.mkdir()
    (malicious / "promtool").write_text("malicious", encoding="utf-8")
    monkeypatch.setenv("PATH", str(malicious))
    monkeypatch.setattr(
        evidence.monitoring_proof,
        "assert_tool_identity",
        lambda actual: None,
    )
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return evidence.subprocess.CompletedProcess(argv, 0, "trusted", "")

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    runner = evidence.SubprocessPromtoolRunner(
        working_directory=tmp_path,
        tools={"promtool": identity},
    )
    result = runner.run(("promtool", "--version"), timeout_seconds=5)

    assert result.returncode == 0
    assert observed["argv"] == [str(trusted), "--version"]
    assert observed["env"] == {"LANG": "C", "LC_ALL": "C", "PATH": ""}


def test_one_immutable_fixture_passes_slo_observability_and_gold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    snapshot_raw = config.metrics_snapshot_path.read_bytes()
    snapshot_payload = json.loads(snapshot_raw)
    snapshot_sha256, bindings = receipts.validate_snapshot_bundle_identity(
        snapshot_payload,
        snapshot_raw,
        expected_commit_sha=RELEASE_SHA,
        expected_image_digest=IMAGE_DIGEST,
        challenge=AUTHORITY.challenge,
        now=NOW,
    )
    binding = bindings[REPLICA_ID]
    sent_at = NOW - timedelta(seconds=5)
    received_at = NOW - timedelta(seconds=2)
    alert = GATEWAY.acknowledgement(
        evidence_authority=AUTHORITY,
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        labels={"alertname": "PropertyQuarryReleaseProof"},
        sent_at=sent_at,
        delivered_at=received_at,
    )
    alert_path = tmp_path / "alert-delivery.json"
    alert_raw = _write_json(alert_path, alert)
    target = {
        **binding,
        "instance": "127.0.0.1:8090",
        "health": "up",
        "last_scrape_at": receipts.isoformat(NOW),
        "scrape_url_sha256": "7" * 64,
    }
    canonical_identity = CANONICAL.payload["identity"]
    assert isinstance(canonical_identity, Mapping)
    monitoring = AUTHORITY.authenticate(
        {
            "schema_version": receipts.MONITORING_SCHEMA,
            "producer": receipts.MONITORING_PRODUCER,
            "captured_at": receipts.isoformat(NOW),
            "release": {"commit_sha": RELEASE_SHA, "image_digest": IMAGE_DIGEST},
            "snapshot_bundle_sha256": snapshot_sha256,
            "identity": {
                **canonical_identity,
                "operator_gateway_trust_sha256": GATEWAY.trust.file_sha256,
                "operator_gateway_key_id_sha256": receipts.sha256_bytes(
                    GATEWAY.trust.key_id.encode()
                ),
                "operator_gateway_audience_sha256": receipts.sha256_bytes(
                    GATEWAY.trust.audience.encode()
                ),
            },
            "prometheus": {
                "loaded_config_sha256": canonical_identity[
                    "prometheus_config_sha256"
                ],
                "rules_sha256": canonical_identity["alert_rules_sha256"],
                "expected_replica_ids": [REPLICA_ID],
                "targets": [target],
            },
            "alertmanager": {
                "loaded_config_sha256": canonical_identity[
                    "alertmanager_config_sha256"
                ],
                "status": "ready",
                "proof_secret_configured": True,
            },
            "alert_delivery_receipt_sha256": receipts.sha256_bytes(alert_raw),
            "started_at": receipts.isoformat(NOW - timedelta(seconds=10)),
            "completed_at": receipts.isoformat(NOW),
        },
        domain=evidence_contract.MONITORING_DOMAIN,
    )
    monitoring_path = tmp_path / "monitoring.json"
    _write_json(monitoring_path, monitoring)
    dashboard_path = tmp_path / "dashboard.json"
    _write_json(dashboard_path, {"kind": "dashboard"})
    structured_logs_path = tmp_path / "structured-logs.json"
    _write_json(structured_logs_path, {"kind": "structured-logs"})
    distributed_trace_path = tmp_path / "distributed-trace.json"
    _write_json(distributed_trace_path, {"kind": "distributed-trace"})

    def fake_operations_verifier(**kwargs):  # type: ignore[no-untyped-def]
        shared_input_hashes = {
            "dashboard_render_receipt": receipts.sha256_bytes(
                kwargs["dashboard_render_receipt_path"].read_bytes()
            ),
            "structured_log_query_receipt": receipts.sha256_bytes(
                kwargs["structured_log_query_receipt_path"].read_bytes()
            ),
            "distributed_trace_query_receipt": receipts.sha256_bytes(
                kwargs["distributed_trace_query_receipt_path"].read_bytes()
            ),
        }
        assert shared_input_hashes == dict(kwargs["expected_input_hashes"])
        return receipts.add_payload_sha256(
            {
                "schema_version": receipts.OPERATIONS_VERIFICATION_SCHEMA,
                "producer": receipts.OPERATIONS_VERIFICATION_PRODUCER,
                "verified_at": receipts.isoformat(kwargs["now"]),
                "release": {
                    "commit_sha": kwargs["release_commit_sha"],
                    "image_digest": kwargs["release_image_digest"],
                },
                "deployment_id": AUTHORITY.challenge.deployment_id,
                "challenge_sha256": AUTHORITY.challenge.artifact_sha256,
                "policy_sha256": AUTHORITY.challenge.policy_hashes[
                    "flagship_operations_sha256"
                ],
                "source_contract_status": "defined_not_live_evidence",
                "shared_input_hashes": shared_input_hashes,
                "replica_ids": [REPLICA_ID],
                "status": "verified",
                "receipts": {
                    "dashboard_render": {
                        "file_sha256": shared_input_hashes[
                            "dashboard_render_receipt"
                        ],
                        "payload_sha256": "a" * 64,
                    },
                    "structured_log_query": {
                        "file_sha256": shared_input_hashes[
                            "structured_log_query_receipt"
                        ],
                        "payload_sha256": "b" * 64,
                    },
                    "distributed_trace_query": {
                        "file_sha256": shared_input_hashes[
                            "distributed_trace_query_receipt"
                        ],
                        "payload_sha256": "c" * 64,
                    },
                },
                "cross_receipt_links_verified": True,
            }
        )

    monkeypatch.setattr(
        receipts,
        "verify_operations_evidence",
        fake_operations_verifier,
    )

    immutable_files = [path for path in tmp_path.iterdir() if path.is_file()]
    original_hashes = {
        path: evidence.sha256_bytes(path.read_bytes()) for path in immutable_files
    }
    for path in immutable_files:
        path.chmod(0o400)

    slo_receipt, observability_receipt, _, _, errors = (
        gold_status._run_canonical_launch_validators(
            release_commit_sha=RELEASE_SHA,
            release_image_digest=IMAGE_DIGEST,
            metrics_snapshot_path=config.metrics_snapshot_path,
            metrics_probe_path=config.metrics_probe_path,
            monitoring_receipt_path=monitoring_path,
            prometheus_range_receipt_path=config.prometheus_range_receipt_path,
            prometheus_range_response_path=config.prometheus_range_path,
            alert_delivery_receipt_path=alert_path,
            dashboard_render_receipt_path=dashboard_path,
            structured_log_query_receipt_path=structured_logs_path,
            distributed_trace_query_receipt_path=distributed_trace_path,
            output_directory=tmp_path / "gold-revalidation",
            slo_runner=FakePromtoolRunner(),
            now=NOW,
            _test_allow_insecure_inputs=True,
        )
    )
    assert errors == []
    assert slo_receipt["gate_passed"] is True
    assert observability_receipt["status"] == "verified"

    gold_receipt: dict[str, object] = {
        "status": "pass",
        "ready_for_notification": True,
        "blockers": [],
        "pass_areas": [],
        "next_required_actions": [],
        "notes": [],
    }
    gold_status._apply_canonical_launch_evidence(
        gold_receipt,
        slo_receipt=slo_receipt,
        observability_receipt=observability_receipt,
        slo_receipt_path=tmp_path / "gold-revalidation" / "slo-revalidated.json",
        observability_receipt_path=tmp_path
        / "gold-revalidation"
        / "observability-revalidated.json",
        validation_errors=errors,
    )
    assert gold_receipt["status"] == "pass"
    assert gold_receipt["ready_for_notification"] is True
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in immutable_files)
    assert {
        path: evidence.sha256_bytes(path.read_bytes()) for path in immutable_files
    } == original_hashes


def test_versioned_rules_cover_every_slo_alert_and_runbook() -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    result = evidence.validate_rule_documents(
        evidence.DEFAULT_RULES_PATH,
        evidence.DEFAULT_RULE_TESTS_PATH,
        required_alerts=slo["required_alerts"],  # type: ignore[arg-type]
    )
    assert result["rule_alerts"] == sorted(slo["required_alerts"])
    assert result["injection_test_alerts"] == sorted(slo["required_alerts"])


def test_monitoring_contract_runbook_references_resolve() -> None:
    root = evidence.DEFAULT_SLO_PATH.parents[2]
    flagship = json.loads(
        (root / "config/monitoring/propertyquarry_flagship_operations.v1.json").read_text(
            encoding="utf-8"
        )
    )
    incident = json.loads(
        (root / "config/monitoring/propertyquarry_incident_support.v1.json").read_text(
            encoding="utf-8"
        )
    )
    rules = yaml.safe_load(evidence.DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    references = [
        *(panel["runbook"] for panel in flagship["panels"]),
        *incident["runbooks"],
        *(
            rule["annotations"]["runbook"]
            for group in rules["groups"]
            for rule in group["rules"]
            if "alert" in rule
        ),
    ]

    for reference in references:
        relative_path, separator, anchor = str(reference).partition("#")
        path = root / relative_path
        assert path.is_file(), reference
        if not separator:
            continue
        text = path.read_text(encoding="utf-8")
        explicit_anchors = set(
            re.findall(r"""<a\s+id=["']([^"']+)["']\s*></a>""", text, re.IGNORECASE)
        )
        heading_anchors = {
            re.sub(r"-+", "-", re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-"))
            for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)
        }
        assert anchor in explicit_anchors | heading_anchors, reference

    assert flagship["source_contract_status"] == "defined_not_live_evidence"


def test_required_metric_families_are_emitted_by_canonical_runtime_exporter(
    tmp_path: Path,
) -> None:
    worker_path = tmp_path / "worker-heartbeat.json"
    _write_json(
        worker_path,
        {
            "role": "worker",
            "epoch": NOW.timestamp(),
            "pid": 10,
            "property_search_work_queue": {
                "observed": True,
                "depth": 0,
                "oldest_item_age_seconds": 0.0,
            },
        },
    )
    registry = RuntimeMetrics()
    registry.record_request(
        method="GET",
        route="/health",
        status_code=200,
        duration_seconds=0.02,
    )
    rendered = registry.render_prometheus(
        readiness_ready=True,
        environ={
            "EA_WORKER_HEARTBEAT_PATH": str(worker_path),
            "EA_SCHEDULER_HEARTBEAT_PATH": str(tmp_path / "missing-scheduler.json"),
        },
        now_epoch=NOW.timestamp(),
    )
    families, _samples = evidence.parse_metrics_snapshot(rendered)
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )

    assert sorted(set(slo["required_metric_families"]) - set(families)) == []


def test_slo_requires_worker_and_scheduler_baseline_heartbeats() -> None:
    slo = json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    heartbeat_objective = next(
        row for row in slo["objectives"] if row["id"] == "runtime_heartbeats"
    )
    heartbeat_objective["baseline_required_roles"] = ["scheduler"]

    with pytest.raises(
        evidence.SloValidationError,
        match="baseline roles must uniquely include worker and scheduler",
    ):
        evidence.validate_slo_document(slo)


@pytest.mark.parametrize(
    "validator",
    (evidence.validate_metrics, evidence._validate_end_state),
)
def test_runtime_heartbeat_baseline_roles_drive_metrics_validation(
    validator: Callable[..., object],
) -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(BASE_END_METRICS)
    worker_optional = [
        evidence.MetricSample(sample.name, sample.labels, 0.0)
        if sample.name == "propertyquarry_runtime_heartbeat_required"
        and sample.labels.get("role") == "worker"
        else sample
        for sample in samples
    ]

    with pytest.raises(
        evidence.SloValidationError,
        match="worker heartbeat must remain required",
    ):
        validator(
            families=families,
            samples=worker_optional,
            slo=slo,
        )


@pytest.mark.parametrize(
    "validator",
    (evidence.validate_metrics, evidence._validate_end_state),
)
def test_property_search_queue_samples_are_required_and_finite(
    validator: Callable[..., object],
) -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(
        BASE_END_METRICS.replace(
            'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} 0',
            'propertyquarry_queue_oldest_item_age_seconds{queue="property_search"} NaN',
        )
    )

    with pytest.raises(
        evidence.SloValidationError,
        match=r"conditional capability queue_backlog has (?:no finite|invalid) samples",
    ):
        validator(families=families, samples=samples, slo=slo)


@pytest.mark.parametrize(
    "validator",
    (evidence.validate_metrics, evidence._validate_end_state),
)
def test_queue_capability_requires_exact_property_search_samples(
    validator: Callable[..., object],
) -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(
        BASE_END_METRICS.replace(
            'propertyquarry_queue_depth{queue="property_search"} 0',
            'propertyquarry_queue_depth{queue="other"} 0',
        )
    )

    with pytest.raises(
        evidence.SloValidationError,
        match=(
            "queue capability must expose one finite non-negative "
            "property_search sample for propertyquarry_queue_depth"
        ),
    ):
        validator(families=families, samples=samples, slo=slo)


@pytest.mark.parametrize(
    "validator",
    (evidence.validate_metrics, evidence._validate_end_state),
)
def test_property_search_queue_depth_must_be_an_integer(
    validator: Callable[..., object],
) -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(
        BASE_END_METRICS.replace(
            'propertyquarry_queue_depth{queue="property_search"} 0',
            'propertyquarry_queue_depth{queue="property_search"} 0.5',
        )
    )

    with pytest.raises(
        evidence.SloValidationError,
        match="property search queue depth must be an integer",
    ):
        validator(families=families, samples=samples, slo=slo)


def test_required_ingress_metric_types_fail_closed() -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(
        BASE_END_METRICS.replace(
            "# TYPE propertyquarry_ingress_rejections_total counter",
            "# TYPE propertyquarry_ingress_rejections_total gauge",
        )
    )

    with pytest.raises(
        evidence.SloValidationError,
        match="propertyquarry_ingress_rejections_total:gauge",
    ):
        evidence._validate_end_state(
            families=families,
            samples=samples,
            slo=slo,
        )


@pytest.mark.parametrize(
    ("mutated", "error"),
    (
        (
            BASE_END_METRICS.replace(
                'propertyquarry_admission_capacity_contract_valid{backend="postgres"} 1',
                'propertyquarry_admission_capacity_contract_valid{backend="postgres"} 0',
            ),
            "capacity contract must be exactly valid",
        ),
        (
            BASE_END_METRICS.replace(
                'propertyquarry_admission_capacity_limit{backend="postgres",capacity_key="lease"} 100000',
                'propertyquarry_admission_capacity_limit{backend="postgres",capacity_key="lease"} 99999',
            ),
            "capacity limits differ",
        ),
        (
            BASE_END_METRICS.replace(
                'propertyquarry_ingress_admission_operations_total{backend="postgres",operation="cleanup",outcome="allowed"} 0\n',
                "",
            ),
            "operation label matrix is incomplete",
        ),
        (
            BASE_END_METRICS.replace(
                'backend="postgres",operation="admit",outcome="allowed"',
                'backend="memory",operation="admit",outcome="allowed"',
            ),
            "operation sample is invalid",
        ),
    ),
)
def test_authoritative_ingress_admission_metrics_fail_closed(
    mutated: str,
    error: str,
) -> None:
    slo = evidence.validate_slo_document(
        json.loads(evidence.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
    )
    families, samples = evidence.parse_metrics_snapshot(mutated)

    with pytest.raises(evidence.SloValidationError, match=error):
        evidence._validate_end_state(
            families=families,
            samples=samples,
            slo=slo,
        )


def test_runtime_exporter_emits_candidate_build_and_exact_histogram_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = {
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": RELEASE_SHA,
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": IMAGE_DIGEST,
        "PROPERTYQUARRY_EXPECTED_API_REPLICAS": "1",
        "PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED": "0",
        "PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED": "0",
    }
    assert runtime_build_identity(environ, hostname=REPLICA_ID) == {
        "release_commit_sha": RELEASE_SHA,
        "release_image_digest": IMAGE_DIGEST,
        "replica_id": REPLICA_ID,
    }
    assert runtime_build_identity({}, hostname="unsafe replica") == {
        "release_commit_sha": "",
        "release_image_digest": "",
        "replica_id": "",
    }
    registry = RuntimeMetrics()
    monkeypatch.setattr(observability.socket, "gethostname", lambda: REPLICA_ID)
    registry.record_request(
        method="GET", route="/health", status_code=200, duration_seconds=0.02
    )
    rendered = registry.render_prometheus(
        readiness_ready=True,
        environ=environ,
        now_epoch=NOW.timestamp(),
    )
    families, samples = evidence.parse_metrics_snapshot(rendered)
    assert families["propertyquarry_runtime_build_info"] == "gauge"
    build_rows = evidence.samples_for(samples, "propertyquarry_runtime_build_info")
    assert len(build_rows) == 1
    assert build_rows[0].value == 1
    assert build_rows[0].labels == {
        "release_commit_sha": RELEASE_SHA,
        "release_image_digest": IMAGE_DIGEST,
        "replica_id": REPLICA_ID,
    }
    evidence._validate_histogram_contract(
        samples, "propertyquarry_http_request_duration_seconds"
    )
