#!/usr/bin/env python3
"""Prove PropertyQuarry's loaded monitoring topology and alert delivery.

This is an intentionally active, release-time proof.  It queries only
tracked private endpoints, checks every configured API replica, injects one
release-bound synthetic alert into an isolated proof route, and persists the
private receiver receipt.  It never selects tools or endpoints from PATH,
environment variables, or command-line URL overrides.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import yaml

_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))
from scripts import propertyquarry_observability_receipts as receipts
from scripts import propertyquarry_evidence_contract as evidence_contract


APP_ROOT = _IMPORT_ROOT
MONITORING_ROOT = APP_ROOT / "config" / "monitoring"
SOURCE_TOPOLOGY_PATH = MONITORING_ROOT / "propertyquarry_monitoring_topology.v1.json"
SOURCE_TOOL_MANIFEST_PATH = MONITORING_ROOT / "propertyquarry_monitoring_tools.v1.json"
DEFAULT_TOPOLOGY_PATH = evidence_contract.DEFAULT_MONITORING_TOPOLOGY_PATH
DEFAULT_TOOL_MANIFEST_PATH = evidence_contract.DEFAULT_MONITORING_TOOL_MANIFEST_PATH
DEFAULT_SLO_PATH = MONITORING_ROOT / "propertyquarry_slo.v1.json"
DEFAULT_PROMETHEUS_CONFIG_PATH = MONITORING_ROOT / "propertyquarry_prometheus.v1.yml"
DEFAULT_ALERTMANAGER_CONFIG_PATH = MONITORING_ROOT / "propertyquarry_alertmanager.v1.yml"
DEFAULT_ALERT_RULES_PATH = MONITORING_ROOT / "propertyquarry_alert_rules.v1.yml"
DEFAULT_ALERT_RULE_TESTS_PATH = MONITORING_ROOT / "propertyquarry_alert_rule_tests.v1.yml"
DEFAULT_FLAGSHIP_OPERATIONS_PATH = evidence_contract.CANONICAL_POLICY_PATHS[
    "flagship_operations_sha256"
]
TOPOLOGY_SCHEMA = "propertyquarry.monitoring-topology.v1"
TOOL_SCHEMA = "propertyquarry.monitoring-tools.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_SECRET_BYTES = 4096


class MonitoringProofError(RuntimeError):
    """The runtime monitoring proof cannot establish launch-safe evidence."""


class HttpRequestError(MonitoringProofError):
    """A protected monitoring request failed."""


class _StrictYamlLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictYamlLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise MonitoringProofError(f"duplicate YAML key is forbidden: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml_text(payload: str, *, name: str) -> Mapping[str, object]:
    try:
        parsed = yaml.load(payload, Loader=_StrictYamlLoader)
    except (yaml.YAMLError, MonitoringProofError) as exc:
        raise MonitoringProofError(f"{name} is not strict YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MonitoringProofError(f"{name} must be a YAML mapping")
    return parsed


def load_yaml(path: Path, *, name: str) -> Mapping[str, object]:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MonitoringProofError(f"{name} is unreadable: {path}") from exc
    return _load_yaml_text(payload, name=name)


def _load_json(path: Path, *, name: str) -> tuple[Mapping[str, object], bytes]:
    payload, raw = receipts.load_json_receipt(path, name=name)
    return payload, raw


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MonitoringProofError(f"{field} must be an object")
    return value


def _list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise MonitoringProofError(f"{field} must be an array")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonitoringProofError(f"{field} must be a non-empty trimmed string")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise MonitoringProofError(
            f"{field} keys do not match v1 contract; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MonitoringProofError(f"required artifact is unreadable: {path}") from exc


def canonical_yaml_sha256(value: Mapping[str, object]) -> str:
    return receipts.sha256_bytes(receipts.canonical_json_bytes(value))


def _private_base_url(raw: object, *, field: str) -> str:
    value = _text(raw, field=field)
    if value == "UNCONFIGURED":
        raise MonitoringProofError(f"{field} remains UNCONFIGURED")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MonitoringProofError(f"{field} is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MonitoringProofError(f"{field} must use http or https and include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise MonitoringProofError(f"{field} must not contain credentials, path, query, or fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname != "localhost":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise MonitoringProofError(f"{field} must use localhost or a private IP literal") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_unspecified
            or address.is_multicast
            or not (address.is_loopback or address.is_private)
        ):
            raise MonitoringProofError(f"{field} must use localhost or a private IP literal")
        hostname = address.compressed
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))


def _private_scrape_url(raw: object, *, field: str) -> tuple[str, str]:
    value = _text(raw, field=field)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MonitoringProofError(f"{field} is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/internal/metrics"
        or parsed.query
        or parsed.fragment
    ):
        raise MonitoringProofError(f"{field} is not the direct private metrics endpoint")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise MonitoringProofError(f"{field} must use a direct private IP literal") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_unspecified
        or address.is_multicast
        or not (address.is_loopback or address.is_private)
    ):
        raise MonitoringProofError(f"{field} must use a private IP literal")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    instance = f"{host}:{port}" if port is not None else host
    return value, instance


def _absolute_secret_path(raw: object, *, field: str) -> str:
    value = _text(raw, field=field)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise MonitoringProofError(f"{field} must be a canonical absolute path")
    return str(path)


@dataclass(frozen=True)
class SecretIdentity:
    value: str = field(repr=False)
    value_bytes: bytes = field(repr=False)
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _load_secret_identity(
    path_text: str,
    *,
    field: str,
    _test_allow_insecure: bool = False,
) -> SecretIdentity:
    path = Path(path_text)
    if not path.is_absolute():
        raise MonitoringProofError(f"{field} path must be absolute")
    try:
        if not _test_allow_insecure:
            evidence_contract.assert_secure_external_parent(path, field=field)
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, evidence_contract.EvidenceContractError) as exc:
        raise MonitoringProofError(f"{field} is unavailable or externally unsafe") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or (not _test_allow_insecure and before.st_uid != 0)
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_SECRET_BYTES
        ):
            raise MonitoringProofError(f"{field} ownership, permissions, or size are invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, MAX_SECRET_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as exc:
            raise MonitoringProofError(f"{field} changed while it was read") from exc
        if (
            len(raw) != before.st_size
            or any(
                getattr(before, name) != getattr(after, name)
                for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            )
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
        ):
            raise MonitoringProofError(f"{field} changed while it was read")
    finally:
        os.close(fd)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise MonitoringProofError(f"{field} is not UTF-8") from exc
    if not value or "\n" in value or "\r" in value:
        raise MonitoringProofError(f"{field} has invalid content")
    value_bytes = value.encode("utf-8")
    return SecretIdentity(
        value=value,
        value_bytes=value_bytes,
        device=before.st_dev,
        inode=before.st_ino,
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
    )


def _load_secret(
    path_text: str,
    *,
    field: str,
    _test_allow_insecure: bool = False,
) -> str:
    return _load_secret_identity(
        path_text,
        field=field,
        _test_allow_insecure=_test_allow_insecure,
    ).value


def _assert_secret_path_identity(
    path_text: str,
    identity: SecretIdentity,
    *,
    field: str,
) -> None:
    try:
        current = Path(path_text).lstat()
    except OSError as exc:
        raise MonitoringProofError(f"{field} changed while secrets were compared") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != identity.device
        or current.st_ino != identity.inode
        or current.st_size != identity.size
        or current.st_mtime_ns != identity.mtime_ns
        or current.st_ctime_ns != identity.ctime_ns
    ):
        raise MonitoringProofError(f"{field} changed while secrets were compared")


@dataclass(frozen=True)
class Topology:
    prometheus_base_url: str
    alertmanager_base_url: str
    proof_receiver_base_url: str
    operator_gateway_base_url: str
    proof_receiver_key_id: str
    proof_receiver_audience: str
    operator_gateway_key_id: str
    operator_gateway_audience: str
    operator_gateway_tls_spki_sha256: str
    expected_replica_ids: tuple[str, ...]
    prometheus_api_token_file: str
    alertmanager_api_token_file: str
    proof_receiver_token_file: str
    operator_gateway_api_token_file: str
    operator_webhook_url_file: str
    proof_webhook_url_file: str


def validate_topology(
    payload: Mapping[str, object],
    *,
    require_configured: bool = True,
    operator_gateway_trust: evidence_contract.OperatorGatewayTrust | None = None,
) -> Topology | None:
    _exact_keys(
        payload,
        {"schema_version", "service", "mode", "network_scope", "images", "endpoints", "identities", "targets", "secrets"},
        field="monitoring topology",
    )
    if (
        payload["schema_version"] != TOPOLOGY_SCHEMA
        or payload["service"] != "propertyquarry"
        or payload["mode"] != "protected_external"
        or payload["network_scope"] != "private_only"
    ):
        raise MonitoringProofError("monitoring topology identity or protection mode is invalid")
    images = _mapping(payload["images"], field="monitoring topology.images")
    _exact_keys(images, {"prometheus", "alertmanager"}, field="monitoring topology.images")
    for name in ("prometheus", "alertmanager"):
        image = _mapping(images[name], field=f"monitoring topology.images.{name}")
        _exact_keys(image, {"repository", "digest"}, field=f"monitoring topology.images.{name}")
        _text(image["repository"], field=f"monitoring topology.images.{name}.repository")
        digest = _text(image["digest"], field=f"monitoring topology.images.{name}.digest")
        if require_configured and not IMAGE_DIGEST_RE.fullmatch(digest):
            raise MonitoringProofError(f"monitoring topology image {name} lacks an immutable digest")

    endpoints = _mapping(payload["endpoints"], field="monitoring topology.endpoints")
    _exact_keys(
        endpoints,
        {"prometheus_base_url", "alertmanager_base_url", "proof_receiver_base_url", "operator_gateway_base_url"},
        field="monitoring topology.endpoints",
    )
    identities = _mapping(payload["identities"], field="monitoring topology.identities")
    _exact_keys(
        identities,
        {"proof_receiver", "operator_gateway"},
        field="monitoring topology.identities",
    )
    proof_identity = _mapping(
        identities["proof_receiver"], field="monitoring topology proof identity"
    )
    operator_identity = _mapping(
        identities["operator_gateway"], field="monitoring topology operator identity"
    )
    _exact_keys(
        proof_identity,
        {"key_id", "audience", "endpoint_origin"},
        field="monitoring topology proof identity",
    )
    _exact_keys(
        operator_identity,
        {"key_id", "audience", "endpoint_origin", "tls_spki_sha256"},
        field="monitoring topology operator identity",
    )
    targets = _mapping(payload["targets"], field="monitoring topology.targets")
    _exact_keys(targets, {"file_sd_path", "expected_replica_ids"}, field="monitoring topology.targets")
    if targets["file_sd_path"] != "/etc/prometheus/propertyquarry_targets.json":
        raise MonitoringProofError("monitoring topology file_sd path is not canonical")
    raw_ids = _list(targets["expected_replica_ids"], field="monitoring topology.targets.expected_replica_ids")
    expected_ids: list[str] = []
    for index, raw in enumerate(raw_ids):
        replica_id = _text(raw, field=f"monitoring topology.targets.expected_replica_ids[{index}]")
        if not REPLICA_ID_RE.fullmatch(replica_id):
            raise MonitoringProofError("monitoring topology contains an invalid replica ID")
        expected_ids.append(replica_id)
    if not expected_ids or expected_ids != sorted(set(expected_ids)):
        raise MonitoringProofError("expected replica IDs must be non-empty, sorted, and unique")
    if require_configured and "UNCONFIGURED" in expected_ids:
        raise MonitoringProofError("expected replica IDs remain UNCONFIGURED")

    secret_keys = {
        "metrics_token_file",
        "prometheus_api_token_file",
        "alertmanager_api_token_file",
        "operator_webhook_url_file",
        "proof_webhook_url_file",
        "operator_gateway_api_token_file",
        "proof_receiver_token_file",
        "proof_receiver_instance_file",
    }
    secret_values = _mapping(payload["secrets"], field="monitoring topology.secrets")
    _exact_keys(secret_values, secret_keys, field="monitoring topology.secrets")
    normalized_secrets = {
        name: _absolute_secret_path(secret_values[name], field=f"monitoring topology.secrets.{name}")
        for name in secret_keys
    }
    if not require_configured:
        return None
    if operator_gateway_trust is None:
        raise MonitoringProofError("pinned operator gateway trust is required")
    try:
        proof_origin, proof_socket = evidence_contract.canonical_endpoint_origin(
            endpoints["proof_receiver_base_url"],
            field="proof receiver endpoint",
            require_https=False,
        )
        operator_origin, operator_socket = evidence_contract.canonical_endpoint_origin(
            endpoints["operator_gateway_base_url"],
            field="operator gateway endpoint",
            require_https=True,
        )
        proof_identity_origin, proof_identity_socket = evidence_contract.canonical_endpoint_origin(
            proof_identity["endpoint_origin"],
            field="proof receiver identity endpoint",
            require_https=False,
        )
        operator_identity_origin, operator_identity_socket = evidence_contract.canonical_endpoint_origin(
            operator_identity["endpoint_origin"],
            field="operator gateway identity endpoint",
            require_https=True,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise MonitoringProofError(str(exc)) from exc
    proof_key_id = _text(proof_identity["key_id"], field="proof receiver key ID")
    proof_audience = _text(proof_identity["audience"], field="proof receiver audience")
    operator_key_id = _text(operator_identity["key_id"], field="operator gateway key ID")
    operator_audience = _text(operator_identity["audience"], field="operator gateway audience")
    operator_spki = _text(
        operator_identity["tls_spki_sha256"], field="operator gateway TLS SPKI"
    )
    if (
        "UNCONFIGURED" in {proof_key_id, proof_audience, operator_key_id, operator_audience, operator_spki}
        or proof_key_id == operator_key_id
        or proof_audience == operator_audience
        or proof_socket == operator_socket
        or proof_identity_socket == operator_identity_socket
        or proof_origin != proof_identity_origin
        or operator_origin != operator_identity_origin
    ):
        raise MonitoringProofError(
            "proof receiver and operator gateway endpoints, keys, and audiences must be distinct"
        )
    if (
        operator_key_id != operator_gateway_trust.key_id
        or operator_audience != operator_gateway_trust.audience
        or operator_origin != operator_gateway_trust.endpoint_origin
        or operator_spki != operator_gateway_trust.tls_spki_sha256
    ):
        raise MonitoringProofError("monitoring topology operator gateway identity is not pinned")
    if (
        normalized_secrets["operator_webhook_url_file"]
        == normalized_secrets["proof_webhook_url_file"]
        or normalized_secrets["operator_gateway_api_token_file"]
        == normalized_secrets["proof_receiver_token_file"]
    ):
        raise MonitoringProofError(
            "operator gateway and proof receiver must not share URL or token secret paths"
        )
    return Topology(
        prometheus_base_url=_private_base_url(endpoints["prometheus_base_url"], field="prometheus endpoint"),
        alertmanager_base_url=_private_base_url(endpoints["alertmanager_base_url"], field="Alertmanager endpoint"),
        proof_receiver_base_url=proof_origin,
        operator_gateway_base_url=operator_origin,
        proof_receiver_key_id=proof_key_id,
        proof_receiver_audience=proof_audience,
        operator_gateway_key_id=operator_key_id,
        operator_gateway_audience=operator_audience,
        operator_gateway_tls_spki_sha256=operator_spki,
        expected_replica_ids=tuple(expected_ids),
        prometheus_api_token_file=normalized_secrets["prometheus_api_token_file"],
        alertmanager_api_token_file=normalized_secrets["alertmanager_api_token_file"],
        proof_receiver_token_file=normalized_secrets["proof_receiver_token_file"],
        operator_gateway_api_token_file=normalized_secrets["operator_gateway_api_token_file"],
        operator_webhook_url_file=normalized_secrets["operator_webhook_url_file"],
        proof_webhook_url_file=normalized_secrets["proof_webhook_url_file"],
    )


def validate_webhook_secret_bindings(topology: Topology) -> None:
    """Bind the actual Alertmanager URL secret to the pinned external gateway."""

    configured = (
        (
            _load_secret(
                topology.operator_webhook_url_file,
                field="operator gateway webhook URL file",
            ),
            topology.operator_gateway_base_url,
            True,
            "operator gateway webhook URL",
        ),
        (
            _load_secret(
                topology.proof_webhook_url_file,
                field="proof receiver webhook URL file",
            ),
            topology.proof_receiver_base_url,
            False,
            "proof receiver webhook URL",
        ),
    )
    for raw_url, expected_origin, require_https, field in configured:
        try:
            parsed = urllib.parse.urlsplit(raw_url)
            if (
                parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path != "/v1/alerts"
            ):
                raise evidence_contract.EvidenceContractError(
                    f"{field} must use the canonical /v1/alerts path"
                )
            origin, _socket_identity = evidence_contract.canonical_endpoint_origin(
                urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
                field=field,
                require_https=require_https,
            )
        except (ValueError, evidence_contract.EvidenceContractError) as exc:
            raise MonitoringProofError(str(exc)) from exc
        if origin != expected_origin:
            raise MonitoringProofError(f"{field} does not target its pinned endpoint")


def validate_distinct_token_secrets(
    topology: Topology,
    *,
    _test_allow_insecure: bool = False,
) -> None:
    operator = _load_secret_identity(
        topology.operator_gateway_api_token_file,
        field="operator gateway API token file",
        _test_allow_insecure=_test_allow_insecure,
    )
    proof = _load_secret_identity(
        topology.proof_receiver_token_file,
        field="proof receiver API token file",
        _test_allow_insecure=_test_allow_insecure,
    )
    _assert_secret_path_identity(
        topology.operator_gateway_api_token_file,
        operator,
        field="operator gateway API token file",
    )
    _assert_secret_path_identity(
        topology.proof_receiver_token_file,
        proof,
        field="proof receiver API token file",
    )
    if (
        (operator.device, operator.inode) == (proof.device, proof.inode)
        or hmac.compare_digest(operator.value_bytes, proof.value_bytes)
    ):
        raise MonitoringProofError(
            "operator gateway and proof receiver tokens must be distinct files and values"
        )


def _critical_prometheus_projection(config: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "rule_files": config.get("rule_files"),
        "alerting": config.get("alerting"),
        "scrape_configs": config.get("scrape_configs"),
    }


def validate_prometheus_config(config: Mapping[str, object]) -> None:
    rule_files = _list(config.get("rule_files"), field="Prometheus rule_files")
    if rule_files != ["propertyquarry_alert_rules.v1.yml"]:
        raise MonitoringProofError("Prometheus must load exactly the versioned PropertyQuarry rule file")
    scrape_configs = _list(config.get("scrape_configs"), field="Prometheus scrape_configs")
    if len(scrape_configs) != 1:
        raise MonitoringProofError("isolated Prometheus config must contain exactly one active scrape job")
    job = _mapping(scrape_configs[0], field="Prometheus propertyquarry scrape job")
    if job.get("job_name") != "propertyquarry" or job.get("metrics_path") != "/internal/metrics" or job.get("scheme") != "http":
        raise MonitoringProofError("PropertyQuarry scrape job identity/path/scheme is invalid")
    for forbidden in ("dns_sd_configs", "static_configs", "kubernetes_sd_configs", "consul_sd_configs"):
        if forbidden in job:
            raise MonitoringProofError(f"PropertyQuarry scrape job must not use {forbidden}")
    authorization = _mapping(job.get("authorization"), field="Prometheus scrape authorization")
    if authorization != {
        "type": "Bearer",
        "credentials_file": "/run/secrets/propertyquarry_metrics_token",
    }:
        raise MonitoringProofError("Prometheus scrape authorization must use the canonical secret file")
    discovery = _list(job.get("file_sd_configs"), field="Prometheus file_sd_configs")
    if discovery != [{"files": ["/etc/prometheus/propertyquarry_targets.json"], "refresh_interval": "30s"}]:
        raise MonitoringProofError("Prometheus must use the canonical direct per-replica file_sd target file")
    relabel = _list(job.get("relabel_configs"), field="Prometheus relabel_configs")
    if {"target_label": "service", "replacement": "propertyquarry"} not in relabel:
        raise MonitoringProofError("Prometheus scrape job must attach the bounded service label")
    alerting = _mapping(config.get("alerting"), field="Prometheus alerting")
    managers = _list(alerting.get("alertmanagers"), field="Prometheus alertmanagers")
    if len(managers) != 1:
        raise MonitoringProofError("Prometheus must define one active Alertmanager block")
    static = _list(_mapping(managers[0], field="Prometheus Alertmanager block").get("static_configs"), field="Prometheus Alertmanager static configs")
    if static != [{"targets": ["propertyquarry-alertmanager:9093"]}]:
        raise MonitoringProofError("Prometheus Alertmanager route is not the isolated canonical target")


def validate_alertmanager_config(config: Mapping[str, object]) -> None:
    route = _mapping(config.get("route"), field="Alertmanager route")
    if route.get("receiver") != "propertyquarry-operator":
        raise MonitoringProofError("Alertmanager root receiver must remain the operator receiver")
    child_routes = _list(route.get("routes"), field="Alertmanager child routes")
    if len(child_routes) != 2:
        raise MonitoringProofError("Alertmanager must contain exactly proof and operator child routes")
    proof_route = _mapping(child_routes[0], field="Alertmanager proof route")
    operator_route = _mapping(child_routes[1], field="Alertmanager operator route")
    if (
        proof_route.get("receiver") != "propertyquarry-operator"
        or set(_list(proof_route.get("matchers"), field="Alertmanager proof matchers"))
        != {'service="propertyquarry"', 'proof="propertyquarry-release"'}
        or proof_route.get("continue") is not False
        or proof_route.get("group_wait") != "0s"
        or proof_route.get("group_by") != ["proof_nonce"]
    ):
        raise MonitoringProofError("Alertmanager proof route is not isolated and first-match")
    if (
        operator_route.get("receiver") != "propertyquarry-operator"
        or _list(operator_route.get("matchers"), field="Alertmanager operator matchers")
        != ['service="propertyquarry"']
        or operator_route.get("continue") is not False
    ):
        raise MonitoringProofError("Alertmanager operator route is invalid")
    receiver_items = _list(config.get("receivers"), field="Alertmanager receivers")
    receivers_by_name: dict[str, Mapping[str, object]] = {}
    for item in receiver_items:
        receiver = _mapping(item, field="Alertmanager receiver")
        name = _text(receiver.get("name"), field="Alertmanager receiver name")
        if name in receivers_by_name:
            raise MonitoringProofError("Alertmanager receiver names must be unique")
        receivers_by_name[name] = receiver
    if set(receivers_by_name) != {"propertyquarry-operator"}:
        raise MonitoringProofError("Alertmanager receiver set is not canonical")
    operator_webhooks = _list(receivers_by_name["propertyquarry-operator"].get("webhook_configs"), field="Alertmanager operator webhooks")
    if len(operator_webhooks) != 1:
        raise MonitoringProofError("Alertmanager final operator receiver must have one webhook")
    operator = _mapping(operator_webhooks[0], field="Alertmanager operator webhook")
    if "url" in operator or operator.get("url_file") != "/run/secrets/propertyquarry_alert_webhook_url":
        raise MonitoringProofError("Alertmanager operator webhook must use only its secret URL file")
    http_config = _mapping(operator.get("http_config"), field="Alertmanager operator http_config")
    auth = _mapping(http_config.get("authorization"), field="Alertmanager operator authorization")
    if auth != {
        "type": "Bearer",
        "credentials_file": "/run/secrets/propertyquarry_operator_gateway_token",
    }:
        raise MonitoringProofError("Alertmanager final operator acknowledgement authentication is invalid")


def required_alert_names(slo: Mapping[str, object]) -> list[str]:
    raw = _list(slo.get("required_alerts"), field="SLO required_alerts")
    result = [_text(item, field="SLO required alert") for item in raw]
    if not result or len(result) != len(set(result)):
        raise MonitoringProofError("SLO required alerts must be non-empty and unique")
    return result


def validate_rule_config(rules: Mapping[str, object], required_alerts: Sequence[str]) -> None:
    groups = _list(rules.get("groups"), field="Prometheus rule groups")
    alert_expressions: dict[str, str] = {}
    for group_value in groups:
        group = _mapping(group_value, field="Prometheus rule group")
        for raw_rule in _list(group.get("rules"), field="Prometheus group rules"):
            rule = _mapping(raw_rule, field="Prometheus rule")
            if "alert" in rule:
                name = _text(rule["alert"], field="Prometheus alert name")
                if name in alert_expressions:
                    raise MonitoringProofError(f"duplicate Prometheus alert: {name}")
                alert_expressions[name] = _text(rule.get("expr"), field=f"Prometheus alert {name} expression")
    missing = sorted(set(required_alerts) - set(alert_expressions))
    if missing:
        raise MonitoringProofError(f"Prometheus rules omit required alerts: {missing}")
    if alert_expressions.get("PropertyQuarryExpectedReplicaMetricMissing") != (
        'up{job="propertyquarry"} == 1 unless on (job, instance) '
        'propertyquarry_expected_api_replicas{job="propertyquarry"}'
    ):
        raise MonitoringProofError("expected-replica missing-gauge alert is not fail-closed")
    if "count_values" not in alert_expressions.get("PropertyQuarryExpectedReplicaConfigurationDivergent", ""):
        raise MonitoringProofError("expected-replica divergence alert is missing")
    queue_telemetry_expression = " ".join(
        alert_expressions.get(
            "PropertyQuarryQueueTelemetryUnobserved",
            "",
        ).split()
    )
    expected_queue_telemetry_expression = " ".join(
        """
        (
          count(
            (propertyquarry_queue_observed{queue="property_search"} == 1)
            and on (job, instance)
            (
              (propertyquarry_runtime_heartbeat_required{role="worker"} == 1)
              and on (job, instance) (propertyquarry_runtime_heartbeat_present{role="worker"} == 1)
              and on (job, instance) (propertyquarry_runtime_heartbeat_stale{role="worker"} == 0)
            )
          ) or vector(0)
        )
        !=
        (
          count(
            (propertyquarry_runtime_heartbeat_required{role="worker"} == 1)
            and on (job, instance) (propertyquarry_runtime_heartbeat_present{role="worker"} == 1)
            and on (job, instance) (propertyquarry_runtime_heartbeat_stale{role="worker"} == 0)
          ) or vector(0)
        )
        """.split()
    )
    if queue_telemetry_expression != expected_queue_telemetry_expression:
        raise MonitoringProofError(
            "queue-telemetry unobserved alert is not fail-closed"
        )


def validate_static_monitoring_contract(
    *,
    prometheus_config: Mapping[str, object],
    alertmanager_config: Mapping[str, object],
    alert_rules: Mapping[str, object],
    slo: Mapping[str, object],
) -> list[str]:
    validate_prometheus_config(prometheus_config)
    validate_alertmanager_config(alertmanager_config)
    names = required_alert_names(slo)
    validate_rule_config(alert_rules, names)
    return names


@dataclass(frozen=True)
class ToolIdentity:
    name: str
    path: Path
    version: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: Path, timeout_seconds: int) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str], *, cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env={"LANG": "C", "LC_ALL": "C", "PATH": ""},
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MonitoringProofError(f"could not execute pinned monitoring tool: {argv[0]}") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def assert_tool_identity(identity: ToolIdentity) -> None:
    """Revalidate the exact binary inode immediately before every execution."""

    try:
        evidence_contract.assert_secure_external_parent(
            identity.path,
            field=f"pinned monitoring tool {identity.name}",
        )
    except evidence_contract.EvidenceContractError as exc:
        raise MonitoringProofError(str(exc)) from exc
    try:
        before = identity.path.lstat()
    except OSError as exc:
        raise MonitoringProofError(f"pinned monitoring tool is missing: {identity.path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise MonitoringProofError(f"pinned monitoring tool must be a regular non-symlink file: {identity.path}")
    if before.st_uid != 0 or stat.S_IMODE(before.st_mode) & 0o022 or not before.st_mode & stat.S_IXUSR:
        raise MonitoringProofError(f"pinned monitoring tool ownership or permissions are unsafe: {identity.path}")
    if (
        before.st_dev != identity.device
        or before.st_ino != identity.inode
        or before.st_size != identity.size
        or before.st_mtime_ns != identity.mtime_ns
    ):
        raise MonitoringProofError(f"pinned monitoring tool inode changed: {identity.name}")
    if sha256_file(identity.path) != identity.sha256:
        raise MonitoringProofError(f"pinned monitoring tool hash changed: {identity.name}")
    after = identity.path.lstat()
    if any(
        getattr(before, field) != getattr(after, field)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise MonitoringProofError(f"pinned monitoring tool changed during identity validation: {identity.name}")


def tool_identity_receipt(identity: ToolIdentity) -> dict[str, object]:
    return {
        "path": str(identity.path),
        "version": identity.version,
        "sha256": identity.sha256,
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def load_tool_identities(
    payload: Mapping[str, object],
    *,
    slo: Mapping[str, object],
) -> Mapping[str, ToolIdentity]:
    _exact_keys(payload, {"schema_version", "tools"}, field="monitoring tool manifest")
    if payload["schema_version"] != TOOL_SCHEMA:
        raise MonitoringProofError("monitoring tool manifest schema is invalid")
    tools = _mapping(payload["tools"], field="monitoring tool manifest.tools")
    _exact_keys(tools, {"promtool", "amtool"}, field="monitoring tool manifest.tools")
    pinned_versions = _mapping(slo.get("monitoring_toolchain"), field="SLO monitoring_toolchain")
    result: dict[str, ToolIdentity] = {}
    for name in ("promtool", "amtool"):
        spec = _mapping(tools[name], field=f"monitoring tool manifest.tools.{name}")
        _exact_keys(spec, {"path", "version", "sha256"}, field=f"monitoring tool manifest.tools.{name}")
        path = Path(_text(spec["path"], field=f"monitoring tool {name} path"))
        version = _text(spec["version"], field=f"monitoring tool {name} version")
        expected_hash = _text(spec["sha256"], field=f"monitoring tool {name} sha256")
        if not path.is_absolute() or ".." in path.parts:
            raise MonitoringProofError(f"monitoring tool {name} path must be absolute and canonical")
        if not VERSION_RE.fullmatch(version) or version != pinned_versions.get(f"{name}_version"):
            raise MonitoringProofError(f"monitoring tool {name} version differs from the SLO contract")
        if not SHA256_RE.fullmatch(expected_hash):
            raise MonitoringProofError(f"monitoring tool {name} sha256 remains UNCONFIGURED or invalid")
        try:
            evidence_contract.assert_secure_external_parent(
                path,
                field=f"pinned monitoring tool {name}",
            )
        except evidence_contract.EvidenceContractError as exc:
            raise MonitoringProofError(str(exc)) from exc
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MonitoringProofError(f"pinned monitoring tool is missing: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise MonitoringProofError(f"pinned monitoring tool must be a regular non-symlink file: {path}")
        if metadata.st_uid != 0 or not metadata.st_mode & stat.S_IXUSR or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise MonitoringProofError(f"pinned monitoring tool permissions are unsafe: {path}")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise MonitoringProofError(f"pinned monitoring tool hash mismatch: {name}")
        result[name] = ToolIdentity(
            name,
            path,
            version,
            actual_hash,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
    return result


def run_tool_validation(
    *,
    tools: Mapping[str, ToolIdentity],
    runner: CommandRunner,
    prometheus_config_path: Path,
    alertmanager_config_path: Path,
    alert_rules_path: Path,
    alert_rule_tests_path: Path,
    timeout_seconds: int,
) -> None:
    commands = [
        ("promtool", [str(tools["promtool"].path), "--version"]),
        ("amtool", [str(tools["amtool"].path), "--version"]),
        (
            "promtool",
            [
                str(tools["promtool"].path),
                "check",
                "config",
                "--syntax-only",
                str(prometheus_config_path),
            ],
        ),
        ("promtool", [str(tools["promtool"].path), "check", "rules", str(alert_rules_path)]),
        ("promtool", [str(tools["promtool"].path), "test", "rules", str(alert_rule_tests_path)]),
        ("amtool", [str(tools["amtool"].path), "check-config", str(alertmanager_config_path)]),
    ]
    for name, argv in commands:
        assert_tool_identity(tools[name])
        result = runner.run(argv, cwd=MONITORING_ROOT, timeout_seconds=timeout_seconds)
        combined = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            raise MonitoringProofError(f"pinned {name} rejected monitoring configuration")
        if argv[1] == "--version" and not re.search(
            rf"(?<![0-9.]){re.escape(tools[name].version)}(?![0-9.])", combined
        ):
            raise MonitoringProofError(f"pinned {name} binary reports an unexpected version")


class JsonHttpClient(Protocol):
    def request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        token_file: str,
        body: object | None = None,
        allow_empty: bool = False,
    ) -> object | None: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class PrivateJsonHttpClient:
    def __init__(self, *, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self._proxy_handler = urllib.request.ProxyHandler({})
        self._redirect_handler = _RejectRedirects()
        self._opener = urllib.request.build_opener(
            self._proxy_handler,
            self._redirect_handler,
        )

    @staticmethod
    def _verify_connected_socket(response: object, *, base_url: str) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        hostname = str(parsed.hostname or "").rstrip(".").lower()
        try:
            socket_object = response.fp.raw._sock  # type: ignore[attr-defined]
            peer_raw = socket_object.getpeername()[0]
            local_raw = socket_object.getsockname()[0]
            peer = ipaddress.ip_address(peer_raw)
            local = ipaddress.ip_address(local_raw)
        except (AttributeError, IndexError, OSError, TypeError, ValueError) as exc:
            raise HttpRequestError(
                "protected monitoring connection identity is unavailable"
            ) from exc
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        if isinstance(local, ipaddress.IPv6Address) and local.ipv4_mapped is not None:
            local = local.ipv4_mapped
        if peer.is_unspecified or peer.is_multicast or local.is_unspecified:
            raise HttpRequestError("protected monitoring connection used a wildcard address")
        if hostname == "localhost":
            if not peer.is_loopback or not local.is_loopback:
                raise HttpRequestError(
                    "localhost monitoring connection did not use a loopback socket"
                )
            return
        try:
            claimed = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise HttpRequestError("protected monitoring endpoint identity is invalid") from exc
        if isinstance(claimed, ipaddress.IPv6Address) and claimed.ipv4_mapped is not None:
            claimed = claimed.ipv4_mapped
        if peer != claimed:
            raise HttpRequestError(
                "protected monitoring connected peer differs from the configured endpoint"
            )
        if claimed.is_loopback and (not peer.is_loopback or not local.is_loopback):
            raise HttpRequestError(
                "loopback monitoring connection did not use a local socket"
            )

    def request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        token_file: str,
        body: object | None = None,
        allow_empty: bool = False,
    ) -> object | None:
        normalized_base = _private_base_url(base_url, field="monitoring request base URL")
        if not path.startswith("/") or path.startswith("//"):
            raise HttpRequestError("monitoring request path is invalid")
        token = _load_secret(token_file, field="monitoring API token file")
        payload = None if body is None else receipts.canonical_json_bytes(body)
        request = urllib.request.Request(
            normalized_base + path,
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.getcode())
                self._verify_connected_socket(response, base_url=normalized_base)
                raw = response.read(MAX_HTTP_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise HttpRequestError(f"protected monitoring request returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HttpRequestError("protected monitoring request failed") from None
        if status < 200 or status >= 300:
            raise HttpRequestError(f"protected monitoring request returned HTTP {status}")
        if len(raw) > MAX_HTTP_BYTES:
            raise HttpRequestError("protected monitoring response is too large")
        if not raw and allow_empty:
            return None
        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=receipts._unique_object,
                parse_constant=receipts._reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError("protected monitoring response is not strict JSON") from exc


def _prometheus_data(response: object, *, field: str) -> Mapping[str, object]:
    root = _mapping(response, field=field)
    if root.get("status") != "success":
        raise MonitoringProofError(f"{field} did not report success")
    return _mapping(root.get("data"), field=f"{field}.data")


def _parse_timestamp(raw: object, *, field: str) -> datetime:
    try:
        return datetime.fromisoformat(_text(raw, field=field).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise MonitoringProofError(f"{field} is not an ISO-8601 timestamp") from exc


def validate_loaded_prometheus_config(
    response: object,
    *,
    source_config: Mapping[str, object],
) -> str:
    data = _prometheus_data(response, field="Prometheus loaded config")
    loaded_value = data.get("yaml")
    if not isinstance(loaded_value, str) or not loaded_value.strip():
        raise MonitoringProofError("Prometheus loaded config YAML must be non-empty text")
    loaded_text = loaded_value
    loaded = _load_yaml_text(loaded_text, name="Prometheus loaded config")
    validate_prometheus_config(loaded)
    loaded_hash = canonical_yaml_sha256(loaded)
    source_hash = canonical_yaml_sha256(source_config)
    if loaded_hash != source_hash or loaded != source_config:
        raise MonitoringProofError(
            "Prometheus loaded effective config differs from the complete canonical config"
        )
    return loaded_hash


def validate_prometheus_targets(
    response: object,
    *,
    expected_replica_ids: Sequence[str],
    completed_at: datetime,
) -> list[dict[str, object]]:
    data = _prometheus_data(response, field="Prometheus targets")
    active = _list(data.get("activeTargets"), field="Prometheus activeTargets")
    relevant: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(active):
        target = _mapping(raw, field=f"Prometheus activeTargets[{index}]")
        labels = _mapping(target.get("labels"), field=f"Prometheus activeTargets[{index}].labels")
        if labels.get("job") != "propertyquarry":
            continue
        replica_id = _text(labels.get("replica_id"), field="Prometheus target replica_id")
        if replica_id in relevant:
            raise MonitoringProofError(f"Prometheus has duplicate targets for replica {replica_id}")
        if labels.get("service") != "propertyquarry":
            raise MonitoringProofError(f"Prometheus target {replica_id} lacks the service label")
        if target.get("health") != "up" or str(target.get("lastError") or ""):
            raise MonitoringProofError(f"Prometheus target {replica_id} is not healthy")
        scrape_url, instance = _private_scrape_url(target.get("scrapeUrl"), field=f"Prometheus target {replica_id} scrape URL")
        if labels.get("instance") != instance:
            raise MonitoringProofError(f"Prometheus target {replica_id} instance is not its direct address")
        last_scrape = _parse_timestamp(target.get("lastScrape"), field=f"Prometheus target {replica_id} lastScrape")
        age = (completed_at - last_scrape).total_seconds()
        if age < -5 or age > 120:
            raise MonitoringProofError(f"Prometheus target {replica_id} scrape is not fresh")
        relevant[replica_id] = {
            "replica_id": replica_id,
            "instance": instance,
            "health": "up",
            "last_scrape_at": receipts.isoformat(last_scrape),
            "scrape_url_sha256": receipts.sha256_bytes(scrape_url.encode("utf-8")),
        }
    expected = list(expected_replica_ids)
    if sorted(relevant) != expected:
        raise MonitoringProofError(
            f"Prometheus target set differs from topology; expected={expected}, actual={sorted(relevant)}"
        )
    return [relevant[replica_id] for replica_id in expected]


def validate_loaded_rules(
    response: object,
    *,
    required_alerts: Sequence[str],
    source_rules: Mapping[str, object],
) -> str:
    source_alerts: dict[str, str] = {}
    for raw_group in _list(source_rules.get("groups"), field="source rule groups"):
        group = _mapping(raw_group, field="source rule group")
        for raw_rule in _list(group.get("rules"), field="source rules"):
            rule = _mapping(raw_rule, field="source rule")
            if "alert" not in rule:
                continue
            name = _text(rule["alert"], field="source alert name")
            expression = _text(rule.get("expr"), field=f"source alert {name} expression")
            if name in source_alerts:
                raise MonitoringProofError(f"source rules contain duplicate alert: {name}")
            source_alerts[name] = expression
    if set(source_alerts) != set(required_alerts):
        raise MonitoringProofError("source alert set differs from the canonical SLO")
    data = _prometheus_data(response, field="Prometheus rules")
    groups = _list(data.get("groups"), field="Prometheus loaded rule groups")
    loaded: dict[str, Mapping[str, object]] = {}
    for group_value in groups:
        group = _mapping(group_value, field="Prometheus loaded rule group")
        for raw_rule in _list(group.get("rules"), field="Prometheus loaded rules"):
            rule = _mapping(raw_rule, field="Prometheus loaded rule")
            if rule.get("type") != "alerting":
                continue
            name = _text(rule.get("name"), field="Prometheus loaded alert name")
            if name in loaded:
                raise MonitoringProofError(f"Prometheus loaded duplicate alert: {name}")
            if rule.get("health") != "ok" or str(rule.get("lastError") or ""):
                raise MonitoringProofError(f"Prometheus loaded alert is unhealthy: {name}")
            loaded[name] = rule
    if set(loaded) != set(source_alerts):
        raise MonitoringProofError("Prometheus loaded alert set differs from canonical rules")
    for name, expected_expression in source_alerts.items():
        if str(loaded[name].get("query") or "").strip() != expected_expression:
            raise MonitoringProofError(
                f"Prometheus loaded alert expression differs from canonical rules: {name}"
            )
    projection = [
        {"name": name, "health": loaded[name].get("health"), "query": loaded[name].get("query")}
        for name in sorted(required_alerts)
    ]
    return receipts.sha256_bytes(receipts.canonical_json_bytes(projection))


def validate_expected_replica_query(response: object, *, expected_replica_ids: Sequence[str]) -> None:
    data = _prometheus_data(response, field="Prometheus expected replica query")
    if data.get("resultType") != "vector":
        raise MonitoringProofError("expected-replica query did not return a vector")
    result = _list(data.get("result"), field="Prometheus expected replica query result")
    values: dict[str, int] = {}
    for index, raw_series in enumerate(result):
        series = _mapping(raw_series, field=f"expected-replica series[{index}]")
        metric = _mapping(series.get("metric"), field=f"expected-replica series[{index}].metric")
        if metric.get("job") != "propertyquarry" or metric.get("service") != "propertyquarry":
            raise MonitoringProofError("expected-replica series has incorrect bounded labels")
        replica_id = _text(metric.get("replica_id"), field="expected-replica series replica_id")
        raw_value = _list(series.get("value"), field="expected-replica series value")
        if len(raw_value) != 2:
            raise MonitoringProofError("expected-replica query sample is malformed")
        try:
            numeric = float(raw_value[1])
        except (TypeError, ValueError) as exc:
            raise MonitoringProofError("expected-replica query value is not numeric") from exc
        if not numeric.is_integer() or numeric < 1:
            raise MonitoringProofError(f"replica {replica_id} exports an invalid expected count")
        if replica_id in values:
            raise MonitoringProofError(f"replica {replica_id} exports duplicate expected-count series")
        values[replica_id] = int(numeric)
    expected = list(expected_replica_ids)
    if sorted(values) != expected:
        raise MonitoringProofError("expected-replica gauge is missing from one or more replicas")
    if set(values.values()) != {len(expected)}:
        raise MonitoringProofError("expected-replica gauges diverge from the topology replica count")


def validate_loaded_alertmanager_config(
    response: object,
    *,
    source_config: Mapping[str, object],
) -> str:
    status = _mapping(response, field="Alertmanager status")
    cluster = _mapping(status.get("cluster"), field="Alertmanager cluster status")
    if cluster.get("status") != "ready":
        raise MonitoringProofError("Alertmanager cluster is not ready")
    config = _mapping(status.get("config"), field="Alertmanager status config")
    loaded_value = config.get("original")
    if not isinstance(loaded_value, str) or not loaded_value.strip():
        raise MonitoringProofError("Alertmanager loaded config must be non-empty text")
    loaded_text = loaded_value
    loaded = _load_yaml_text(loaded_text, name="Alertmanager loaded config")
    validate_alertmanager_config(loaded)
    if loaded != source_config:
        raise MonitoringProofError("Alertmanager loaded config differs from source control")
    return canonical_yaml_sha256(loaded)


@dataclass(frozen=True)
class ProofConfig:
    release_commit_sha: str
    release_image_digest: str
    receipt_path: Path
    alert_delivery_receipt_path: Path
    metrics_snapshot_path: Path
    topology_path: Path = DEFAULT_TOPOLOGY_PATH
    tool_manifest_path: Path = DEFAULT_TOOL_MANIFEST_PATH
    slo_path: Path = DEFAULT_SLO_PATH
    prometheus_config_path: Path = DEFAULT_PROMETHEUS_CONFIG_PATH
    alertmanager_config_path: Path = DEFAULT_ALERTMANAGER_CONFIG_PATH
    alert_rules_path: Path = DEFAULT_ALERT_RULES_PATH
    alert_rule_tests_path: Path = DEFAULT_ALERT_RULE_TESTS_PATH
    flagship_operations_path: Path = DEFAULT_FLAGSHIP_OPERATIONS_PATH
    command_timeout_seconds: int = 120
    delivery_timeout_seconds: int = 60
    overwrite: bool = False


def _normalize_release(config: ProofConfig) -> tuple[str, str]:
    commit_sha = str(config.release_commit_sha or "").strip().lower()
    image_digest = str(config.release_image_digest or "").strip().lower()
    if not receipts.GIT_SHA_RE.fullmatch(commit_sha):
        raise MonitoringProofError("release commit must be a full lowercase Git SHA")
    if not receipts.IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise MonitoringProofError("release image digest must be immutable sha256")
    if config.receipt_path.resolve() == config.alert_delivery_receipt_path.resolve():
        raise MonitoringProofError("monitoring and alert-delivery receipt paths must differ")
    if config.delivery_timeout_seconds < 1 or config.delivery_timeout_seconds > 60:
        raise MonitoringProofError("delivery timeout must be between 1 and 60 seconds")
    selected_policy_paths = (
        (config.slo_path, DEFAULT_SLO_PATH),
        (config.prometheus_config_path, DEFAULT_PROMETHEUS_CONFIG_PATH),
        (
            config.alertmanager_config_path,
            DEFAULT_ALERTMANAGER_CONFIG_PATH,
        ),
        (config.alert_rules_path, DEFAULT_ALERT_RULES_PATH),
        (
            config.alert_rule_tests_path,
            DEFAULT_ALERT_RULE_TESTS_PATH,
        ),
        (
            config.flagship_operations_path,
            DEFAULT_FLAGSHIP_OPERATIONS_PATH,
        ),
    )
    if any(
        selected.resolve() != canonical.resolve()
        for selected, canonical in selected_policy_paths
    ):
        raise MonitoringProofError("monitoring launch policy path override is forbidden")
    return commit_sha, image_digest


def _post_synthetic_alert(
    *,
    client: JsonHttpClient,
    topology: Topology,
    commit_sha: str,
    image_digest: str,
    nonce: str,
    sent_at: datetime,
    deployment_id: str,
    challenge_sha256: str,
) -> Mapping[str, str]:
    labels = evidence_contract.canonical_release_proof_labels(
        release_commit_sha=commit_sha,
        release_image_digest=image_digest,
        deployment_id=deployment_id,
        nonce=nonce,
        challenge_sha256=challenge_sha256,
    )
    payload = [
        {
            "labels": labels,
            "annotations": {
                "proof_sent_at": receipts.isoformat(sent_at),
                "summary": "PropertyQuarry isolated release alert-delivery proof",
            },
            "startsAt": receipts.isoformat(sent_at),
            "endsAt": receipts.isoformat(sent_at + timedelta(minutes=10)),
            "generatorURL": "http://127.0.0.1/propertyquarry-release-proof",
        }
    ]
    client.request_json(
        "POST",
        topology.alertmanager_base_url,
        "/api/v2/alerts",
        token_file=topology.alertmanager_api_token_file,
        body=payload,
        allow_empty=True,
    )
    return labels


def _poll_delivery_receipt(
    *,
    client: JsonHttpClient,
    topology: Topology,
    nonce: str,
    attempts: int,
    sleeper: Callable[[float], None],
) -> Mapping[str, object]:
    for attempt in range(attempts):
        try:
            response = client.request_json(
                "GET",
                topology.proof_receiver_base_url,
                f"/receipts/{nonce}",
                token_file=topology.proof_receiver_token_file,
            )
            return _mapping(response, field="proof receiver receipt")
        except HttpRequestError:
            if attempt + 1 == attempts:
                break
            sleeper(1.0)
    raise MonitoringProofError("isolated Alertmanager proof delivery was not received before timeout")


def run_monitoring_proof(
    *,
    config: ProofConfig,
    http_client: JsonHttpClient,
    command_runner: CommandRunner,
    clock: Callable[[], datetime] = receipts.utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    signature_provider: evidence_contract.SignatureProvider = evidence_contract.request_release_control_signature,
) -> dict[str, object]:
    commit_sha, image_digest = _normalize_release(config)
    initial_now = clock().astimezone(timezone.utc)
    try:
        anchor, challenge = evidence_contract.load_evidence_challenge(
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            now=initial_now,
        )
        operator_gateway_trust = evidence_contract.load_operator_gateway_trust(
            evidence_anchor=anchor
        )
    except evidence_contract.EvidenceContractError as exc:
        raise MonitoringProofError(str(exc)) from exc
    snapshot_payload, snapshot_raw = receipts.load_json_receipt(
        config.metrics_snapshot_path, name="metrics snapshot bundle"
    )
    try:
        snapshot_sha256, replica_bindings = receipts.validate_snapshot_bundle_identity(
            snapshot_payload,
            snapshot_raw,
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            challenge=challenge,
            now=initial_now,
        )
    except receipts.ReceiptValidationError as exc:
        raise MonitoringProofError(str(exc)) from exc
    for output_path in (config.receipt_path, config.alert_delivery_receipt_path):
        if output_path.exists() and not config.overwrite:
            raise MonitoringProofError(
                f"proof output already exists: {output_path}; use --overwrite only for an intentional rerun"
            )
    topology_payload, topology_raw = _load_json(config.topology_path, name="monitoring topology")
    tool_payload, tool_raw = _load_json(config.tool_manifest_path, name="monitoring tool manifest")
    slo_payload, _ = _load_json(config.slo_path, name="SLO contract")
    prometheus_config = load_yaml(config.prometheus_config_path, name="Prometheus config")
    alertmanager_config = load_yaml(config.alertmanager_config_path, name="Alertmanager config")
    alert_rules = load_yaml(config.alert_rules_path, name="Prometheus alert rules")
    runtime_policy_hashes = {
        "slo_definition_sha256": sha256_file(config.slo_path),
        "prometheus_config_sha256": sha256_file(config.prometheus_config_path),
        "alertmanager_config_sha256": sha256_file(config.alertmanager_config_path),
        "alert_rules_sha256": sha256_file(config.alert_rules_path),
        "alert_rule_tests_sha256": sha256_file(config.alert_rule_tests_path),
        "flagship_operations_sha256": sha256_file(config.flagship_operations_path),
    }
    if runtime_policy_hashes != dict(challenge.policy_hashes):
        raise MonitoringProofError(
            "canonical monitoring policy hashes differ from the signed challenge"
        )
    required_alerts = validate_static_monitoring_contract(
        prometheus_config=prometheus_config,
        alertmanager_config=alertmanager_config,
        alert_rules=alert_rules,
        slo=slo_payload,
    )
    topology = validate_topology(
        topology_payload,
        require_configured=True,
        operator_gateway_trust=operator_gateway_trust,
    )
    if topology is None:
        raise MonitoringProofError("configured topology was not returned")
    validate_webhook_secret_bindings(topology)
    validate_distinct_token_secrets(topology)
    if list(topology.expected_replica_ids) != sorted(replica_bindings):
        raise MonitoringProofError("monitoring topology differs from the fresh snapshot replica inventory")
    tools = load_tool_identities(tool_payload, slo=slo_payload)
    try:
        canonical_monitoring = evidence_contract.load_canonical_monitoring_identity()
    except evidence_contract.EvidenceContractError as exc:
        raise MonitoringProofError(str(exc)) from exc
    canonical_identity = _mapping(
        canonical_monitoring.get("identity"), field="canonical monitoring identity"
    )
    canonical_tools = _mapping(
        canonical_monitoring.get("monitoring_tools"),
        field="canonical monitoring tool identities",
    )
    if (
        receipts.sha256_bytes(topology_raw)
        != canonical_identity.get("topology_contract_sha256")
        or
        receipts.sha256_bytes(tool_raw)
        != canonical_identity.get("tool_manifest_sha256")
        or {
            name: tool_identity_receipt(identity)
            for name, identity in tools.items()
        }
        != canonical_tools
        or any(
            runtime_policy_hashes[name] != canonical_identity.get(name)
            for name in runtime_policy_hashes
        )
    ):
        raise MonitoringProofError(
            "runtime monitoring tools or policies differ from canonical fd-bound identities"
        )
    run_tool_validation(
        tools=tools,
        runner=command_runner,
        prometheus_config_path=config.prometheus_config_path,
        alertmanager_config_path=config.alertmanager_config_path,
        alert_rules_path=config.alert_rules_path,
        alert_rule_tests_path=config.alert_rule_tests_path,
        timeout_seconds=config.command_timeout_seconds,
    )

    started_at = clock().astimezone(timezone.utc)
    loaded_prometheus_hash = validate_loaded_prometheus_config(
        http_client.request_json(
            "GET",
            topology.prometheus_base_url,
            "/api/v1/status/config",
            token_file=topology.prometheus_api_token_file,
        ),
        source_config=prometheus_config,
    )
    target_response = http_client.request_json(
        "GET",
        topology.prometheus_base_url,
        "/api/v1/targets?state=active",
        token_file=topology.prometheus_api_token_file,
    )
    target_checked_at = clock().astimezone(timezone.utc)
    targets = validate_prometheus_targets(
        target_response,
        expected_replica_ids=topology.expected_replica_ids,
        completed_at=target_checked_at,
    )
    targets = [
        {**replica_bindings[str(target["replica_id"])], **target}
        for target in targets
    ]
    query = urllib.parse.quote('propertyquarry_expected_api_replicas{job="propertyquarry"}', safe="")
    validate_expected_replica_query(
        http_client.request_json(
            "GET",
            topology.prometheus_base_url,
            f"/api/v1/query?query={query}",
            token_file=topology.prometheus_api_token_file,
        ),
        expected_replica_ids=topology.expected_replica_ids,
    )
    loaded_rules_hash = validate_loaded_rules(
        http_client.request_json(
            "GET",
            topology.prometheus_base_url,
            "/api/v1/rules?type=alert",
            token_file=topology.prometheus_api_token_file,
        ),
        required_alerts=required_alerts,
        source_rules=alert_rules,
    )
    loaded_alertmanager_hash = validate_loaded_alertmanager_config(
        http_client.request_json(
            "GET",
            topology.alertmanager_base_url,
            "/api/v2/status",
            token_file=topology.alertmanager_api_token_file,
        ),
        source_config=alertmanager_config,
    )
    sent_at = clock().astimezone(timezone.utc)
    nonce = challenge.nonce
    if not receipts.NONCE_RE.fullmatch(nonce):
        raise MonitoringProofError("nonce source did not return 32 lowercase hex characters")
    injected_labels = _post_synthetic_alert(
        client=http_client,
        topology=topology,
        commit_sha=commit_sha,
        image_digest=image_digest,
        nonce=nonce,
        sent_at=sent_at,
        deployment_id=challenge.deployment_id,
        challenge_sha256=challenge.artifact_sha256,
    )
    delivery = _poll_delivery_receipt(
        client=http_client,
        topology=topology,
        nonce=nonce,
        attempts=config.delivery_timeout_seconds,
        sleeper=sleeper,
    )
    validated_delivery = receipts.validate_alert_delivery_receipt(
        delivery,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        operator_gateway_trust=operator_gateway_trust,
        challenge=challenge,
        now=clock().astimezone(timezone.utc),
    )
    if validated_delivery["nonce"] != nonce or validated_delivery["sent_at"] != sent_at:
        raise MonitoringProofError("alert-delivery receipt does not bind the injected synthetic alert")
    expected_labels_hash = receipts.sha256_bytes(receipts.canonical_json_bytes(injected_labels))
    expected_alert_fingerprint = evidence_contract.alert_fingerprint_sha256(
        labels={str(key): str(value) for key, value in injected_labels.items()},
        sent_at=sent_at,
    )
    if (
        validated_delivery["labels_sha256"] != expected_labels_hash
        or validated_delivery["alert_fingerprint_sha256"] != expected_alert_fingerprint
    ):
        raise MonitoringProofError("alert-delivery receipt labels differ from the injected synthetic alert")
    receipts.atomic_write_json(config.alert_delivery_receipt_path, delivery, overwrite=config.overwrite)
    alert_raw = config.alert_delivery_receipt_path.read_bytes()

    completed_at = clock().astimezone(timezone.utc)
    runtime_receipt = receipts.authenticate_payload(
        {
            "schema_version": receipts.MONITORING_SCHEMA,
            "producer": receipts.MONITORING_PRODUCER,
            "captured_at": receipts.isoformat(completed_at),
            "release": {"commit_sha": commit_sha, "image_digest": image_digest},
            "snapshot_bundle_sha256": snapshot_sha256,
            "identity": {
                **canonical_identity,
                "operator_gateway_trust_sha256": operator_gateway_trust.file_sha256,
                "operator_gateway_key_id_sha256": receipts.sha256_bytes(
                    operator_gateway_trust.key_id.encode("utf-8")
                ),
                "operator_gateway_audience_sha256": receipts.sha256_bytes(
                    operator_gateway_trust.audience.encode("utf-8")
                ),
            },
            "prometheus": {
                "loaded_config_sha256": runtime_policy_hashes[
                    "prometheus_config_sha256"
                ],
                "rules_sha256": runtime_policy_hashes["alert_rules_sha256"],
                "expected_replica_ids": list(topology.expected_replica_ids),
                "targets": targets,
            },
            "alertmanager": {
                "loaded_config_sha256": runtime_policy_hashes[
                    "alertmanager_config_sha256"
                ],
                "status": "ready",
                "proof_secret_configured": True,
            },
            "alert_delivery_receipt_sha256": receipts.sha256_bytes(alert_raw),
            "started_at": receipts.isoformat(started_at),
            "completed_at": receipts.isoformat(completed_at),
        },
        domain=evidence_contract.MONITORING_DOMAIN,
        anchor=anchor,
        challenge=challenge,
        signature_provider=signature_provider,
    )
    receipts.validate_monitoring_runtime_receipt(
        runtime_receipt,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        expected_snapshot_bundle_sha256=snapshot_sha256,
        expected_replica_bindings=replica_bindings,
        anchor=anchor,
        operator_gateway_trust=operator_gateway_trust,
        canonical_monitoring_identity=canonical_monitoring,
        challenge=challenge,
        now=completed_at,
    )
    receipts.atomic_write_json(config.receipt_path, runtime_receipt, overwrite=config.overwrite)
    return runtime_receipt


def _positive_int(raw: str, *, field: str, maximum: int) -> int:
    if not str(raw).isdigit() or not 1 <= int(raw) <= maximum:
        raise argparse.ArgumentTypeError(f"{field} must be between 1 and {maximum}")
    return int(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="confirm active private monitoring proof execution")
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--alert-delivery-receipt", type=Path, required=True)
    parser.add_argument("--metrics-snapshot", type=Path, required=True)
    parser.add_argument(
        "--flagship-operations-policy",
        type=Path,
        default=DEFAULT_FLAGSHIP_OPERATIONS_PATH,
    )
    parser.add_argument("--command-timeout-seconds", type=lambda value: _positive_int(value, field="command timeout", maximum=300), default=120)
    parser.add_argument("--delivery-timeout-seconds", type=lambda value: _positive_int(value, field="delivery timeout", maximum=60), default=60)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print("monitoring runtime proof refused: --execute authorization is required", file=sys.stderr)
        return 2
    config = ProofConfig(
        release_commit_sha=args.release_sha,
        release_image_digest=args.image_digest,
        receipt_path=args.receipt,
        alert_delivery_receipt_path=args.alert_delivery_receipt,
        metrics_snapshot_path=args.metrics_snapshot,
        flagship_operations_path=args.flagship_operations_policy,
        command_timeout_seconds=args.command_timeout_seconds,
        delivery_timeout_seconds=args.delivery_timeout_seconds,
        overwrite=args.overwrite,
    )
    try:
        run_monitoring_proof(
            config=config,
            http_client=PrivateJsonHttpClient(timeout_seconds=min(args.command_timeout_seconds, 30)),
            command_runner=SubprocessCommandRunner(),
        )
    except (MonitoringProofError, receipts.ReceiptValidationError) as exc:
        print(f"monitoring runtime proof failed: {exc}", file=sys.stderr)
        return 2
    print(f"monitoring runtime proof passed: {args.receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
