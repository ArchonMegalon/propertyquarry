#!/usr/bin/env python3
"""Offline launch evidence gate for PropertyQuarry SLOs and alert rules.

This tool never contacts Prometheus or Alertmanager. It validates a previously
captured authenticated metrics snapshot and its provenance receipt, then uses
preinstalled promtool commands to syntax-check the versioned rules and inject
synthetic series through Prometheus rule-unit tests.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

if __package__:
    from scripts import propertyquarry_evidence_contract as evidence_contract
    from scripts import propertyquarry_monitoring_runtime_proof as monitoring_proof
else:
    import propertyquarry_evidence_contract as evidence_contract
    import propertyquarry_monitoring_runtime_proof as monitoring_proof


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SLO_PATH = APP_ROOT / "config" / "monitoring" / "propertyquarry_slo.v1.json"
DEFAULT_RULES_PATH = APP_ROOT / "config" / "monitoring" / "propertyquarry_alert_rules.v1.yml"
DEFAULT_RULE_TESTS_PATH = (
    APP_ROOT / "config" / "monitoring" / "propertyquarry_alert_rule_tests.v1.yml"
)
DEFAULT_PROMETHEUS_CONFIG_PATH = (
    APP_ROOT / "config" / "monitoring" / "propertyquarry_prometheus.v1.yml"
)
DEFAULT_ALERTMANAGER_CONFIG_PATH = (
    APP_ROOT / "config" / "monitoring" / "propertyquarry_alertmanager.v1.yml"
)
RECEIPT_SCHEMA = "propertyquarry.slo_evidence_receipt.v2"
PROBE_SCHEMA = "propertyquarry.metrics_probe.v2"
PROBE_BUNDLE_SCHEMA = "propertyquarry.metrics_probe_bundle.v2"
SNAPSHOT_BUNDLE_SCHEMA = "propertyquarry.metrics_snapshot_bundle.v2"
RANGE_RECEIPT_SCHEMA = evidence_contract.RANGE_RECEIPT_SCHEMA
RANGE_RECEIPT_PRODUCER = evidence_contract.RANGE_RECEIPT_PRODUCER
SLO_SCHEMA = "propertyquarry.slo.v1"
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
REPLICA_ID_RE = re.compile(r"^[a-zA-Z0-9._:-]{1,128}$")
METRIC_NAME_RE = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
TYPE_RE = re.compile(rf"^#\s+TYPE\s+({METRIC_NAME_RE})\s+([a-zA-Z]+)\s*$")
SAMPLE_RE = re.compile(
    rf"^({METRIC_NAME_RE})(\{{(?P<labels>[^}}]*)\}})?\s+(?P<value>[^\s]+)(?:\s+[0-9]+)?\s*$"
)
ALERT_RE = re.compile(r"^\s*-\s+alert:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*$", re.MULTILINE)
ALERT_TEST_RE = re.compile(
    r"^\s*alertname:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*$", re.MULTILINE
)
RULE_FILE_RE = re.compile(
    r"^\s*-\s+['\"]?([^'\"#\s]+\.ya?ml)['\"]?\s*$", re.MULTILINE
)
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    math.inf,
)
PROMETHEUS_RANGE_WINDOW_SECONDS = evidence_contract.RANGE_WINDOW_SECONDS
PROMETHEUS_RANGE_QUERY = evidence_contract.PROMETHEUS_RANGE_QUERY
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


class SloEvidenceError(RuntimeError):
    """Base SLO evidence failure."""


class SloValidationError(SloEvidenceError):
    """An input, series, rule, or probe contract is invalid."""


class PromtoolError(SloEvidenceError):
    """Offline Prometheus rule validation or injection failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class PromtoolRunner(Protocol):
    def available(self, tool: str = "promtool") -> bool: ...

    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> CommandResult: ...


class SubprocessPromtoolRunner:
    def __init__(
        self,
        *,
        working_directory: Path,
        tools: Mapping[str, monitoring_proof.ToolIdentity],
    ) -> None:
        self._working_directory = working_directory
        self._tools = dict(tools)

    def available(self, tool: str = "promtool") -> bool:
        return tool in self._tools

    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        if not argv or argv[0] not in self._tools:
            raise PromtoolError("monitoring command did not select a pinned tool")
        identity = self._tools[argv[0]]
        try:
            monitoring_proof.assert_tool_identity(identity)
        except monitoring_proof.MonitoringProofError as exc:
            raise PromtoolError(str(exc)) from exc
        pinned_argv = [str(identity.path), *argv[1:]]
        try:
            completed = subprocess.run(
                pinned_argv,
                cwd=self._working_directory,
                env={"LANG": "C", "LC_ALL": "C", "PATH": ""},
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise PromtoolError(
                f"promtool command timed out after {timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise PromtoolError("could not start preinstalled promtool") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class EvidenceConfig:
    release_commit_sha: str
    release_image_digest: str
    metrics_snapshot_path: Path
    metrics_probe_path: Path
    slo_path: Path
    rules_path: Path
    rule_tests_path: Path
    receipt_path: Path
    flagship: bool
    prometheus_range_path: Path | None = None
    prometheus_range_receipt_path: Path | None = None
    prometheus_config_path: Path = DEFAULT_PROMETHEUS_CONFIG_PATH
    alertmanager_config_path: Path = DEFAULT_ALERTMANAGER_CONFIG_PATH
    tool_manifest_path: Path = monitoring_proof.DEFAULT_TOOL_MANIFEST_PATH
    timeout_seconds: int = 120
    overwrite_receipt: bool = False
    shared_input_hashes: Mapping[str, str] | None = None
    shared_input_paths: Mapping[str, Path] | None = None


@dataclass(frozen=True)
class MetricSample:
    name: str
    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True)
class ReplicaSnapshotPair:
    replica_id: str
    container_id: str
    container_image_id: str
    release_commit_sha: str
    release_image_digest: str
    start_at: datetime
    end_at: datetime
    start_path: Path
    end_path: Path
    start_sha256: str
    end_sha256: str
    start_snapshot: bytes
    end_snapshot: bytes

    def range_binding(self) -> dict[str, str]:
        return {
            "replica_id": self.replica_id,
            "container_id": self.container_id,
            "container_image_id": self.container_image_id,
            "release_commit_sha": self.release_commit_sha,
            "release_image_digest": self.release_image_digest,
            "start_snapshot_sha256": self.start_sha256,
            "end_snapshot_sha256": self.end_sha256,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(raw: object, *, field_name: str) -> datetime:
    value = str(raw or "").strip()
    if not value:
        raise SloValidationError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SloValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SloValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_release_sha(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(value):
        raise SloValidationError("release commit must be a full 40-character Git SHA")
    return value


def normalize_image_digest(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not IMAGE_DIGEST_RE.fullmatch(value):
        raise SloValidationError("release image digest must be sha256:<64 lowercase hex>")
    return value


def positive_int(raw: object, *, field_name: str, default: int) -> int:
    value = str(raw or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) <= 0:
        raise SloValidationError(f"{field_name} must be a positive integer")
    return int(value)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"path": str(path), "bytes": len(payload), "sha256": sha256_bytes(payload)}


def output_evidence(value: str) -> dict[str, object]:
    payload = str(value or "").encode("utf-8", errors="replace")
    return {"bytes": len(payload), "sha256": sha256_bytes(payload)}


def atomic_write_json(path: Path, payload: object, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not overwrite:
        raise SloValidationError(
            f"receipt already exists: {path}; choose a new path or use --overwrite-receipt"
        )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_json(path: Path, *, document_name: str) -> object:
    if not path.is_file():
        raise SloValidationError(f"{document_name} is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SloValidationError(f"{document_name} is not valid JSON") from exc


def _is_exact_json_number(value: object, expected: int | float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value == expected
    )


def validate_slo_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema") != SLO_SCHEMA:
        raise SloValidationError(f"SLO schema must be {SLO_SCHEMA}")
    if payload.get("version") != 1 or payload.get("service") != "propertyquarry":
        raise SloValidationError("SLO document must identify PropertyQuarry version 1")
    required_metrics = payload.get("required_metric_families")
    required_alerts = payload.get("required_alerts")
    conditional = payload.get("conditional_capabilities")
    objectives = payload.get("objectives")
    toolchain = payload.get("monitoring_toolchain")
    if not isinstance(required_metrics, list) or not required_metrics:
        raise SloValidationError("SLO required_metric_families must be a non-empty list")
    if not isinstance(required_alerts, list) or not required_alerts:
        raise SloValidationError("SLO required_alerts must be a non-empty list")
    if len(set(required_metrics)) != len(required_metrics) or len(set(required_alerts)) != len(
        required_alerts
    ):
        raise SloValidationError("SLO required metric and alert names must be unique")
    admission_metrics = {
        "propertyquarry_ingress_admission_operations_total",
        "propertyquarry_admission_capacity_contract_valid",
        "propertyquarry_admission_capacity_row_count",
        "propertyquarry_admission_capacity_limit",
    }
    admission_alerts = {
        "PropertyQuarryIngressAdmissionBackendUnavailable",
        "PropertyQuarryAdmissionCapacityContractInvalid",
        "PropertyQuarryAdmissionCapacityWarning",
        "PropertyQuarryAdmissionCapacityCritical",
    }
    queue_metrics = {
        "propertyquarry_queue_observed",
        "propertyquarry_queue_depth",
        "propertyquarry_queue_oldest_item_age_seconds",
    }
    queue_alerts = {
        "PropertyQuarryQueueTelemetryUnobserved",
        "PropertyQuarryQueueDepthHigh",
        "PropertyQuarryQueueOldestItemStale",
    }
    if not admission_metrics.issubset(set(required_metrics)):
        raise SloValidationError(
            "SLO document is missing an authoritative admission metric"
        )
    if not admission_alerts.issubset(set(required_alerts)):
        raise SloValidationError(
            "SLO document is missing an authoritative admission alert"
        )
    if not queue_metrics.issubset(set(required_metrics)):
        raise SloValidationError(
            "SLO document is missing an authoritative queue metric"
        )
    if not queue_alerts.issubset(set(required_alerts)):
        raise SloValidationError(
            "SLO document is missing an authoritative queue alert"
        )
    if not isinstance(conditional, dict):
        raise SloValidationError("SLO conditional_capabilities must be an object")
    queue_capability = conditional.get("queue_backlog")
    if (
        not isinstance(queue_capability, dict)
        or queue_capability.get("metric_families")
        != [
            "propertyquarry_queue_observed",
            "propertyquarry_queue_depth",
            "propertyquarry_queue_oldest_item_age_seconds",
        ]
        or not _is_exact_json_number(
            queue_capability.get("target_observed"),
            1,
        )
        or not _is_exact_json_number(
            queue_capability.get("warning_depth"),
            100,
        )
        or not _is_exact_json_number(
            queue_capability.get("critical_depth"),
            500,
        )
        or not _is_exact_json_number(
            queue_capability.get("maximum_oldest_age_seconds"),
            300,
        )
        or queue_capability.get("runbook_anchor") != "queue-backlog"
    ):
        raise SloValidationError(
            "SLO queue-backlog capability is not canonical"
        )
    if not isinstance(objectives, list) or not objectives:
        raise SloValidationError("SLO objectives must be a non-empty list")
    objective_ids = {
        str(row.get("id") or "")
        for row in objectives
        if isinstance(row, dict)
    }
    required_objectives = {
        "availability",
        "error_rate",
        "ingress_admission_backend",
        "ingress_admission_capacity",
        "latency_p95_seconds",
        "latency_p99_seconds",
        "provider_quota_failures",
    }
    if not required_objectives.issubset(objective_ids):
        raise SloValidationError("SLO document is missing a launch-evaluated objective")
    admission_backend_objectives = [
        row
        for row in objectives
        if isinstance(row, dict) and row.get("id") == "ingress_admission_backend"
    ]
    if len(admission_backend_objectives) != 1:
        raise SloValidationError(
            "SLO document must define one ingress-admission-backend objective"
        )
    admission_backend_objective = admission_backend_objectives[0]
    if (
        not _is_exact_json_number(
            admission_backend_objective.get("target_rate"),
            0,
        )
        or admission_backend_objective.get("indicator")
        != (
            "increase(propertyquarry_ingress_admission_operations_total"
            '{outcome="backend_unavailable"}[5m])'
        )
        or admission_backend_objective.get("runbook_anchor")
        != "ingress-admission-backend"
    ):
        raise SloValidationError(
            "SLO ingress-admission-backend objective is not canonical"
        )
    admission_capacity_objectives = [
        row
        for row in objectives
        if isinstance(row, dict) and row.get("id") == "ingress_admission_capacity"
    ]
    if len(admission_capacity_objectives) != 1:
        raise SloValidationError(
            "SLO document must define one ingress-admission-capacity objective"
        )
    admission_capacity_objective = admission_capacity_objectives[0]
    hard_limits = admission_capacity_objective.get("hard_limits")
    hard_limits_are_canonical = (
        isinstance(hard_limits, dict)
        and set(hard_limits) == {"lease", "quota"}
        and type(hard_limits["lease"]) is int
        and type(hard_limits["quota"]) is int
        and hard_limits["lease"] == 100_000
        and hard_limits["quota"] == 1_000_000
    )
    if (
        not _is_exact_json_number(
            admission_capacity_objective.get("target_contract_valid"),
            1,
        )
        or not _is_exact_json_number(
            admission_capacity_objective.get("warning_ratio"),
            0.8,
        )
        or not _is_exact_json_number(
            admission_capacity_objective.get("critical_ratio"),
            0.95,
        )
        or not hard_limits_are_canonical
        or admission_capacity_objective.get("indicator")
        != (
            "propertyquarry_admission_capacity_row_count / "
            "propertyquarry_admission_capacity_limit"
        )
        or admission_capacity_objective.get("runbook_anchor")
        != "ingress-admission-capacity"
    ):
        raise SloValidationError(
            "SLO ingress-admission-capacity objective is not canonical"
        )
    heartbeat_objective = next(
        (
            row
            for row in objectives
            if isinstance(row, dict) and row.get("id") == "runtime_heartbeats"
        ),
        None,
    )
    if heartbeat_objective is None:
        raise SloValidationError("SLO document is missing the runtime-heartbeats objective")
    baseline_roles = heartbeat_objective.get("baseline_required_roles")
    if (
        not isinstance(baseline_roles, list)
        or not baseline_roles
        or any(
            not isinstance(role, str) or role not in {"worker", "scheduler"}
            for role in baseline_roles
        )
        or len(set(baseline_roles)) != len(baseline_roles)
        or set(baseline_roles) != {"worker", "scheduler"}
    ):
        raise SloValidationError(
            "SLO runtime-heartbeats baseline roles must uniquely include worker and scheduler"
        )
    if (
        heartbeat_objective.get("conditional_roles_from_metric")
        != "propertyquarry_runtime_heartbeat_required"
    ):
        raise SloValidationError(
            "SLO runtime-heartbeats objective must use the canonical required-role metric"
        )
    if not isinstance(toolchain, dict):
        raise SloValidationError("SLO monitoring_toolchain must pin promtool and amtool")
    for tool in ("promtool", "amtool"):
        version = str(toolchain.get(f"{tool}_version") or "").strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise SloValidationError(f"SLO must pin an exact {tool} semantic version")
    for name, capability in conditional.items():
        if not isinstance(capability, dict):
            raise SloValidationError(f"conditional capability {name} must be an object")
        families = capability.get("metric_families")
        if not isinstance(families, list) or len(families) < 2:
            raise SloValidationError(
                f"conditional capability {name} must declare at least two metric families"
            )
    return payload


def validate_rule_documents(
    rules_path: Path,
    tests_path: Path,
    *,
    required_alerts: Sequence[str],
) -> dict[str, object]:
    if not rules_path.is_file():
        raise SloValidationError(f"Prometheus rule file is missing: {rules_path}")
    if not tests_path.is_file():
        raise SloValidationError(f"Prometheus rule-test file is missing: {tests_path}")
    rules_text = rules_path.read_text(encoding="utf-8")
    tests_text = tests_path.read_text(encoding="utf-8")
    rule_file_header = tests_text.split("evaluation_interval:", 1)[0]
    declared_rule_files = RULE_FILE_RE.findall(rule_file_header)
    if (
        declared_rule_files != [rules_path.name]
        or rules_path.parent.resolve() != tests_path.parent.resolve()
    ):
        raise SloValidationError(
            "Prometheus injection tests must reference the exact adjacent rule file"
        )
    rule_alerts = ALERT_RE.findall(rules_text)
    test_alerts = ALERT_TEST_RE.findall(tests_text)
    if len(rule_alerts) != len(set(rule_alerts)):
        raise SloValidationError("Prometheus rule file contains duplicate alert names")
    missing_rules = sorted(set(required_alerts) - set(rule_alerts))
    missing_tests = sorted(set(required_alerts) - set(test_alerts))
    if missing_rules:
        raise SloValidationError(f"required Prometheus alerts are missing: {missing_rules}")
    if missing_tests:
        raise SloValidationError(
            f"required alerts lack synthetic injection tests: {missing_tests}"
        )
    for alert in required_alerts:
        start_match = re.search(rf"^\s*-\s+alert:\s*{re.escape(alert)}\s*$", rules_text, re.MULTILINE)
        assert start_match is not None
        next_rule = re.search(
            r"^\s*-\s+(?:alert|record):",
            rules_text[start_match.end() :],
            re.MULTILINE,
        )
        end = start_match.end() + next_rule.start() if next_rule else len(rules_text)
        if "runbook:" not in rules_text[start_match.start() : end]:
            raise SloValidationError(f"alert {alert} has no incident runbook annotation")
    return {
        "rule_alerts": sorted(rule_alerts),
        "injection_test_alerts": sorted(set(test_alerts)),
        "rules": file_identity(rules_path),
        "tests": file_identity(tests_path),
    }


def validate_monitoring_configs(
    prometheus_config_path: Path,
    alertmanager_config_path: Path,
    *,
    rules_path: Path,
) -> dict[str, object]:
    if not prometheus_config_path.is_file():
        raise SloValidationError(
            f"versioned Prometheus config is missing: {prometheus_config_path}"
        )
    if not alertmanager_config_path.is_file():
        raise SloValidationError(
            f"versioned Alertmanager config is missing: {alertmanager_config_path}"
        )
    prometheus_text = prometheus_config_path.read_text(encoding="utf-8")
    alertmanager_text = alertmanager_config_path.read_text(encoding="utf-8")
    prometheus_requirements = {
        "propertyquarry_job": re.search(
            r"^\s*-\s+job_name:\s*propertyquarry\s*$", prometheus_text, re.MULTILINE
        ),
        "private_metrics_path": re.search(
            r"^\s+metrics_path:\s*/internal/metrics\s*$", prometheus_text, re.MULTILINE
        ),
        "bearer_credentials_file": re.search(
            r"^\s+credentials_file:\s*/run/secrets/propertyquarry_metrics_token\s*$",
            prometheus_text,
            re.MULTILINE,
        ),
        "per_replica_file_discovery": (
            "file_sd_configs:" in prometheus_text
            and re.search(
                r"^\s+-\s+/etc/prometheus/propertyquarry_targets\.json\s*$",
                prometheus_text,
                re.MULTILINE,
            )
        ),
        "service_label": re.search(
            r"^\s+replacement:\s*propertyquarry\s*$", prometheus_text, re.MULTILINE
        ),
        "alertmanager_delivery": re.search(
            r"^\s+-\s+propertyquarry-alertmanager:9093\s*$",
            prometheus_text,
            re.MULTILINE,
        ),
    }
    missing_prometheus = sorted(
        name for name, matched in prometheus_requirements.items() if not matched
    )
    if missing_prometheus:
        raise SloValidationError(
            "Prometheus config is missing private scrape contract(s): "
            + ", ".join(missing_prometheus)
        )
    if re.search(r"^\s+credentials:\s*\S+", prometheus_text, re.MULTILINE):
        raise SloValidationError("Prometheus config must not embed a bearer credential")
    declared_rules = RULE_FILE_RE.findall(prometheus_text.split("scrape_configs:", 1)[0])
    if (
        declared_rules != [rules_path.name]
        or prometheus_config_path.parent.resolve() != rules_path.parent.resolve()
    ):
        raise SloValidationError(
            "Prometheus config must load the exact adjacent PropertyQuarry rule file"
        )

    alertmanager_requirements = {
        "propertyquarry_matcher": 'service="propertyquarry"' in alertmanager_text,
        "operator_receiver": re.search(
            r"^\s*-?\s*receiver:\s*propertyquarry-operator\s*$",
            alertmanager_text,
            re.MULTILINE,
        ),
        "receiver_definition": re.search(
            r"^\s*-\s+name:\s*propertyquarry-operator\s*$",
            alertmanager_text,
            re.MULTILINE,
        ),
        "secret_backed_webhook": re.search(
            r"^\s+-?\s*url_file:\s*/run/secrets/propertyquarry_alert_webhook_url\s*$",
            alertmanager_text,
            re.MULTILINE,
        ),
        "resolved_delivery": re.search(
            r"^\s+send_resolved:\s*true\s*$", alertmanager_text, re.MULTILINE
        ),
    }
    missing_routing = sorted(
        name for name, matched in alertmanager_requirements.items() if not matched
    )
    if missing_routing:
        raise SloValidationError(
            "Alertmanager config is missing fail-closed routing contract(s): "
            + ", ".join(missing_routing)
        )
    if re.search(r"^\s+url:\s*\S+", alertmanager_text, re.MULTILINE):
        raise SloValidationError("Alertmanager config must not embed a webhook URL")
    return {
        "prometheus_config": file_identity(prometheus_config_path),
        "alertmanager_config": file_identity(alertmanager_config_path),
        "private_authenticated_scrape": True,
        "per_replica_discovery": True,
        "rule_file_loaded": rules_path.name,
        "propertyquarry_alert_route": "propertyquarry-operator",
    }


def parse_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not raw.strip():
        return labels
    position = 0
    pattern = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"(?:,|$)')
    while position < len(raw):
        match = pattern.match(raw, position)
        if match is None or match.group(1) in labels:
            raise SloValidationError("metrics snapshot contains malformed or duplicate labels")
        labels[match.group(1)] = (
            match.group(2).replace(r"\n", "\n").replace(r'\"', '"').replace(r"\\", "\\")
        )
        position = match.end()
    return labels


def parse_metrics_snapshot(text: str) -> tuple[dict[str, str], list[MetricSample]]:
    families: dict[str, str] = {}
    samples: list[MetricSample] = []
    seen_samples: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        type_match = TYPE_RE.match(line)
        if type_match:
            name, metric_type = type_match.groups()
            if name in families and families[name] != metric_type:
                raise SloValidationError(f"metric family {name} has conflicting TYPE lines")
            families[name] = metric_type
            continue
        if line.startswith("#"):
            continue
        sample_match = SAMPLE_RE.match(line)
        if not sample_match:
            raise SloValidationError(f"metrics snapshot line {line_number} is not valid exposition")
        name = sample_match.group(1)
        labels = parse_labels(sample_match.group("labels") or "")
        try:
            value = float(sample_match.group("value"))
        except ValueError as exc:
            raise SloValidationError(
                f"metrics snapshot line {line_number} has an invalid value"
            ) from exc
        key = (name, tuple(sorted(labels.items())))
        if key in seen_samples:
            raise SloValidationError(f"metrics snapshot contains duplicate sample {name}")
        seen_samples.add(key)
        samples.append(MetricSample(name=name, labels=labels, value=value))
    return families, samples


def samples_for(samples: Sequence[MetricSample], name: str) -> list[MetricSample]:
    return [sample for sample in samples if sample.name == name]


def single_role_sample(
    samples: Sequence[MetricSample], name: str, role: str
) -> MetricSample:
    matches = [
        sample
        for sample in samples
        if sample.name == name and sample.labels.get("role") == role
    ]
    if len(matches) != 1:
        raise SloValidationError(f"metric {name} must expose exactly one {role} sample")
    return matches[0]


def objective(slo: Mapping[str, object], objective_id: str) -> Mapping[str, object]:
    for row in slo.get("objectives", []):
        if isinstance(row, dict) and row.get("id") == objective_id:
            return row
    raise SloValidationError(f"SLO objective {objective_id} is missing")


def objective_number(
    slo: Mapping[str, object], objective_id: str, field_name: str
) -> float:
    raw = objective(slo, objective_id).get(field_name)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SloValidationError(
            f"SLO objective {objective_id} must declare numeric {field_name}"
        ) from exc
    if not math.isfinite(value):
        raise SloValidationError(
            f"SLO objective {objective_id} has non-finite {field_name}"
        )
    return value


def histogram_quantile(
    samples: Sequence[MetricSample], family: str, quantile: float
) -> float:
    bucket_name = f"{family}_bucket"
    buckets: dict[float, float] = defaultdict(float)
    for sample in samples_for(samples, bucket_name):
        raw_bound = str(sample.labels.get("le") or "").strip()
        try:
            bound = math.inf if raw_bound in {"+Inf", "Inf"} else float(raw_bound)
        except ValueError as exc:
            raise SloValidationError(f"histogram {family} has an invalid le label") from exc
        if math.isnan(bound) or not math.isfinite(sample.value) or sample.value < 0:
            raise SloValidationError(f"histogram {family} has an invalid bucket value")
        buckets[bound] += sample.value
    if math.inf not in buckets or buckets[math.inf] <= 0:
        raise SloValidationError(f"histogram {family} has no observed requests")
    ordered = sorted(buckets.items(), key=lambda row: row[0])
    previous_count = 0.0
    for bound, cumulative_count in ordered:
        if cumulative_count < previous_count:
            raise SloValidationError(f"histogram {family} buckets are not cumulative")
        previous_count = cumulative_count
    rank = quantile * buckets[math.inf]
    lower_count = 0.0
    lower_bound = 0.0
    for upper_bound, cumulative_count in ordered:
        if cumulative_count < rank:
            lower_count = cumulative_count
            if math.isfinite(upper_bound):
                lower_bound = upper_bound
            continue
        if math.isinf(upper_bound):
            return lower_bound
        bucket_count = cumulative_count - lower_count
        if bucket_count <= 0:
            return upper_bound
        position = (rank - lower_count) / bucket_count
        return lower_bound + (upper_bound - lower_bound) * position
    raise SloValidationError(f"histogram {family} quantile could not be evaluated")


def validate_current_slo_values(
    *, samples: Sequence[MetricSample], slo: Mapping[str, object]
) -> dict[str, object]:
    request_samples = samples_for(samples, "propertyquarry_http_requests_total")
    if not request_samples or any(not math.isfinite(row.value) or row.value < 0 for row in request_samples):
        raise SloValidationError("HTTP request counters must contain finite non-negative samples")
    request_total = sum(row.value for row in request_samples)
    if request_total <= 0:
        raise SloValidationError("HTTP SLO evidence requires observed request traffic")
    server_error_total = sum(
        row.value for row in request_samples if row.labels.get("status_class") == "5xx"
    )
    if server_error_total > request_total:
        raise SloValidationError("HTTP 5xx counters exceed total request counters")
    error_ratio = server_error_total / request_total
    availability = 1.0 - error_ratio
    p95 = histogram_quantile(
        samples, "propertyquarry_http_request_duration_seconds", 0.95
    )
    p99 = histogram_quantile(
        samples, "propertyquarry_http_request_duration_seconds", 0.99
    )
    histogram_total = sum(
        row.value
        for row in samples_for(
            samples, "propertyquarry_http_request_duration_seconds_bucket"
        )
        if row.labels.get("le") in {"+Inf", "Inf"}
    )
    if histogram_total != request_total:
        raise SloValidationError(
            "HTTP request and latency histogram totals are inconsistent"
        )
    provider_error_samples = [
        row
        for row in samples_for(samples, "propertyquarry_http_request_errors_total")
        if re.search(r"provider|quota|balance", str(row.labels.get("route") or ""), re.I)
    ]
    if any(not math.isfinite(row.value) or row.value < 0 for row in provider_error_samples):
        raise SloValidationError("provider/quota failure counters must be finite and non-negative")
    provider_quota_failures = sum(row.value for row in provider_error_samples)
    thresholds = {
        "availability_minimum": objective_number(slo, "availability", "target"),
        "error_ratio_maximum": objective_number(slo, "error_rate", "target_maximum"),
        "p95_seconds_maximum": objective_number(
            slo, "latency_p95_seconds", "target_maximum"
        ),
        "p99_seconds_maximum": objective_number(
            slo, "latency_p99_seconds", "target_maximum"
        ),
        "provider_quota_failures_maximum": objective_number(
            slo, "provider_quota_failures", "target_rate"
        ),
    }
    values = {
        "request_total": request_total,
        "server_error_total": server_error_total,
        "availability": availability,
        "error_ratio": error_ratio,
        "latency_p95_seconds": p95,
        "latency_p99_seconds": p99,
        "provider_quota_failure_total": provider_quota_failures,
    }
    failures: list[str] = []
    if availability < thresholds["availability_minimum"]:
        failures.append("availability")
    if error_ratio > thresholds["error_ratio_maximum"]:
        failures.append("error_ratio")
    if p95 > thresholds["p95_seconds_maximum"]:
        failures.append("latency_p95_seconds")
    if p99 > thresholds["p99_seconds_maximum"]:
        failures.append("latency_p99_seconds")
    if provider_quota_failures > thresholds["provider_quota_failures_maximum"]:
        failures.append("provider_quota_failures")
    if failures:
        raise SloValidationError(
            "current launch metrics exceed SLO objective(s): " + ", ".join(failures)
        )
    return {"status": "pass", "values": values, "thresholds": thresholds}


def validate_metrics(
    *,
    families: Mapping[str, str],
    samples: Sequence[MetricSample],
    slo: Mapping[str, object],
) -> dict[str, object]:
    required = [str(item) for item in slo["required_metric_families"]]  # type: ignore[index]
    missing = sorted(set(required) - set(families))
    if missing:
        raise SloValidationError(f"required metrics are missing: {missing}")
    expected_types = {
        "propertyquarry_http_requests_total": "counter",
        "propertyquarry_http_request_errors_total": "counter",
        "propertyquarry_http_request_duration_seconds": "histogram",
        "propertyquarry_readiness": "gauge",
        "propertyquarry_expected_api_replicas": "gauge",
        "propertyquarry_runtime_heartbeat_required": "gauge",
        "propertyquarry_runtime_heartbeat_age_seconds": "gauge",
        "propertyquarry_runtime_heartbeat_present": "gauge",
        "propertyquarry_runtime_heartbeat_stale": "gauge",
        "propertyquarry_ingress_rejections_total": "counter",
        "propertyquarry_ingress_cost_units_total": "counter",
        "propertyquarry_ingress_high_cost_inflight": "gauge",
        "propertyquarry_queue_depth": "gauge",
        "propertyquarry_queue_oldest_item_age_seconds": "gauge",
        "propertyquarry_scheduler_delivery_outbox_events_total": "counter",
        "propertyquarry_content_ledger_events_total": "counter",
    }
    wrong_types = sorted(
        f"{name}:{families.get(name)}"
        for name, expected_type in expected_types.items()
        if families.get(name) != expected_type
    )
    if wrong_types:
        raise SloValidationError(
            "required metrics have invalid Prometheus types: " + ", ".join(wrong_types)
        )

    readiness = samples_for(samples, "propertyquarry_readiness")
    if len(readiness) != 1 or readiness[0].value != 1.0:
        raise SloValidationError("PropertyQuarry readiness must have exactly one passing sample")

    expected_replicas = samples_for(samples, "propertyquarry_expected_api_replicas")
    if (
        len(expected_replicas) != 1
        or not math.isfinite(expected_replicas[0].value)
        or expected_replicas[0].value < 1
        or not expected_replicas[0].value.is_integer()
    ):
        raise SloValidationError(
            "PropertyQuarry expected API replicas must have exactly one positive integer sample"
        )

    current_slos = validate_current_slo_values(samples=samples, slo=slo)

    integrity_outcomes = {
        "delivery_outbox": (
            "propertyquarry_scheduler_delivery_outbox_events_total",
            ("dead_lettered", "failed", "claim_conflicts"),
        ),
        "content_ledger": (
            "propertyquarry_content_ledger_events_total",
            ("replay_conflict", "failed", "corruption"),
        ),
    }
    integrity: dict[str, object] = {}
    for area, (family, outcomes) in integrity_outcomes.items():
        values: dict[str, float] = {}
        for outcome in outcomes:
            matches = [
                row
                for row in samples_for(samples, family)
                if row.labels.get("outcome") == outcome
            ]
            if len(matches) != 1 or not math.isfinite(matches[0].value) or matches[0].value < 0:
                raise SloValidationError(
                    f"metric {family} must expose one finite non-negative {outcome} sample"
                )
            values[outcome] = matches[0].value
        if any(value > 0 for value in values.values()):
            raise SloValidationError(
                f"current {area} integrity counters are non-zero at launch evidence capture"
            )
        integrity[area] = {"status": "pass", "values": values}

    runtime_objective = objective(slo, "runtime_heartbeats")
    baseline_required_roles = {
        str(item or "").strip().lower()
        for item in runtime_objective.get("baseline_required_roles", [])
        if str(item or "").strip()
    }
    if baseline_required_roles != {"worker", "scheduler"}:
        raise SloValidationError("runtime heartbeat baseline roles are invalid")
    roles: dict[str, object] = {}
    for role in ("worker", "scheduler"):
        required_sample = single_role_sample(
            samples, "propertyquarry_runtime_heartbeat_required", role
        )
        if required_sample.value not in {0.0, 1.0}:
            raise SloValidationError(f"heartbeat required value for {role} must be 0 or 1")
        present = single_role_sample(samples, "propertyquarry_runtime_heartbeat_present", role)
        stale = single_role_sample(samples, "propertyquarry_runtime_heartbeat_stale", role)
        age = single_role_sample(samples, "propertyquarry_runtime_heartbeat_age_seconds", role)
        if role in baseline_required_roles and required_sample.value != 1.0:
            raise SloValidationError(f"{role} heartbeat must remain required")
        role_required = required_sample.value == 1.0
        if role_required and (
            present.value != 1.0
            or stale.value != 0.0
            or not math.isfinite(age.value)
            or age.value < 0
        ):
            raise SloValidationError(f"required {role} heartbeat is absent, stale, or invalid")
        roles[role] = {
            "required": role_required,
            "present": present.value == 1.0,
            "stale": stale.value == 1.0,
            "age_seconds": age.value if math.isfinite(age.value) else None,
        }

    capabilities: dict[str, object] = {}
    conditional = slo["conditional_capabilities"]
    assert isinstance(conditional, dict)
    for capability_name, raw_capability in conditional.items():
        assert isinstance(raw_capability, dict)
        capability_families = [str(item) for item in raw_capability["metric_families"]]
        exposed = [family in families for family in capability_families]
        if any(exposed) and not all(exposed):
            raise SloValidationError(
                f"conditional capability {capability_name} is only partially exposed"
            )
        if not any(exposed):
            capabilities[capability_name] = {"status": "not_exposed"}
            continue
        values: dict[str, float] = {}
        for family in capability_families:
            family_samples = samples_for(samples, family)
            if not family_samples or any(not math.isfinite(item.value) for item in family_samples):
                raise SloValidationError(
                    f"conditional capability {capability_name} has no finite samples for {family}"
                )
            values[family] = sum(item.value for item in family_samples)
        if capability_name == "database_pool_saturation":
            capacity = values["propertyquarry_db_pool_capacity"]
            ratio = values["propertyquarry_db_pool_in_use"] / capacity if capacity > 0 else math.inf
            if ratio >= float(raw_capability["warning_ratio"]):
                raise SloValidationError("database pool is saturated at launch evidence capture")
            values["utilization_ratio"] = ratio
        elif capability_name == "queue_backlog":
            for family in capability_families:
                property_search_samples = [
                    item
                    for item in samples_for(samples, family)
                    if item.labels.get("queue") == "property_search"
                ]
                if (
                    len(property_search_samples) != 1
                    or not math.isfinite(property_search_samples[0].value)
                    or property_search_samples[0].value < 0
                ):
                    raise SloValidationError(
                        "queue capability must expose one finite non-negative "
                        f"property_search sample for {family}"
                    )
                values[family] = property_search_samples[0].value
            if not values["propertyquarry_queue_depth"].is_integer():
                raise SloValidationError("property search queue depth must be an integer")
            if values["propertyquarry_queue_depth"] > float(raw_capability["warning_depth"]):
                raise SloValidationError("queue depth exceeds the launch evidence threshold")
            if values["propertyquarry_queue_oldest_item_age_seconds"] > float(
                raw_capability["maximum_oldest_age_seconds"]
            ):
                raise SloValidationError("oldest queue item exceeds the launch evidence threshold")
        capabilities[capability_name] = {"status": "exposed", "values": values}
    return {
        "readiness": True,
        "expected_api_replicas": int(expected_replicas[0].value),
        "current_slos": current_slos,
        "runtime_roles": roles,
        "integrity": integrity,
        "conditional_capabilities": capabilities,
    }


def canonical_json_sha256(payload: object) -> str:
    return sha256_bytes(evidence_contract.canonical_json_bytes(payload))


def _validated_payload_hash(payload: object, *, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise SloValidationError(f"{label} must be an object")
    normalized = dict(payload)
    observed = str(normalized.pop("payload_sha256", "")).strip().lower()
    authentication = normalized.get("authentication")
    if isinstance(authentication, dict):
        authentication = dict(authentication)
        authentication.pop("signature", None)
        normalized["authentication"] = authentication
    if not SHA256_RE.fullmatch(observed) or observed != canonical_json_sha256(normalized):
        raise SloValidationError(f"{label} payload hash is invalid")
    return dict(payload)


def _referenced_file(parent: Path, raw_name: object, *, label: str) -> Path:
    if not isinstance(raw_name, str) or not raw_name or raw_name != raw_name.strip():
        raise SloValidationError(f"{label} must be one adjacent file name")
    name = raw_name
    candidate = Path(name)
    if not name or candidate.name != name or candidate.is_absolute():
        raise SloValidationError(f"{label} must be one adjacent file name")
    resolved = (parent / candidate).resolve()
    if resolved.parent != parent.resolve() or not resolved.is_file() or resolved.is_symlink():
        raise SloValidationError(f"{label} is missing or unsafe")
    return resolved


def _json_nonnegative_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SloValidationError(f"{label} must be a nonnegative JSON integer")
    return value


def _validated_file_reference(
    parent: Path,
    payload: object,
    *,
    label: str,
) -> tuple[Path, bytes]:
    if not isinstance(payload, Mapping):
        raise SloValidationError(f"{label} file evidence is invalid")
    raw_name = payload.get("path")
    if not isinstance(raw_name, str) or not raw_name or raw_name != raw_name.strip():
        raise SloValidationError(f"{label} path must be one adjacent file name")
    candidate = Path(raw_name)
    if candidate.name != raw_name or candidate.is_absolute():
        raise SloValidationError(f"{label} path must be one adjacent file name")
    parent_resolved = parent.resolve()
    path = parent_resolved / candidate
    try:
        path_before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise SloValidationError(f"{label} is missing or unsafe") from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise SloValidationError(f"{label} is missing or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SloValidationError(f"{label} is missing or unsafe") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != path_before.st_dev
            or before.st_ino != path_before.st_ino
        ):
            raise SloValidationError(f"{label} inode changed before it was read")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        path_after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise SloValidationError(f"{label} changed while it was read") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        len(raw) != before.st_size
        or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
        or any(getattr(before, name) != getattr(path_after, name) for name in stable_fields)
    ):
        raise SloValidationError(f"{label} changed while it was read")
    observed_hash_value = payload.get("sha256")
    if not isinstance(observed_hash_value, str):
        raise SloValidationError(f"{label} file hash or size does not match")
    observed_hash = observed_hash_value
    observed_bytes = _json_nonnegative_integer(
        payload.get("bytes"), label=f"{label} byte count"
    )
    if (
        not SHA256_RE.fullmatch(observed_hash)
        or observed_hash != sha256_bytes(raw)
        or observed_bytes != len(raw)
    ):
        raise SloValidationError(f"{label} file hash or size does not match")
    return path, raw


def _private_peer(transport: Mapping[str, object], *, label: str) -> str:
    try:
        address = ipaddress.ip_address(str(transport.get("connected_peer_ip") or ""))
    except ValueError as exc:
        raise SloValidationError(f"{label} connected peer must be an IP literal") from exc
    private = address.is_loopback or address.is_private
    if not private:
        raise SloValidationError(f"{label} connected peer must be private")
    if not address.is_loopback and transport.get("tls_verified") is not True:
        raise SloValidationError(f"{label} private peer must have verified TLS")
    return address.compressed


def _validate_transport(
    payload: object,
    *,
    label: str,
    endpoint_path: str,
    authenticated: bool,
    require_prometheus: bool = False,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise SloValidationError(f"{label} transport evidence is invalid")
    if (
        payload.get("endpoint_path") != endpoint_path
        or payload.get("authenticated") is not authenticated
        or payload.get("private_route") is not True
        or payload.get("credential_persisted") is not False
        or payload.get("http_status") != 200
    ):
        raise SloValidationError(f"{label} transport evidence is incomplete")
    content_type = str(payload.get("content_type") or "").strip()
    if require_prometheus and not content_type.lower().startswith("text/plain"):
        raise SloValidationError(f"{label} is not Prometheus text")
    cache_control = str(payload.get("cache_control") or "").strip()
    directives = {part.strip().lower() for part in cache_control.split(",") if part.strip()}
    if require_prometheus and "no-store" not in directives:
        raise SloValidationError(f"{label} does not prove Cache-Control: no-store")
    return {
        "endpoint_path": endpoint_path,
        "authenticated": authenticated,
        "private_route": True,
        "credential_persisted": False,
        "http_status": 200,
        "content_type": content_type,
        "cache_control": cache_control,
        "connected_peer_ip": _private_peer(payload, label=label),
        "tls_verified": payload.get("tls_verified") is True,
    }


def validate_snapshot_bundle(
    *,
    snapshot_bundle_path: Path,
    probe_bundle_path: Path,
    release_commit_sha: str,
    release_image_digest: str,
    maximum_age_seconds: int,
    now: datetime,
) -> tuple[list[ReplicaSnapshotPair], dict[str, object]]:
    snapshot_bundle_raw = snapshot_bundle_path.read_bytes()
    try:
        snapshot_bundle_payload = json.loads(snapshot_bundle_raw)
    except json.JSONDecodeError as exc:
        raise SloValidationError("metrics snapshot bundle is not valid JSON") from exc
    snapshot_bundle = _validated_payload_hash(
        snapshot_bundle_payload,
        label="metrics snapshot bundle",
    )
    probe_bundle = _validated_payload_hash(
        load_json(probe_bundle_path, document_name="metrics probe bundle"),
        label="metrics probe bundle",
    )
    if snapshot_bundle.get("schema") != SNAPSHOT_BUNDLE_SCHEMA:
        raise SloValidationError(f"metrics snapshot bundle schema must be {SNAPSHOT_BUNDLE_SCHEMA}")
    if probe_bundle.get("schema") != PROBE_BUNDLE_SCHEMA:
        raise SloValidationError(f"metrics probe bundle schema must be {PROBE_BUNDLE_SCHEMA}")
    for payload, label in (
        (snapshot_bundle, "metrics snapshot bundle"),
        (probe_bundle, "metrics probe bundle"),
    ):
        if normalize_release_sha(str(payload.get("release_commit_sha") or "")) != release_commit_sha:
            raise SloValidationError(f"{label} is not bound to the candidate release")
        if normalize_image_digest(str(payload.get("release_image_digest") or "")) != release_image_digest:
            raise SloValidationError(f"{label} is not bound to the candidate image digest")
    if (
        str(probe_bundle.get("snapshot_bundle_sha256") or "").lower()
        != sha256_bytes(snapshot_bundle_raw)
        or _json_nonnegative_integer(
            probe_bundle.get("snapshot_bundle_bytes"),
            label="metrics probe bundle snapshot byte count",
        )
        != len(snapshot_bundle_raw)
        or probe_bundle.get("credential_persisted") is not False
    ):
        raise SloValidationError("metrics probe bundle does not bind the snapshot bundle")
    start_at = parse_timestamp(snapshot_bundle.get("window_start"), field_name="snapshot window start")
    end_at = parse_timestamp(snapshot_bundle.get("window_end"), field_name="snapshot window end")
    duration = (end_at - start_at).total_seconds()
    if duration < 1:
        raise SloValidationError("metrics snapshot window must contain two distinct timestamps")
    try:
        declared_duration = float(snapshot_bundle.get("window_seconds"))
    except (TypeError, ValueError) as exc:
        raise SloValidationError("metrics snapshot window duration is invalid") from exc
    if not math.isfinite(declared_duration) or abs(declared_duration - duration) > 0.001:
        raise SloValidationError("metrics snapshot window duration does not match its timestamps")
    age = (now - end_at).total_seconds()
    if age < -60 or age > maximum_age_seconds:
        raise SloValidationError("metrics snapshot bundle is future-dated or too old")
    manifest_rows = snapshot_bundle.get("replicas")
    probe_rows = probe_bundle.get("replicas")
    if not isinstance(manifest_rows, list) or not isinstance(probe_rows, list):
        raise SloValidationError("metrics replica evidence is missing")
    manifest_count = _json_nonnegative_integer(
        snapshot_bundle.get("replica_count"), label="metrics snapshot replica count"
    )
    probe_count = _json_nonnegative_integer(
        probe_bundle.get("replica_count"), label="metrics probe replica count"
    )
    if (
        manifest_count <= 0
        or manifest_count != probe_count
        or manifest_count != len(manifest_rows)
        or probe_count != len(probe_rows)
    ):
        raise SloValidationError("metrics replica count does not match distinct artifacts")
    manifest_by_container: dict[str, Mapping[str, object]] = {}
    for row in manifest_rows:
        if not isinstance(row, Mapping):
            raise SloValidationError("metrics snapshot replica evidence is invalid")
        container_id = str(row.get("container_id") or "").strip().lower()
        if not CONTAINER_ID_RE.fullmatch(container_id) or container_id in manifest_by_container:
            raise SloValidationError("metrics snapshot container identity is invalid or duplicate")
        manifest_by_container[container_id] = row
    probe_by_container: dict[str, Mapping[str, object]] = {}
    for row in probe_rows:
        if not isinstance(row, Mapping):
            raise SloValidationError("metrics probe replica evidence is invalid")
        container_id = str(row.get("container_id") or "").strip().lower()
        if not CONTAINER_ID_RE.fullmatch(container_id) or container_id in probe_by_container:
            raise SloValidationError("metrics probe container identity is invalid or duplicate")
        probe_by_container[container_id] = row
    if set(manifest_by_container) != set(probe_by_container):
        raise SloValidationError("metrics snapshot and probe replica sets diverge")

    pairs: list[ReplicaSnapshotPair] = []
    used_paths: set[Path] = set()
    replica_ids: set[str] = set()
    parent = snapshot_bundle_path.resolve().parent
    probe_parent = probe_bundle_path.resolve().parent
    for container_id in sorted(manifest_by_container):
        row = manifest_by_container[container_id]
        probe_ref = probe_by_container[container_id]
        replica_id = str(row.get("replica_id") or "").strip()
        container_image_id = str(row.get("container_image_id") or "").strip().lower()
        inspect_hash = str(row.get("docker_inspect_sha256") or "").strip().lower()
        observed_release = normalize_release_sha(str(row.get("release_commit_sha") or ""))
        observed_image = normalize_image_digest(str(row.get("release_image_digest") or ""))
        if (
            not REPLICA_ID_RE.fullmatch(replica_id)
            or replica_id in replica_ids
            or not IMAGE_DIGEST_RE.fullmatch(container_image_id)
            or observed_release != release_commit_sha
            or observed_image != release_image_digest
            or not SHA256_RE.fullmatch(inspect_hash)
        ):
            raise SloValidationError("metrics Docker replica binding is invalid or divergent")
        replica_ids.add(replica_id)
        start_path, start_snapshot = _validated_file_reference(
            parent,
            row.get("start"),
            label=f"{replica_id} start snapshot",
        )
        end_path, end_snapshot = _validated_file_reference(
            parent,
            row.get("end"),
            label=f"{replica_id} end snapshot",
        )
        if start_path in used_paths or end_path in used_paths or start_path == end_path:
            raise SloValidationError("metrics replicas must have distinct snapshot artifacts")
        used_paths.update((start_path, end_path))
        start_row = row.get("start")
        end_row = row.get("end")
        assert isinstance(start_row, Mapping) and isinstance(end_row, Mapping)
        if (
            parse_timestamp(start_row.get("captured_at"), field_name="replica start capture") != start_at
            or parse_timestamp(end_row.get("captured_at"), field_name="replica end capture") != end_at
        ):
            raise SloValidationError("replica snapshot timestamps diverge from the bundle window")
        replica_probe_path, replica_probe_raw = _validated_file_reference(
            probe_parent,
            probe_ref,
            label=f"{replica_id} probe receipt",
        )
        if replica_probe_path in used_paths:
            raise SloValidationError("metrics replicas must have distinct probe artifacts")
        used_paths.add(replica_probe_path)
        try:
            replica_probe_payload = json.loads(replica_probe_raw)
        except json.JSONDecodeError as exc:
            raise SloValidationError("replica probe receipt is not valid JSON") from exc
        replica_probe = _validated_payload_hash(
            replica_probe_payload,
            label=f"{replica_id} probe receipt",
        )
        if replica_probe.get("schema") != PROBE_SCHEMA:
            raise SloValidationError(f"replica probe schema must be {PROBE_SCHEMA}")
        for key, expected in (
            ("container_id", container_id),
            ("container_image_id", container_image_id),
            ("replica_id", replica_id),
            ("release_commit_sha", release_commit_sha),
            ("release_image_digest", release_image_digest),
            ("docker_inspect_sha256", inspect_hash),
        ):
            if str(replica_probe.get(key) or "").strip().lower() != expected.lower():
                raise SloValidationError("replica probe identity diverges from its snapshot")
        version = replica_probe.get("version")
        if not isinstance(version, Mapping) or any(
            str(version.get(key) or "").strip() != expected
            for key, expected in (
                ("release_commit_sha", release_commit_sha),
                ("release_image_digest", release_image_digest),
                ("replica_id", replica_id),
                ("role", "api"),
            )
        ) or not SHA256_RE.fullmatch(str(version.get("response_sha256") or "")):
            raise SloValidationError("replica /version identity is incomplete")
        version_transport = _validate_transport(
            replica_probe.get("version_transport"),
            label=f"{replica_id} version probe",
            endpoint_path="/version",
            authenticated=False,
        )
        snapshots = replica_probe.get("snapshots")
        if not isinstance(snapshots, list) or len(snapshots) != 2:
            raise SloValidationError("replica probe must bind exactly two metrics snapshots")
        expected_rows = (start_row, end_row)
        for index, (snapshot_probe, expected_row) in enumerate(zip(snapshots, expected_rows, strict=True)):
            if not isinstance(snapshot_probe, Mapping):
                raise SloValidationError("replica metrics transport evidence is invalid")
            _json_nonnegative_integer(
                snapshot_probe.get("bytes"),
                label=f"{replica_id} metrics snapshot {index} byte count",
            )
            if not isinstance(snapshot_probe.get("path"), str) or not isinstance(
                snapshot_probe.get("sha256"), str
            ):
                raise SloValidationError(
                    "replica metrics snapshot file reference types are invalid"
                )
            for field in ("captured_at", "path", "sha256", "bytes"):
                if snapshot_probe.get(field) != expected_row.get(field):
                    raise SloValidationError("replica probe does not bind its metrics snapshot")
            transport = _validate_transport(
                snapshot_probe,
                label=f"{replica_id} metrics snapshot {index}",
                endpoint_path="/internal/metrics",
                authenticated=True,
                require_prometheus=True,
            )
            if transport["connected_peer_ip"] != version_transport["connected_peer_ip"]:
                raise SloValidationError("replica version and metrics peers diverge")
        pairs.append(
            ReplicaSnapshotPair(
                replica_id=replica_id,
                container_id=container_id,
                container_image_id=container_image_id,
                release_commit_sha=release_commit_sha,
                release_image_digest=release_image_digest,
                start_at=start_at,
                end_at=end_at,
                start_path=start_path,
                end_path=end_path,
                start_sha256=sha256_bytes(start_snapshot),
                end_sha256=sha256_bytes(end_snapshot),
                start_snapshot=start_snapshot,
                end_snapshot=end_snapshot,
            )
        )
    return pairs, {
        "schema": SNAPSHOT_BUNDLE_SCHEMA,
        "probe_schema": PROBE_BUNDLE_SCHEMA,
        "release_commit_sha": release_commit_sha,
        "release_image_digest": release_image_digest,
        "window_start": isoformat(start_at),
        "window_end": isoformat(end_at),
        "window_seconds": duration,
        "replica_count": len(pairs),
        "replica_ids": sorted(replica_ids),
        "snapshot_bundle_sha256": sha256_bytes(snapshot_bundle_raw),
        "probe_bundle_sha256": sha256_bytes(probe_bundle_path.read_bytes()),
        "credential_persisted": False,
    }


def _sample_key(sample: MetricSample) -> tuple[str, tuple[tuple[str, str], ...]]:
    return sample.name, tuple(sorted(sample.labels.items()))


def _sample_map(samples: Sequence[MetricSample]) -> dict[tuple[str, tuple[tuple[str, str], ...]], MetricSample]:
    return {_sample_key(sample): sample for sample in samples}


def _validate_runtime_build_info(
    samples: Sequence[MetricSample],
    *,
    pair: ReplicaSnapshotPair,
) -> None:
    rows = samples_for(samples, "propertyquarry_runtime_build_info")
    expected = {
        "release_commit_sha": pair.release_commit_sha,
        "release_image_digest": pair.release_image_digest,
        "replica_id": pair.replica_id,
    }
    if len(rows) != 1 or rows[0].value != 1.0 or dict(rows[0].labels) != expected:
        raise SloValidationError("runtime build info diverges from Docker-bound probe evidence")


def _validate_histogram_contract(
    samples: Sequence[MetricSample],
    family: str,
) -> None:
    buckets = samples_for(samples, f"{family}_bucket")
    counts = samples_for(samples, f"{family}_count")
    sums = samples_for(samples, f"{family}_sum")
    grouped_buckets: dict[tuple[tuple[str, str], ...], dict[str, float]] = defaultdict(dict)
    for sample in buckets:
        if set(sample.labels) != {"method", "route", "le"}:
            raise SloValidationError(f"histogram {family} bucket labels are not canonical")
        if not math.isfinite(sample.value) or sample.value < 0 or not sample.value.is_integer():
            raise SloValidationError(f"histogram {family} has a non-finite or negative bucket")
        base = tuple(sorted((key, value) for key, value in sample.labels.items() if key != "le"))
        bound = str(sample.labels["le"])
        if bound in grouped_buckets[base]:
            raise SloValidationError(f"histogram {family} has a duplicate bucket")
        grouped_buckets[base][bound] = sample.value
    if not grouped_buckets:
        raise SloValidationError(f"histogram {family} has no bucket series")
    expected_bounds = {
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.1",
        "0.25",
        "0.5",
        "1",
        "2.5",
        "5",
        "10",
        "+Inf",
    }
    count_map = {tuple(sorted(row.labels.items())): row for row in counts}
    sum_map = {tuple(sorted(row.labels.items())): row for row in sums}
    if len(count_map) != len(counts) or len(sum_map) != len(sums):
        raise SloValidationError(f"histogram {family} count or sum series is duplicated")
    if set(grouped_buckets) != set(count_map) or set(grouped_buckets) != set(sum_map):
        raise SloValidationError(f"histogram {family} bucket/count/sum label sets diverge")
    ordered_labels = [
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.1",
        "0.25",
        "0.5",
        "1",
        "2.5",
        "5",
        "10",
        "+Inf",
    ]
    for base, values in grouped_buckets.items():
        if set(values) != expected_bounds:
            raise SloValidationError(f"histogram {family} must expose exact finite buckets plus +Inf")
        previous = 0.0
        for bound in ordered_labels:
            value = values[bound]
            if value < previous:
                raise SloValidationError(f"histogram {family} buckets are not cumulative")
            previous = value
        count = count_map[base].value
        total = sum_map[base].value
        if (
            not math.isfinite(count)
            or count < 0
            or not count.is_integer()
            or values["+Inf"] != count
            or not math.isfinite(total)
            or total < 0
        ):
            raise SloValidationError(f"histogram {family} count/sum is inconsistent")


def _monotonic_deltas(
    *,
    start_families: Mapping[str, str],
    start_samples: Sequence[MetricSample],
    end_families: Mapping[str, str],
    end_samples: Sequence[MetricSample],
) -> list[MetricSample]:
    if dict(start_families) != dict(end_families):
        raise SloValidationError("metric family/type sets diverge between snapshots")
    _validate_histogram_contract(start_samples, "propertyquarry_http_request_duration_seconds")
    _validate_histogram_contract(end_samples, "propertyquarry_http_request_duration_seconds")
    monotonic_names = {
        name for name, metric_type in start_families.items() if metric_type == "counter"
    }
    monotonic_names.update(
        {
            "propertyquarry_http_request_duration_seconds_bucket",
            "propertyquarry_http_request_duration_seconds_count",
            "propertyquarry_http_request_duration_seconds_sum",
        }
    )
    start_map = {
        key: sample
        for key, sample in _sample_map(start_samples).items()
        if sample.name in monotonic_names
    }
    end_map = {
        key: sample
        for key, sample in _sample_map(end_samples).items()
        if sample.name in monotonic_names
    }
    if set(start_map) != set(end_map):
        raise SloValidationError("counter/histogram series diverge between snapshots")
    deltas: list[MetricSample] = []
    for key in sorted(start_map):
        start = start_map[key]
        end = end_map[key]
        if (
            not math.isfinite(start.value)
            or not math.isfinite(end.value)
            or start.value < 0
            or end.value < 0
        ):
            raise SloValidationError("counter/histogram evidence contains NaN, infinity, or negatives")
        if end.value < start.value:
            raise SloValidationError("counter reset detected inside the SLO evidence window")
        deltas.append(MetricSample(end.name, dict(end.labels), end.value - start.value))
    return deltas


def _validate_ingress_admission_state(
    samples: Sequence[MetricSample],
    *,
    capacity_objective: Mapping[str, object],
) -> None:
    backends = {"postgres"}
    operations = {
        "ip_request",
        "admit",
        "renew",
        "release",
        "cleanup",
        "snapshot",
    }
    outcomes = {
        "allowed",
        "quota_limited",
        "lease_limited",
        "capacity_exhausted",
        "backend_unavailable",
    }
    operation_rows = samples_for(
        samples,
        "propertyquarry_ingress_admission_operations_total",
    )
    expected_operation_labels = {
        (backend, operation, outcome)
        for backend in backends
        for operation in operations
        for outcome in outcomes
    }
    observed_operation_labels: set[tuple[str, str, str]] = set()
    for row in operation_rows:
        if set(row.labels) != {"backend", "operation", "outcome"}:
            raise SloValidationError(
                "ingress admission operation labels are not canonical"
            )
        identity = (
            row.labels["backend"],
            row.labels["operation"],
            row.labels["outcome"],
        )
        if (
            identity[0] not in backends
            or identity[1] not in operations
            or identity[2] not in outcomes
            or not math.isfinite(row.value)
            or row.value < 0
            or row.value != math.floor(row.value)
        ):
            raise SloValidationError(
                "ingress admission operation sample is invalid"
            )
        observed_operation_labels.add(identity)
    if observed_operation_labels != expected_operation_labels:
        raise SloValidationError(
            "ingress admission operation label matrix is incomplete"
        )

    contract_rows = samples_for(
        samples,
        "propertyquarry_admission_capacity_contract_valid",
    )
    if (
        len(contract_rows) != 1
        or contract_rows[0].labels != {"backend": "postgres"}
        or contract_rows[0].value != 1.0
    ):
        raise SloValidationError(
            "PostgreSQL admission capacity contract must be exactly valid"
        )

    expected_limits = {
        str(key): int(value)
        for key, value in dict(capacity_objective["hard_limits"]).items()
    }
    row_count_samples = samples_for(
        samples,
        "propertyquarry_admission_capacity_row_count",
    )
    limit_samples = samples_for(
        samples,
        "propertyquarry_admission_capacity_limit",
    )

    def capacity_values(
        rows: Sequence[MetricSample],
        *,
        family: str,
    ) -> dict[str, int]:
        values: dict[str, int] = {}
        for row in rows:
            if set(row.labels) != {"backend", "capacity_key"}:
                raise SloValidationError(
                    f"{family} labels are not canonical"
                )
            backend = row.labels["backend"]
            capacity_key = row.labels["capacity_key"]
            if (
                backend != "postgres"
                or capacity_key not in expected_limits
                or capacity_key in values
                or not math.isfinite(row.value)
                or row.value < 0
                or row.value != math.floor(row.value)
            ):
                raise SloValidationError(f"{family} sample is invalid")
            values[capacity_key] = int(row.value)
        if set(values) != set(expected_limits):
            raise SloValidationError(f"{family} key set is incomplete")
        return values

    row_counts = capacity_values(
        row_count_samples,
        family="admission capacity row count",
    )
    limits = capacity_values(
        limit_samples,
        family="admission capacity limit",
    )
    if limits != expected_limits:
        raise SloValidationError(
            "admission capacity limits differ from the canonical contract"
        )
    if any(row_counts[key] > limits[key] for key in expected_limits):
        raise SloValidationError(
            "admission capacity row count exceeds its hard limit"
        )


def _validate_end_state(
    *,
    families: Mapping[str, str],
    samples: Sequence[MetricSample],
    slo: Mapping[str, object],
) -> dict[str, object]:
    required = [str(item) for item in slo["required_metric_families"]]  # type: ignore[index]
    missing = sorted(set(required) - set(families))
    if missing:
        raise SloValidationError(f"required metrics are missing: {missing}")
    expected_types = {
        "propertyquarry_http_requests_total": "counter",
        "propertyquarry_http_request_errors_total": "counter",
        "propertyquarry_http_request_duration_seconds": "histogram",
        "propertyquarry_readiness": "gauge",
        "propertyquarry_runtime_build_info": "gauge",
        "propertyquarry_runtime_heartbeat_required": "gauge",
        "propertyquarry_runtime_heartbeat_age_seconds": "gauge",
        "propertyquarry_runtime_heartbeat_present": "gauge",
        "propertyquarry_runtime_heartbeat_stale": "gauge",
        "propertyquarry_ingress_rejections_total": "counter",
        "propertyquarry_ingress_cost_units_total": "counter",
        "propertyquarry_ingress_high_cost_inflight": "gauge",
        "propertyquarry_ingress_admission_operations_total": "counter",
        "propertyquarry_admission_capacity_contract_valid": "gauge",
        "propertyquarry_admission_capacity_row_count": "gauge",
        "propertyquarry_admission_capacity_limit": "gauge",
        "propertyquarry_queue_observed": "gauge",
        "propertyquarry_queue_depth": "gauge",
        "propertyquarry_queue_oldest_item_age_seconds": "gauge",
        "propertyquarry_scheduler_delivery_outbox_events_total": "counter",
        "propertyquarry_content_ledger_events_total": "counter",
    }
    wrong = sorted(
        f"{name}:{families.get(name)}"
        for name, expected in expected_types.items()
        if families.get(name) != expected
    )
    if wrong:
        raise SloValidationError("required metrics have invalid Prometheus types: " + ", ".join(wrong))
    capacity_objective = next(
        row
        for row in slo["objectives"]  # type: ignore[index]
        if (
            isinstance(row, Mapping)
            and row.get("id") == "ingress_admission_capacity"
        )
    )
    _validate_ingress_admission_state(
        samples,
        capacity_objective=capacity_objective,
    )
    readiness = samples_for(samples, "propertyquarry_readiness")
    if len(readiness) != 1 or readiness[0].value != 1.0:
        raise SloValidationError("PropertyQuarry readiness must have exactly one passing sample")
    heartbeat_objective = next(
        row
        for row in slo["objectives"]  # type: ignore[index]
        if isinstance(row, Mapping) and row.get("id") == "runtime_heartbeats"
    )
    baseline_required_roles = set(heartbeat_objective["baseline_required_roles"])
    roles: dict[str, object] = {}
    for role in ("worker", "scheduler"):
        required_sample = single_role_sample(samples, "propertyquarry_runtime_heartbeat_required", role)
        present = single_role_sample(samples, "propertyquarry_runtime_heartbeat_present", role)
        stale = single_role_sample(samples, "propertyquarry_runtime_heartbeat_stale", role)
        age = single_role_sample(samples, "propertyquarry_runtime_heartbeat_age_seconds", role)
        if required_sample.value not in {0.0, 1.0} or present.value not in {0.0, 1.0} or stale.value not in {0.0, 1.0}:
            raise SloValidationError(f"heartbeat state for {role} is invalid")
        if role in baseline_required_roles and required_sample.value != 1.0:
            raise SloValidationError(f"{role} heartbeat must remain required")
        role_required = required_sample.value == 1.0
        if role_required and (
            present.value != 1.0
            or stale.value != 0.0
            or not math.isfinite(age.value)
            or age.value < 0
        ):
            raise SloValidationError(f"required {role} heartbeat is absent, stale, or invalid")
        roles[role] = {
            "required": role_required,
            "present": present.value == 1.0,
            "stale": stale.value == 1.0,
            "age_seconds": age.value if math.isfinite(age.value) else None,
        }
    capabilities: dict[str, object] = {}
    conditional = slo["conditional_capabilities"]
    assert isinstance(conditional, dict)
    for capability_name, raw_capability in conditional.items():
        assert isinstance(raw_capability, dict)
        capability_families = [str(item) for item in raw_capability["metric_families"]]
        exposed = [family in families for family in capability_families]
        if any(exposed) and not all(exposed):
            raise SloValidationError(f"conditional capability {capability_name} is only partially exposed")
        if not any(exposed):
            capabilities[capability_name] = {"status": "not_exposed"}
            continue
        values: dict[str, float] = {}
        for family in capability_families:
            rows = samples_for(samples, family)
            if not rows or any(not math.isfinite(row.value) or row.value < 0 for row in rows):
                raise SloValidationError(f"conditional capability {capability_name} has invalid samples")
            values[family] = sum(row.value for row in rows)
        if capability_name == "database_pool_saturation":
            capacity = values["propertyquarry_db_pool_capacity"]
            ratio = values["propertyquarry_db_pool_in_use"] / capacity if capacity > 0 else math.inf
            if ratio >= float(raw_capability["warning_ratio"]):
                raise SloValidationError("database pool is saturated at launch evidence capture")
            values["utilization_ratio"] = ratio
        elif capability_name == "queue_backlog":
            for family in capability_families:
                property_search_samples = [
                    item
                    for item in samples_for(samples, family)
                    if item.labels.get("queue") == "property_search"
                ]
                if (
                    len(property_search_samples) != 1
                    or not math.isfinite(property_search_samples[0].value)
                    or property_search_samples[0].value < 0
                ):
                    raise SloValidationError(
                        "queue capability must expose one finite non-negative "
                        f"property_search sample for {family}"
                    )
                values[family] = property_search_samples[0].value
            if not values["propertyquarry_queue_depth"].is_integer():
                raise SloValidationError("property search queue depth must be an integer")
            if (
                values["propertyquarry_queue_observed"]
                != float(raw_capability["target_observed"])
            ):
                raise SloValidationError(
                    "property search queue telemetry is unobserved"
                )
            if (
                values["propertyquarry_queue_depth"] == 0
                and values[
                    "propertyquarry_queue_oldest_item_age_seconds"
                ]
                != 0
            ):
                raise SloValidationError(
                    "empty property search queue reports a non-zero oldest age"
                )
            if values["propertyquarry_queue_depth"] > float(raw_capability["warning_depth"]):
                raise SloValidationError("queue depth exceeds the launch evidence threshold")
            if values["propertyquarry_queue_oldest_item_age_seconds"] > float(
                raw_capability["maximum_oldest_age_seconds"]
            ):
                raise SloValidationError("oldest queue item exceeds the launch evidence threshold")
        capabilities[capability_name] = {"status": "exposed", "values": values}
    return {"readiness": True, "runtime_roles": roles, "conditional_capabilities": capabilities}


def _slo_delta_values(
    *,
    deltas: Sequence[MetricSample],
    slo: Mapping[str, object],
    window_name: str,
    require_integrity: bool = True,
) -> dict[str, object]:
    requests = samples_for(deltas, "propertyquarry_http_requests_total")
    request_total = sum(row.value for row in requests)
    if request_total <= 0 or any(not math.isfinite(row.value) or row.value < 0 for row in requests):
        raise SloValidationError(f"{window_name} SLO evidence requires positive finite request deltas")
    server_errors = sum(row.value for row in requests if row.labels.get("status_class") == "5xx")
    if server_errors > request_total:
        raise SloValidationError(f"{window_name} HTTP 5xx deltas exceed request deltas")
    histogram_total = sum(
        row.value
        for row in samples_for(deltas, "propertyquarry_http_request_duration_seconds_bucket")
        if row.labels.get("le") == "+Inf"
    )
    if histogram_total != request_total:
        raise SloValidationError(f"{window_name} request and histogram deltas are inconsistent")
    error_ratio = server_errors / request_total
    availability = 1.0 - error_ratio
    p95 = histogram_quantile(deltas, "propertyquarry_http_request_duration_seconds", 0.95)
    p99 = histogram_quantile(deltas, "propertyquarry_http_request_duration_seconds", 0.99)
    provider_failures = sum(
        row.value
        for row in samples_for(deltas, "propertyquarry_http_request_errors_total")
        if re.search(r"provider|quota|balance", str(row.labels.get("route") or ""), re.I)
    )
    admission_backend_rows = [
        row
        for row in samples_for(
            deltas,
            "propertyquarry_ingress_admission_operations_total",
        )
        if row.labels.get("outcome") == "backend_unavailable"
    ]
    if not admission_backend_rows or any(
        not math.isfinite(row.value) or row.value < 0
        for row in admission_backend_rows
    ):
        raise SloValidationError(
            f"{window_name} ingress admission backend deltas are missing or invalid"
        )
    admission_backend_failures = sum(
        row.value for row in admission_backend_rows
    )
    integrity_contract = {
        "delivery_outbox": (
            "propertyquarry_scheduler_delivery_outbox_events_total",
            ("dead_lettered", "failed", "claim_conflicts"),
        ),
        "content_ledger": (
            "propertyquarry_content_ledger_events_total",
            ("replay_conflict", "failed", "corruption"),
        ),
    }
    integrity: dict[str, object] = {}
    for area, (family, outcomes) in integrity_contract.items() if require_integrity else ():
        values: dict[str, float] = {}
        for outcome in outcomes:
            matches = [row for row in samples_for(deltas, family) if row.labels.get("outcome") == outcome]
            if len(matches) != 1 or not math.isfinite(matches[0].value) or matches[0].value < 0:
                raise SloValidationError(f"{window_name} {family} delta is invalid for {outcome}")
            values[outcome] = matches[0].value
        if any(value > 0 for value in values.values()):
            raise SloValidationError(f"{window_name} {area} integrity deltas are non-zero")
        integrity[area] = {"status": "pass", "values": values}
    thresholds = {
        "availability_minimum": objective_number(slo, "availability", "target"),
        "error_ratio_maximum": objective_number(slo, "error_rate", "target_maximum"),
        "p95_seconds_maximum": objective_number(slo, "latency_p95_seconds", "target_maximum"),
        "p99_seconds_maximum": objective_number(slo, "latency_p99_seconds", "target_maximum"),
        "provider_quota_failures_maximum": objective_number(slo, "provider_quota_failures", "target_rate"),
        "ingress_admission_backend_maximum": objective_number(
            slo,
            "ingress_admission_backend",
            "target_rate",
        ),
    }
    values = {
        "request_delta": request_total,
        "server_error_delta": server_errors,
        "availability": availability,
        "error_ratio": error_ratio,
        "latency_p95_seconds": p95,
        "latency_p99_seconds": p99,
        "provider_quota_failure_delta": provider_failures,
        "ingress_admission_backend_failure_delta": admission_backend_failures,
    }
    failures: list[str] = []
    if availability < thresholds["availability_minimum"]:
        failures.append("availability")
    if error_ratio > thresholds["error_ratio_maximum"]:
        failures.append("error_ratio")
    if p95 > thresholds["p95_seconds_maximum"]:
        failures.append("latency_p95_seconds")
    if p99 > thresholds["p99_seconds_maximum"]:
        failures.append("latency_p99_seconds")
    if provider_failures > thresholds["provider_quota_failures_maximum"]:
        failures.append("provider_quota_failures")
    if admission_backend_failures > thresholds["ingress_admission_backend_maximum"]:
        failures.append("ingress_admission_backend")
    if failures:
        raise SloValidationError(f"{window_name} metrics exceed SLO objective(s): " + ", ".join(failures))
    return {
        "status": "pass",
        "window": window_name,
        "values": values,
        "thresholds": thresholds,
        "integrity": integrity,
    }


def _validate_ingress_admission_operation_matrix(
    samples: Sequence[MetricSample],
    *,
    window_name: str,
) -> None:
    rows = samples_for(
        samples,
        "propertyquarry_ingress_admission_operations_total",
    )
    expected = {
        ("postgres", operation, outcome)
        for operation in _INGRESS_ADMISSION_OPERATIONS
        for outcome in _INGRESS_ADMISSION_OUTCOMES
    }
    observed = [
        (
            str(row.labels.get("backend") or ""),
            str(row.labels.get("operation") or ""),
            str(row.labels.get("outcome") or ""),
        )
        for row in rows
    ]
    if (
        len(rows) != len(expected)
        or set(observed) != expected
        or any(
            set(row.labels) != {"backend", "operation", "outcome"}
            for row in rows
        )
    ):
        raise SloValidationError(
            f"{window_name} ingress admission operation matrix is incomplete or divergent"
        )


def validate_short_window_metrics(
    *,
    pairs: Sequence[ReplicaSnapshotPair],
    slo: Mapping[str, object],
) -> dict[str, object]:
    aggregate_deltas: list[MetricSample] = []
    replica_evidence: list[dict[str, object]] = []
    reference_families: dict[str, str] | None = None
    for pair in pairs:
        try:
            start_text = pair.start_snapshot.decode("utf-8")
            end_text = pair.end_snapshot.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SloValidationError("metrics snapshots must be UTF-8 text") from exc
        start_families, start_samples = parse_metrics_snapshot(start_text)
        end_families, end_samples = parse_metrics_snapshot(end_text)
        if reference_families is None:
            reference_families = dict(end_families)
        elif reference_families != dict(end_families):
            raise SloValidationError("API replicas expose divergent metric family/type contracts")
        _validate_runtime_build_info(start_samples, pair=pair)
        _validate_runtime_build_info(end_samples, pair=pair)
        state = _validate_end_state(families=end_families, samples=end_samples, slo=slo)
        reported_replicas = samples_for(
            end_samples, "propertyquarry_expected_api_replicas"
        )
        if (
            len(reported_replicas) != 1
            or not math.isfinite(reported_replicas[0].value)
            or not reported_replicas[0].value.is_integer()
            or int(reported_replicas[0].value) != len(pairs)
        ):
            raise SloValidationError(
                "non-authoritative expected API replica telemetry diverges from "
                "Docker-derived replica coverage"
            )
        state["reported_expected_api_replicas"] = int(reported_replicas[0].value)
        deltas = _monotonic_deltas(
            start_families=start_families,
            start_samples=start_samples,
            end_families=end_families,
            end_samples=end_samples,
        )
        aggregate_deltas.extend(deltas)
        replica_evidence.append(
            {
                **pair.range_binding(),
                "window_start": isoformat(pair.start_at),
                "window_end": isoformat(pair.end_at),
                "metric_family_count": len(end_families),
                "start_sample_count": len(start_samples),
                "end_sample_count": len(end_samples),
                **state,
            }
        )
    slo_values = _slo_delta_values(deltas=aggregate_deltas, slo=slo, window_name="short_window")
    return {
        "replica_count": len(pairs),
        "replicas": replica_evidence,
        "short_window_slos": slo_values,
    }


def _normalized_matrix(result: object) -> list[dict[str, object]]:
    if not isinstance(result, list):
        raise SloValidationError("Prometheus range response matrix is invalid")
    normalized: list[dict[str, object]] = []
    for row in result:
        if not isinstance(row, Mapping) or not isinstance(row.get("metric"), Mapping) or not isinstance(row.get("values"), list):
            raise SloValidationError("Prometheus range response series is invalid")
        metric = {str(key): str(value) for key, value in row["metric"].items()}
        values = list(row["values"])
        normalized.append({"metric": metric, "values": values})
    return sorted(normalized, key=lambda row: json.dumps(row["metric"], sort_keys=True, separators=(",", ":")))


def validate_prometheus_range_evidence(
    *,
    response_path: Path,
    receipt_path: Path,
    release_commit_sha: str,
    release_image_digest: str,
    pairs: Sequence[ReplicaSnapshotPair],
    prometheus_config_path: Path,
    slo: Mapping[str, object],
    now: datetime,
    snapshot_bundle_sha256: str,
    anchor: evidence_contract.TrustAnchor,
    challenge: evidence_contract.EvidenceChallenge,
) -> dict[str, object]:
    response_raw = response_path.read_bytes()
    receipt = _validated_payload_hash(
        load_json(receipt_path, document_name="Prometheus range receipt"),
        label="Prometheus range receipt",
    )
    if receipt.get("schema") != RANGE_RECEIPT_SCHEMA or receipt.get("producer") != RANGE_RECEIPT_PRODUCER:
        raise SloValidationError("Prometheus range receipt schema or producer is invalid")
    if set(receipt) != set(evidence_contract.RANGE_RECEIPT_KEYS):
        raise SloValidationError(
            "Prometheus range receipt fields are not the canonical v2 schema"
        )
    try:
        captured_at = evidence_contract.validate_evidence_time(
            receipt.get("captured_at"),
            field="Prometheus range captured_at",
            now=now,
            challenge=challenge,
        )
        evidence_contract.verify_authenticated_payload(
            receipt,
            domain=evidence_contract.RANGE_DOMAIN,
            anchor=anchor,
            challenge=challenge,
            field="Prometheus range receipt",
        )
    except evidence_contract.EvidenceContractError as exc:
        raise SloValidationError(str(exc)) from exc
    release = receipt.get("release")
    if not isinstance(release, Mapping) or (
        normalize_release_sha(str(release.get("commit_sha") or "")) != release_commit_sha
        or normalize_image_digest(str(release.get("image_digest") or "")) != release_image_digest
    ):
        raise SloValidationError("Prometheus range receipt is not bound to the candidate release")
    try:
        start, end, step_seconds = evidence_contract.validate_range_query_contract(
            receipt.get("query"), expected_expression=PROMETHEUS_RANGE_QUERY
        )
    except evidence_contract.EvidenceContractError as exc:
        raise SloValidationError(str(exc)) from exc
    if end > now + timedelta(seconds=60) or (now - end).total_seconds() > 900:
        raise SloValidationError("Prometheus range proof end is future-dated or stale")
    if captured_at < end - timedelta(seconds=60):
        raise SloValidationError("Prometheus range receipt predates its query window")
    window_seconds = (end - start).total_seconds()
    if window_seconds % step_seconds != 0:
        raise SloValidationError("Prometheus range window is not aligned to the canonical query step")
    expected_sample_count = int(window_seconds // step_seconds) + 1
    if receipt.get("snapshot_bundle_sha256") != snapshot_bundle_sha256:
        raise SloValidationError("Prometheus range receipt is not bound to the fresh snapshot bundle")
    transport = receipt.get("transport")
    if not isinstance(transport, Mapping) or (
        transport.get("endpoint_path") != "/api/v1/query_range"
        or transport.get("authenticated") is not True
        or transport.get("credential_persisted") is not False
        or transport.get("http_status") != 200
        or transport.get("tls_verified") is not True
    ):
        raise SloValidationError("Prometheus range transport proof is incomplete")
    try:
        ipaddress.ip_address(str(transport.get("connected_peer_ip") or ""))
    except ValueError as exc:
        raise SloValidationError("Prometheus range connected peer must be an IP literal") from exc
    if str(receipt.get("prometheus_config_sha256") or "").lower() != sha256_bytes(
        prometheus_config_path.read_bytes()
    ):
        raise SloValidationError("Prometheus range proof is not bound to the release config")
    expected_bindings = sorted(
        (pair.range_binding() for pair in pairs),
        key=lambda row: (row["replica_id"], row["container_id"]),
    )
    expected_ids = [row["replica_id"] for row in expected_bindings]
    if receipt.get("expected_replica_ids") != expected_ids:
        raise SloValidationError("Prometheus range expected replica set diverges from capture")
    observed_bindings = receipt.get("replicas")
    if not isinstance(observed_bindings, list) or sorted(
        observed_bindings,
        key=lambda row: (str(row.get("replica_id") or ""), str(row.get("container_id") or ""))
        if isinstance(row, Mapping)
        else ("", ""),
    ) != expected_bindings:
        raise SloValidationError("Prometheus range container/image/snapshot bindings diverge")
    if (
        str(receipt.get("range_response_sha256") or "").lower() != sha256_bytes(response_raw)
        or _json_nonnegative_integer(
            receipt.get("range_response_bytes"),
            label="Prometheus range response byte count",
        )
        != len(response_raw)
    ):
        raise SloValidationError("Prometheus range raw response hash or size does not match")
    try:
        response = json.loads(response_raw)
    except json.JSONDecodeError as exc:
        raise SloValidationError("Prometheus range response is not valid JSON") from exc
    if not isinstance(response, Mapping) or response.get("status") != "success":
        raise SloValidationError("Prometheus range response did not succeed")
    data = response.get("data")
    if not isinstance(data, Mapping) or data.get("resultType") != "matrix":
        raise SloValidationError("Prometheus range response must be a matrix")
    matrix = _normalized_matrix(data.get("result"))
    series_summary = receipt.get("series")
    if not isinstance(series_summary, Mapping) or (
        series_summary.get("result_type") != "matrix"
        or series_summary.get("count") != len(matrix)
        or str(series_summary.get("sha256") or "").lower() != canonical_json_sha256(matrix)
    ):
        raise SloValidationError("Prometheus range series summary is invalid")
    binding_by_replica = {pair.replica_id: pair for pair in pairs}
    provenance_labels = {
        "replica_id",
        "container_id",
        "release_commit_sha",
        "release_image_digest",
        "instance",
        "job",
        "service",
    }
    range_rows: dict[str, tuple[list[MetricSample], list[MetricSample]]] = {
        pair.replica_id: ([], []) for pair in pairs
    }
    seen_series: set[tuple[tuple[str, str], ...]] = set()
    for row in matrix:
        metric = row["metric"]
        values = row["values"]
        assert isinstance(metric, dict) and isinstance(values, list)
        key = tuple(sorted(metric.items()))
        if key in seen_series:
            raise SloValidationError("Prometheus range response contains duplicate series")
        seen_series.add(key)
        replica_id = str(metric.get("replica_id") or "")
        pair = binding_by_replica.get(replica_id)
        if pair is None or any(
            str(metric.get(label) or "") != expected
            for label, expected in (
                ("container_id", pair.container_id),
                ("release_commit_sha", pair.release_commit_sha),
                ("release_image_digest", pair.release_image_digest),
            )
        ):
            raise SloValidationError("Prometheus range series has divergent replica provenance")
        name = str(metric.get("__name__") or "")
        if name not in {
            "propertyquarry_http_requests_total",
            "propertyquarry_http_request_errors_total",
            "propertyquarry_http_request_duration_seconds_bucket",
            "propertyquarry_http_request_duration_seconds_sum",
            "propertyquarry_http_request_duration_seconds_count",
            "propertyquarry_ingress_admission_operations_total",
            "propertyquarry_runtime_build_info",
        }:
            raise SloValidationError("Prometheus range response contains an uncontracted metric")
        labels = {
            label: value
            for label, value in metric.items()
            if label != "__name__" and label not in provenance_labels
        }
        if len(values) < 2:
            raise SloValidationError("Prometheus range series must contain at least two samples")
        if len(values) != expected_sample_count:
            raise SloValidationError(
                "Prometheus range series is sparse or has a scrape-continuity gap"
            )
        parsed_values: list[tuple[float, float]] = []
        previous_time = -math.inf
        previous_value = -math.inf
        for sample_index, value_row in enumerate(values):
            if not isinstance(value_row, list) or len(value_row) != 2:
                raise SloValidationError("Prometheus range sample is invalid")
            try:
                timestamp = float(value_row[0])
                value = float(value_row[1])
            except (TypeError, ValueError) as exc:
                raise SloValidationError("Prometheus range sample is not numeric") from exc
            if (
                not math.isfinite(timestamp)
                or not math.isfinite(value)
                or value < 0
                or timestamp <= previous_time
            ):
                raise SloValidationError("Prometheus range sample is NaN, negative, or unordered")
            expected_timestamp = start.timestamp() + sample_index * step_seconds
            if abs(timestamp - expected_timestamp) > 1.0:
                raise SloValidationError(
                    "Prometheus range series is not aligned to the canonical query cadence"
                )
            if name == "propertyquarry_runtime_build_info":
                if value != 1.0:
                    raise SloValidationError("Prometheus runtime build identity is invalid")
            elif value < previous_value:
                raise SloValidationError("Prometheus range counter reset detected")
            previous_time = timestamp
            previous_value = value
            parsed_values.append((timestamp, value))
        if abs(parsed_values[0][0] - start.timestamp()) > 1.0 or abs(
            parsed_values[-1][0] - end.timestamp()
        ) > 1.0:
            raise SloValidationError(
                "Prometheus range samples do not cover the complete receipt window"
            )
        start_rows, end_rows = range_rows[replica_id]
        start_rows.append(MetricSample(name, labels, parsed_values[0][1]))
        end_rows.append(MetricSample(name, labels, parsed_values[-1][1]))
    aggregate_deltas: list[MetricSample] = []
    for pair in pairs:
        start_rows, end_rows = range_rows[pair.replica_id]
        build_start = samples_for(start_rows, "propertyquarry_runtime_build_info")
        build_end = samples_for(end_rows, "propertyquarry_runtime_build_info")
        if len(build_start) != 1 or len(build_end) != 1:
            raise SloValidationError("Prometheus range proof lacks one runtime identity per replica")
        _validate_ingress_admission_operation_matrix(
            start_rows,
            window_name="Prometheus range start",
        )
        _validate_ingress_admission_operation_matrix(
            end_rows,
            window_name="Prometheus range end",
        )
        families = {
            "propertyquarry_http_requests_total": "counter",
            "propertyquarry_http_request_errors_total": "counter",
            "propertyquarry_http_request_duration_seconds": "histogram",
            "propertyquarry_ingress_admission_operations_total": "counter",
            "propertyquarry_runtime_build_info": "gauge",
        }
        _validate_histogram_contract(start_rows, "propertyquarry_http_request_duration_seconds")
        _validate_histogram_contract(end_rows, "propertyquarry_http_request_duration_seconds")
        aggregate_deltas.extend(
            _monotonic_deltas(
                start_families=families,
                start_samples=start_rows,
                end_families=families,
                end_samples=end_rows,
            )
        )
    thirty_day = _slo_delta_values(
        deltas=aggregate_deltas,
        slo=slo,
        window_name="prometheus_30d_range",
        require_integrity=False,
    )
    return {
        "schema": RANGE_RECEIPT_SCHEMA,
        "producer": RANGE_RECEIPT_PRODUCER,
        "window_start": isoformat(start),
        "window_end": isoformat(end),
        "window_seconds": (end - start).total_seconds(),
        "step_seconds": step_seconds,
        "authenticated": True,
        "tls_verified": True,
        "credential_persisted": False,
        "replica_ids": expected_ids,
        "range_response_sha256": sha256_bytes(response_raw),
        "receipt_sha256": sha256_bytes(receipt_path.read_bytes()),
        "slo": thirty_day,
    }


def validate_probe(
    *,
    payload: object,
    snapshot: bytes,
    release_commit_sha: str,
    release_image_digest: str,
    maximum_age_seconds: int,
    now: datetime,
) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema") != PROBE_SCHEMA:
        raise SloValidationError(f"metrics probe schema must be {PROBE_SCHEMA}")
    observed_release = normalize_release_sha(str(payload.get("release_commit_sha") or ""))
    if observed_release != release_commit_sha:
        raise SloValidationError("metrics probe is not bound to the candidate release")
    observed_image_digest = normalize_image_digest(
        str(payload.get("release_image_digest") or "")
    )
    if observed_image_digest != release_image_digest:
        raise SloValidationError("metrics probe is not bound to the candidate image digest")
    replica_id = str(payload.get("replica_id") or "").strip()
    if not REPLICA_ID_RE.fullmatch(replica_id):
        raise SloValidationError("metrics probe replica_id is missing or unsafe")
    replica_count = positive_int(
        payload.get("replica_count"), field_name="metrics probe replica_count", default=0
    )
    if replica_count <= 0:
        raise SloValidationError("metrics probe replica_count must be a positive integer")
    captured_at = parse_timestamp(payload.get("captured_at"), field_name="metrics probe captured_at")
    age = (now - captured_at).total_seconds()
    if age < -60:
        raise SloValidationError("metrics probe capture time is in the future")
    if age > maximum_age_seconds:
        raise SloValidationError("metrics probe is too old for flagship evidence")
    if payload.get("endpoint_path") != "/internal/metrics":
        raise SloValidationError("metrics probe must come from /internal/metrics")
    if payload.get("authenticated") is not True or payload.get("private_route") is not True:
        raise SloValidationError("metrics probe must prove authenticated private-route capture")
    if payload.get("http_status") != 200:
        raise SloValidationError("metrics probe HTTP status must be 200")
    if not str(payload.get("content_type") or "").startswith("text/plain"):
        raise SloValidationError("metrics probe content type must be Prometheus text")
    cache_control = str(payload.get("cache_control") or "").strip()
    cache_directives = {
        directive.strip().lower() for directive in cache_control.split(",") if directive.strip()
    }
    if "no-store" not in cache_directives:
        raise SloValidationError("metrics probe must prove Cache-Control: no-store")
    if payload.get("credential_persisted") is not False:
        raise SloValidationError("metrics probe must prove the credential was not persisted")
    observed_hash = str(payload.get("metrics_sha256") or "").strip().lower()
    expected_hash = sha256_bytes(snapshot)
    if observed_hash != expected_hash:
        raise SloValidationError("metrics probe hash does not match the snapshot")
    return {
        "captured_at": isoformat(captured_at),
        "age_seconds": max(0.0, age),
        "endpoint_path": "/internal/metrics",
        "authenticated": True,
        "private_route": True,
        "http_status": 200,
        "content_type": str(payload["content_type"]),
        "cache_control": cache_control,
        "credential_persisted": False,
        "release_commit_sha": observed_release,
        "release_image_digest": observed_image_digest,
        "replica_id": replica_id,
        "replica_count": replica_count,
        "metrics_sha256": expected_hash,
    }


def promtool_command(
    runner: PromtoolRunner, argv: Sequence[str], *, timeout_seconds: int
) -> CommandResult:
    result = runner.run(tuple(argv), timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise PromtoolError(
            f"{argv[0]} {argv[1]} failed with exit code {result.returncode}; raw output was withheld"
        )
    return result


def normalized_tool_version(output: str) -> str:
    match = re.search(r"\b[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+._a-zA-Z0-9]*)?\b", output)
    if not match:
        raise SloValidationError("promtool version output is not recognizable")
    return match.group(0)


def blank_receipt(config: EvidenceConfig) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "generated_at": isoformat(utc_now()),
        "mode": "flagship" if config.flagship else "advisory",
        "status": "initializing",
        "gate_passed": False,
        "release_commit_sha": str(config.release_commit_sha or ""),
        "release_image_digest": str(config.release_image_digest or ""),
        "live_monitoring_contacted": False,
        "inputs": {},
        "probe": {},
        "metrics": {},
        "prometheus_range": {},
        "rules": {},
        "monitoring_config": {},
        "promtool": {
            "available": False,
            "version_pinned": False,
            "rule_check_passed": False,
            "config_check_passed": False,
            "injection_test_passed": False,
        },
        "amtool": {
            "available": False,
            "version_pinned": False,
            "routing_check_passed": False,
        },
    }


def run_evidence_gate(
    *,
    config: EvidenceConfig,
    runner: PromtoolRunner | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], int]:
    now = (now or utc_now()).astimezone(timezone.utc)
    receipt = blank_receipt(config)
    exit_code = 0
    try:
        release = normalize_release_sha(config.release_commit_sha)
        image_digest = normalize_image_digest(config.release_image_digest)
        try:
            evidence_anchor, evidence_challenge = evidence_contract.load_evidence_challenge(
                expected_commit_sha=release,
                expected_image_digest=image_digest,
                now=now,
            )
        except evidence_contract.EvidenceContractError as exc:
            raise SloValidationError(str(exc)) from exc
        selected_policy_paths = {
            "slo_definition_sha256": config.slo_path,
            "alert_rules_sha256": config.rules_path,
            "alert_rule_tests_sha256": config.rule_tests_path,
            "prometheus_config_sha256": config.prometheus_config_path,
            "alertmanager_config_sha256": config.alertmanager_config_path,
        }
        if config.flagship:
            for name, selected_path in selected_policy_paths.items():
                canonical_path = evidence_contract.CANONICAL_POLICY_PATHS[name]
                if selected_path.resolve() != canonical_path.resolve():
                    raise SloValidationError(
                        f"flagship launch policy path override is forbidden: {name}"
                    )
                actual_hash = sha256_bytes(selected_path.read_bytes())
                if actual_hash != evidence_challenge.policy_hashes[name]:
                    raise SloValidationError(
                        f"canonical launch policy hash differs from challenge: {name}"
                    )
        canonical_monitoring: Mapping[str, object] | None = None
        if config.flagship:
            try:
                canonical_monitoring = (
                    evidence_contract.load_canonical_monitoring_identity()
                )
            except evidence_contract.EvidenceContractError as exc:
                raise SloValidationError(str(exc)) from exc
            canonical_identity = canonical_monitoring.get("identity")
            if not isinstance(canonical_identity, Mapping) or any(
                canonical_identity.get(name) != evidence_challenge.policy_hashes[name]
                for name in evidence_contract.CANONICAL_POLICY_PATHS
            ):
                raise SloValidationError(
                    "canonical monitoring identity differs from challenge policy"
                )
        timeout = positive_int(config.timeout_seconds, field_name="promtool timeout", default=120)
        slo_payload = validate_slo_document(load_json(config.slo_path, document_name="SLO document"))
        pinned_tools: Mapping[str, monitoring_proof.ToolIdentity] | None = None
        if runner is None:
            try:
                tool_manifest, _tool_manifest_raw = monitoring_proof._load_json(
                    config.tool_manifest_path, name="monitoring tool manifest"
                )
                pinned_tools = monitoring_proof.load_tool_identities(tool_manifest, slo=slo_payload)
            except (
                monitoring_proof.MonitoringProofError,
                monitoring_proof.receipts.ReceiptValidationError,
            ) as exc:
                raise PromtoolError(str(exc)) from exc
            runner = SubprocessPromtoolRunner(
                working_directory=config.rule_tests_path.parent,
                tools=pinned_tools,
            )
        if canonical_monitoring is not None and pinned_tools is not None:
            observed_tools = {
                name: monitoring_proof.tool_identity_receipt(identity)
                for name, identity in pinned_tools.items()
            }
            if observed_tools != canonical_monitoring.get("monitoring_tools"):
                raise PromtoolError(
                    "SLO monitoring tools differ from canonical fd-bound identities"
                )
        required_alerts = [str(item) for item in slo_payload["required_alerts"]]
        rule_evidence = validate_rule_documents(
            config.rules_path,
            config.rule_tests_path,
            required_alerts=required_alerts,
        )
        monitoring_config_evidence = validate_monitoring_configs(
            config.prometheus_config_path,
            config.alertmanager_config_path,
            rules_path=config.rules_path,
        )
        snapshot_pairs, probe_evidence = validate_snapshot_bundle(
            snapshot_bundle_path=config.metrics_snapshot_path,
            probe_bundle_path=config.metrics_probe_path,
            release_commit_sha=release,
            release_image_digest=image_digest,
            maximum_age_seconds=int(slo_payload["probe_max_age_seconds"]),
            now=now,
        )
        metrics_evidence = validate_short_window_metrics(
            pairs=snapshot_pairs,
            slo=slo_payload,
        )
        if (
            config.prometheus_range_path is None
            or config.prometheus_range_receipt_path is None
        ):
            raise SloValidationError(
                "authenticated 30-day Prometheus range response and receipt are required"
            )
        range_evidence = validate_prometheus_range_evidence(
            response_path=config.prometheus_range_path,
            receipt_path=config.prometheus_range_receipt_path,
            release_commit_sha=release,
            release_image_digest=image_digest,
            pairs=snapshot_pairs,
            prometheus_config_path=config.prometheus_config_path,
            slo=slo_payload,
            now=now,
            snapshot_bundle_sha256=sha256_bytes(config.metrics_snapshot_path.read_bytes()),
            anchor=evidence_anchor,
            challenge=evidence_challenge,
        )

        receipt["release_commit_sha"] = release
        receipt["release_image_digest"] = image_digest
        receipt["inputs"] = {
            "slo": file_identity(config.slo_path),
            "metrics_snapshot_bundle": file_identity(config.metrics_snapshot_path),
            "metrics_probe_bundle": file_identity(config.metrics_probe_path),
            "replica_snapshots": [
                {
                    "replica_id": pair.replica_id,
                    "container_id": pair.container_id,
                    "start": file_identity(pair.start_path),
                    "end": file_identity(pair.end_path),
                }
                for pair in snapshot_pairs
            ],
            "prometheus_range_response": file_identity(config.prometheus_range_path),
            "prometheus_range_receipt": file_identity(
                config.prometheus_range_receipt_path
            ),
        }
        receipt["probe"] = probe_evidence
        receipt["metrics"] = {
            "required_families": sorted(str(item) for item in slo_payload["required_metric_families"]),
            **metrics_evidence,
        }
        receipt["prometheus_range"] = range_evidence
        receipt["authenticated_evidence"] = {
            "deployment_id": evidence_challenge.deployment_id,
            "challenge_sha256": evidence_challenge.artifact_sha256,
            "trust_anchor_sha256": evidence_anchor.file_sha256,
            "policy_hashes": dict(evidence_challenge.policy_hashes),
        }
        if canonical_monitoring is not None:
            receipt["canonical_monitoring_identity"] = dict(
                canonical_monitoring["identity"]  # type: ignore[arg-type]
            )
            receipt["monitoring_tools"] = dict(
                canonical_monitoring["monitoring_tools"]  # type: ignore[arg-type]
            )
        if config.shared_input_hashes is not None:
            required_shared_inputs = {
                "metrics_snapshot",
                "metrics_probe",
                "monitoring_receipt",
                "prometheus_range_receipt",
                "prometheus_range_response",
                "alert_delivery_receipt",
            }
            if (
                set(config.shared_input_hashes) != required_shared_inputs
                or config.shared_input_paths is None
                or set(config.shared_input_paths) != required_shared_inputs
                or any(
                    not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                    for value in config.shared_input_hashes.values()
                )
            ):
                raise SloValidationError("shared launch input hash set is invalid")
            expected_primary_paths = {
                "metrics_snapshot": config.metrics_snapshot_path,
                "metrics_probe": config.metrics_probe_path,
                "prometheus_range_receipt": config.prometheus_range_receipt_path,
                "prometheus_range_response": config.prometheus_range_path,
            }
            if any(
                expected is None
                or config.shared_input_paths[name].resolve() != expected.resolve()
                for name, expected in expected_primary_paths.items()
            ):
                raise SloValidationError("shared launch input paths differ from SLO inputs")
            recomputed_shared_hashes = {
                name: sha256_bytes(path.read_bytes())
                for name, path in config.shared_input_paths.items()
            }
            if recomputed_shared_hashes != dict(config.shared_input_hashes):
                raise SloValidationError("shared launch input bytes differ from pinned hashes")
            receipt["shared_input_hashes"] = recomputed_shared_hashes
        receipt["rules"] = rule_evidence
        receipt["monitoring_config"] = monitoring_config_evidence

        missing_tools = [
            tool for tool in ("promtool", "amtool") if not runner.available(tool)
        ]
        if missing_tools:
            if config.flagship:
                raise PromtoolError(
                    "preinstalled pinned monitoring tools are required for flagship evidence: "
                    + ", ".join(missing_tools)
                )
            receipt["status"] = "advisory_unavailable"
            receipt["error"] = {
                "type": "PromtoolUnavailable",
                "message": "monitoring tools are unavailable; rules and routing were not fully tested",
            }
            return receipt, 0
        receipt["promtool"]["available"] = True
        receipt["amtool"]["available"] = True
        toolchain = slo_payload["monitoring_toolchain"]
        assert isinstance(toolchain, dict)
        version_result = promtool_command(
            runner, ("promtool", "--version"), timeout_seconds=timeout
        )
        promtool_version = normalized_tool_version(version_result.stdout)
        if promtool_version != str(toolchain["promtool_version"]):
            raise PromtoolError(
                f"promtool version {promtool_version} does not match the pinned release version"
            )
        receipt["promtool"]["version"] = promtool_version
        receipt["promtool"]["version_pinned"] = True
        receipt["promtool"]["version_output"] = output_evidence(version_result.stdout)
        amtool_version_result = promtool_command(
            runner, ("amtool", "--version"), timeout_seconds=timeout
        )
        amtool_version = normalized_tool_version(amtool_version_result.stdout)
        if amtool_version != str(toolchain["amtool_version"]):
            raise PromtoolError(
                f"amtool version {amtool_version} does not match the pinned release version"
            )
        receipt["amtool"]["version"] = amtool_version
        receipt["amtool"]["version_pinned"] = True
        receipt["amtool"]["version_output"] = output_evidence(
            amtool_version_result.stdout
        )
        check_result = promtool_command(
            runner,
            ("promtool", "check", "rules", str(config.rules_path)),
            timeout_seconds=timeout,
        )
        receipt["promtool"]["rule_check_passed"] = True
        receipt["promtool"]["rule_check_output"] = output_evidence(check_result.stdout)
        config_check_result = promtool_command(
            runner,
            ("promtool", "check", "config", str(config.prometheus_config_path)),
            timeout_seconds=timeout,
        )
        receipt["promtool"]["config_check_passed"] = True
        receipt["promtool"]["config_check_output"] = output_evidence(
            config_check_result.stdout
        )
        test_result = promtool_command(
            runner,
            ("promtool", "test", "rules", str(config.rule_tests_path)),
            timeout_seconds=timeout,
        )
        receipt["promtool"]["injection_test_passed"] = True
        receipt["promtool"]["injection_test_output"] = output_evidence(test_result.stdout)
        receipt["promtool"]["injected_alerts"] = required_alerts
        routing_result = promtool_command(
            runner,
            (
                "amtool",
                "check-config",
                str(config.alertmanager_config_path),
                "--enable-feature=utf8-strict-mode",
            ),
            timeout_seconds=timeout,
        )
        receipt["amtool"]["routing_check_passed"] = True
        receipt["amtool"]["routing_check_output"] = output_evidence(
            routing_result.stdout
        )
        receipt["status"] = "pass"
        receipt["gate_passed"] = True
    except SloValidationError as exc:
        exit_code = 2
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
    except PromtoolError as exc:
        if config.flagship:
            exit_code = 2
            receipt["status"] = "failed"
        else:
            exit_code = 0
            receipt["status"] = "advisory_unavailable"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
    except Exception:
        exit_code = 2 if config.flagship else 0
        receipt["status"] = "failed" if config.flagship else "advisory_unavailable"
        receipt["error"] = {
            "type": "UnexpectedSloEvidenceError",
            "message": "unexpected SLO evidence failure; raw probe and tool output were withheld",
        }
    finally:
        receipt["completed_at"] = isoformat(utc_now())
        atomic_write_json(
            config.receipt_path,
            receipt,
            overwrite=config.overwrite_receipt,
        )
    return receipt, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate offline PropertyQuarry metrics and Prometheus alert evidence."
    )
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--metrics-snapshot", type=Path, required=True)
    parser.add_argument("--metrics-probe", type=Path, required=True)
    parser.add_argument("--prometheus-range", type=Path, required=True)
    parser.add_argument("--prometheus-range-receipt", type=Path, required=True)
    parser.add_argument("--slo", type=Path, default=DEFAULT_SLO_PATH)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--rule-tests", type=Path, default=DEFAULT_RULE_TESTS_PATH)
    parser.add_argument(
        "--prometheus-config", type=Path, default=DEFAULT_PROMETHEUS_CONFIG_PATH
    )
    parser.add_argument(
        "--alertmanager-config", type=Path, default=DEFAULT_ALERTMANAGER_CONFIG_PATH
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--flagship", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--overwrite-receipt", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = EvidenceConfig(
        release_commit_sha=args.release_sha,
        release_image_digest=args.image_digest,
        metrics_snapshot_path=args.metrics_snapshot,
        metrics_probe_path=args.metrics_probe,
        prometheus_range_path=args.prometheus_range,
        prometheus_range_receipt_path=args.prometheus_range_receipt,
        slo_path=args.slo,
        rules_path=args.rules,
        rule_tests_path=args.rule_tests,
        prometheus_config_path=args.prometheus_config,
        alertmanager_config_path=args.alertmanager_config,
        receipt_path=args.receipt,
        flagship=args.flagship,
        timeout_seconds=args.timeout_seconds,
        overwrite_receipt=args.overwrite_receipt,
    )
    try:
        receipt, exit_code = run_evidence_gate(config=config)
    except SloValidationError as exc:
        print(f"PropertyQuarry SLO receipt error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"status": receipt.get("status"), "receipt": str(config.receipt_path)},
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
