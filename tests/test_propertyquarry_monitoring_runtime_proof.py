from __future__ import annotations

import hashlib
import json
import os
import stat
import copy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest
import yaml

from propertyquarry_evidence_test_support import (
    EvidenceTestAuthority,
    CanonicalMonitoringTestIdentity,
    OperatorGatewayTestAuthority,
    install_test_authority,
    install_test_canonical_monitoring_identity,
    install_test_operator_gateway,
)
from scripts import propertyquarry_alert_proof_receiver as receiver
from scripts import propertyquarry_evidence_contract as contract
from scripts import propertyquarry_monitoring_runtime_proof as proof
from scripts import propertyquarry_observability_receipts as receipts


RELEASE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NONCE = "c" * 32
REPLICA_IDS = ["api-a", "api-b"]
AUTHORITY: EvidenceTestAuthority
GATEWAY: OperatorGatewayTestAuthority
CANONICAL: CanonicalMonitoringTestIdentity
REAL_LOAD_SECRET = proof._load_secret
REAL_VALIDATE_DISTINCT_TOKENS = proof.validate_distinct_token_secrets


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
        nonce=NONCE,
    )
    global GATEWAY
    GATEWAY = install_test_operator_gateway(
        monkeypatch,
        evidence_authority=AUTHORITY,
    )

    def test_secret(path_text: str, *, field: str, _test_allow_insecure: bool = False) -> str:
        del field, _test_allow_insecure
        if path_text.endswith("propertyquarry_alert_webhook_url"):
            return f"{GATEWAY.trust.endpoint_origin}/v1/alerts"
        if path_text.endswith("propertyquarry_alert_proof_webhook_url"):
            return "http://127.0.0.1:9199/v1/alerts"
        return "test-token"

    monkeypatch.setattr(proof, "_load_secret", test_secret)
    monkeypatch.setattr(proof, "validate_distinct_token_secrets", lambda topology: None)
    global CANONICAL
    CANONICAL = install_test_canonical_monitoring_identity(
        monkeypatch,
        directory=tmp_path / "canonical-monitoring",
    )


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> proof.CommandResult:
        self.calls.append(tuple(argv))
        if argv[1] == "--version":
            version = "3.5.0" if argv[0].endswith("promtool") else "0.28.1"
            return proof.CommandResult(0, f"version {version}", "")
        return proof.CommandResult(0, "SUCCESS", "")


class FakeHttpClient:
    def __init__(self, *, target_health: str = "up", omit_gauge_for: str = "") -> None:
        self.target_health = target_health
        self.omit_gauge_for = omit_gauge_for
        self.calls: list[tuple[str, str, str, object | None]] = []
        self.injected: list[dict[str, object]] | None = None

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
        self.calls.append((method, base_url, path, body))
        if path == "/api/v1/status/config":
            source = proof.DEFAULT_PROMETHEUS_CONFIG_PATH.read_text(encoding="utf-8")
            return {"status": "success", "data": {"yaml": source}}
        if path == "/api/v1/targets?state=active":
            return {
                "status": "success",
                "data": {
                    "activeTargets": [
                        {
                            "labels": {
                                "job": "propertyquarry",
                                "service": "propertyquarry",
                                "replica_id": replica_id,
                                "instance": f"10.0.0.{11 + index}:8090",
                            },
                            "health": self.target_health if index == 0 else "up",
                            "lastError": "" if self.target_health == "up" or index else "connection refused",
                            "scrapeUrl": f"http://10.0.0.{11 + index}:8090/internal/metrics",
                            "lastScrape": receipts.isoformat(NOW),
                        }
                        for index, replica_id in enumerate(REPLICA_IDS)
                    ]
                },
            }
        if path.startswith("/api/v1/query?"):
            return {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {
                                "job": "propertyquarry",
                                "service": "propertyquarry",
                                "replica_id": replica_id,
                            },
                            "value": [NOW.timestamp(), "2"],
                        }
                        for replica_id in REPLICA_IDS
                        if replica_id != self.omit_gauge_for
                    ],
                },
            }
        if path == "/api/v1/rules?type=alert":
            slo = json.loads(proof.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))
            source_rules = yaml.safe_load(
                proof.DEFAULT_ALERT_RULES_PATH.read_text(encoding="utf-8")
            )
            expressions = {
                rule["alert"]: rule["expr"]
                for group in source_rules["groups"]
                for rule in group["rules"]
                if "alert" in rule
            }
            return {
                "status": "success",
                "data": {
                    "groups": [
                        {
                            "rules": [
                                {
                                    "type": "alerting",
                                    "name": name,
                                    "health": "ok",
                                    "lastError": "",
                                    "query": expressions[name],
                                }
                                for name in slo["required_alerts"]
                            ]
                        }
                    ]
                },
            }
        if path == "/api/v2/status":
            source = proof.DEFAULT_ALERTMANAGER_CONFIG_PATH.read_text(encoding="utf-8")
            return {"cluster": {"status": "ready"}, "config": {"original": source}}
        if method == "POST" and path == "/api/v2/alerts":
            assert isinstance(body, list)
            self.injected = body
            return None
        if method == "GET" and path == f"/receipts/{NONCE}":
            assert self.injected is not None
            alert = self.injected[0]
            return GATEWAY.acknowledgement(
                evidence_authority=AUTHORITY,
                release_commit_sha=RELEASE_SHA,
                release_image_digest=IMAGE_DIGEST,
                labels={str(key): str(value) for key, value in alert["labels"].items()},
                sent_at=datetime.fromisoformat(
                    str(alert["startsAt"]).replace("Z", "+00:00")
                ),
                delivered_at=NOW,
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _configured_inputs(tmp_path: Path) -> tuple[Path, Path]:
    topology = json.loads(proof.SOURCE_TOPOLOGY_PATH.read_text(encoding="utf-8"))
    topology["images"]["prometheus"]["digest"] = "sha256:" + "1" * 64
    topology["images"]["alertmanager"]["digest"] = "sha256:" + "2" * 64
    topology["endpoints"] = {
        "prometheus_base_url": "http://127.0.0.1:9090",
        "alertmanager_base_url": "http://127.0.0.1:9093",
        "proof_receiver_base_url": "http://127.0.0.1:9199",
        "operator_gateway_base_url": GATEWAY.trust.endpoint_origin,
    }
    topology["identities"] = {
        "proof_receiver": {
            "key_id": "propertyquarry-local-proof-cache-v1",
            "audience": "propertyquarry-proof-cache",
            "endpoint_origin": "http://127.0.0.1:9199",
        },
        "operator_gateway": {
            "key_id": GATEWAY.trust.key_id,
            "audience": GATEWAY.trust.audience,
            "endpoint_origin": GATEWAY.trust.endpoint_origin,
            "tls_spki_sha256": GATEWAY.trust.tls_spki_sha256,
        },
    }
    topology["targets"]["expected_replica_ids"] = REPLICA_IDS
    topology_path = CANONICAL.topology_path
    topology_path.write_text(json.dumps(topology), encoding="utf-8")
    refreshed = contract.compute_canonical_monitoring_identity(
        topology_path=topology_path,
        tool_manifest_path=CANONICAL.tool_manifest_path,
        _test_allow_insecure_tools=True,
    )
    assert isinstance(CANONICAL.payload, dict)
    CANONICAL.payload.clear()
    CANONICAL.payload.update(refreshed)
    return topology_path, CANONICAL.tool_manifest_path


def _config(tmp_path: Path) -> proof.ProofConfig:
    topology, manifest = _configured_inputs(tmp_path)
    snapshot_path = tmp_path / "metrics.json"
    snapshot = receipts.add_payload_sha256(
        {
            "schema": "propertyquarry.metrics_snapshot_bundle.v2",
            "capture_tool": "propertyquarry.slo_metrics_capture.v2",
            "release_commit_sha": RELEASE_SHA,
            "release_image_digest": IMAGE_DIGEST,
            "window_start": receipts.isoformat(NOW),
            "window_end": receipts.isoformat(NOW),
            "window_seconds": 0.0,
            "replica_count": len(REPLICA_IDS),
            "replicas": [
                {
                    "container_id": str(index + 1) * 64,
                    "container_image_id": IMAGE_DIGEST,
                    "replica_id": replica_id,
                    "release_commit_sha": RELEASE_SHA,
                    "release_image_digest": IMAGE_DIGEST,
                    "docker_inspect_sha256": "d" * 64,
                    "start": {
                        "captured_at": receipts.isoformat(NOW),
                        "path": f"{replica_id}.start.prom",
                        "sha256": "e" * 64,
                        "bytes": 1,
                    },
                    "end": {
                        "captured_at": receipts.isoformat(NOW),
                        "path": f"{replica_id}.end.prom",
                        "sha256": "f" * 64,
                        "bytes": 1,
                    },
                }
                for index, replica_id in enumerate(REPLICA_IDS)
            ],
        }
    )
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return proof.ProofConfig(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        receipt_path=tmp_path / "monitoring.json",
        alert_delivery_receipt_path=tmp_path / "alert.json",
        metrics_snapshot_path=snapshot_path,
        topology_path=topology,
        tool_manifest_path=manifest,
        delivery_timeout_seconds=1,
    )


def _fixture_tool_identities(
    payload: dict[str, object], *, slo: dict[str, object]
) -> dict[str, proof.ToolIdentity]:
    del slo
    tools = payload["tools"]
    assert isinstance(tools, dict)
    result: dict[str, proof.ToolIdentity] = {}
    for name in ("promtool", "amtool"):
        spec = tools[name]
        assert isinstance(spec, dict)
        path = Path(str(spec["path"]))
        metadata = path.lstat()
        result[name] = proof.ToolIdentity(
            name=name,
            path=path,
            version=str(spec["version"]),
            sha256=str(spec["sha256"]),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
        )
    return result


def test_source_configs_are_structurally_active_and_protected() -> None:
    prometheus = proof.load_yaml(proof.DEFAULT_PROMETHEUS_CONFIG_PATH, name="Prometheus")
    alertmanager = proof.load_yaml(proof.DEFAULT_ALERTMANAGER_CONFIG_PATH, name="Alertmanager")
    rules = proof.load_yaml(proof.DEFAULT_ALERT_RULES_PATH, name="rules")
    slo = json.loads(proof.DEFAULT_SLO_PATH.read_text(encoding="utf-8"))

    required = proof.validate_static_monitoring_contract(
        prometheus_config=prometheus,
        alertmanager_config=alertmanager,
        alert_rules=rules,
        slo=slo,
    )

    assert "PropertyQuarryExpectedReplicaMetricMissing" in required
    assert "PropertyQuarryExpectedReplicaConfigurationDivergent" in required
    assert "PropertyQuarryQueueTelemetryUnobserved" in required


def test_static_validator_rejects_weakened_queue_telemetry_alert() -> None:
    rules = copy.deepcopy(
        proof.load_yaml(proof.DEFAULT_ALERT_RULES_PATH, name="rules")
    )
    queue_alert = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "PropertyQuarryQueueTelemetryUnobserved"
    )
    queue_alert["expr"] = (
        'propertyquarry_queue_observed{queue="property_search"} == 0'
    )

    with pytest.raises(
        proof.MonitoringProofError,
        match="queue-telemetry unobserved alert is not fail-closed",
    ):
        proof.validate_rule_config(
            rules,
            ["PropertyQuarryQueueTelemetryUnobserved"],
        )


def test_static_validator_rejects_dns_discovery_and_inline_operator_url() -> None:
    prometheus = dict(proof.load_yaml(proof.DEFAULT_PROMETHEUS_CONFIG_PATH, name="Prometheus"))
    prometheus["scrape_configs"] = [dict(prometheus["scrape_configs"][0])]
    prometheus["scrape_configs"][0]["dns_sd_configs"] = [{"names": ["api"], "type": "A", "port": 8090}]
    with pytest.raises(proof.MonitoringProofError, match="dns_sd_configs"):
        proof.validate_prometheus_config(prometheus)

    alertmanager = dict(proof.load_yaml(proof.DEFAULT_ALERTMANAGER_CONFIG_PATH, name="Alertmanager"))
    alertmanager["receivers"] = [dict(item) for item in alertmanager["receivers"]]
    operator = alertmanager["receivers"][0]
    operator["webhook_configs"] = [{"url": "https://operator.invalid", "send_resolved": True}]
    with pytest.raises(proof.MonitoringProofError, match="secret URL file"):
        proof.validate_alertmanager_config(alertmanager)


def test_loaded_prometheus_config_rejects_extra_remote_write() -> None:
    source = proof.load_yaml(proof.DEFAULT_PROMETHEUS_CONFIG_PATH, name="Prometheus")
    loaded = copy.deepcopy(source)
    loaded["remote_write"] = [{"url": "https://10.0.0.50/write"}]
    response = {
        "status": "success",
        "data": {"yaml": yaml.safe_dump(loaded, sort_keys=False)},
    }
    with pytest.raises(proof.MonitoringProofError, match="complete canonical config"):
        proof.validate_loaded_prometheus_config(response, source_config=source)


@pytest.mark.parametrize(
    "url",
    ["http://0.0.0.0:9090", "http://[::]:9090"],
)
def test_private_base_url_rejects_wildcard_addresses(url: str) -> None:
    with pytest.raises(proof.MonitoringProofError, match="private IP literal"):
        proof._private_base_url(url, field="endpoint")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9090",
        "http://[::1]:9090",
        "http://10.0.0.5:9090",
        "http://[fd00::5]:9090",
        "http://localhost:9090",
    ],
)
def test_private_base_url_accepts_explicit_private_and_loopback(url: str) -> None:
    assert proof._private_base_url(url, field="endpoint")


class _SocketIdentity:
    def __init__(self, peer: str, local: str) -> None:
        self._peer = peer
        self._local = local

    def getpeername(self) -> tuple[str, int]:
        return self._peer, 9090

    def getsockname(self) -> tuple[str, int]:
        return self._local, 50000


class _SocketResponse:
    def __init__(self, peer: str, local: str) -> None:
        raw = type("Raw", (), {"_sock": _SocketIdentity(peer, local)})()
        self.fp = type("Fp", (), {"raw": raw})()


@pytest.mark.parametrize(
    ("endpoint_url", "peer", "local"),
    [
        ("http://localhost:9090", "10.0.0.8", "10.0.0.9"),
        ("http://127.0.0.1:9090", "127.0.0.1", "10.0.0.9"),
        ("http://[::1]:9090", "::1", "2001:db8::1"),
    ],
)
def test_localhost_and_loopback_claims_reject_spoofed_socket(
    endpoint_url: str,
    peer: str,
    local: str,
) -> None:
    with pytest.raises(proof.HttpRequestError, match="loopback|local socket"):
        proof.PrivateJsonHttpClient._verify_connected_socket(
            _SocketResponse(peer, local),
            base_url=endpoint_url,
        )


@pytest.mark.parametrize(
    ("endpoint_url", "peer", "local"),
    [
        ("http://localhost:9090", "127.0.0.1", "127.0.0.1"),
        ("http://127.0.0.1:9090", "127.0.0.1", "127.0.0.1"),
        ("http://[::1]:9090", "::1", "::1"),
        ("http://10.0.0.5:9090", "10.0.0.5", "10.0.0.2"),
    ],
)
def test_connected_socket_accepts_exact_peer(
    endpoint_url: str,
    peer: str,
    local: str,
) -> None:
    proof.PrivateJsonHttpClient._verify_connected_socket(
        _SocketResponse(peer, local),
        base_url=endpoint_url,
    )


def test_private_http_client_disables_proxy_and_redirects() -> None:
    client = proof.PrivateJsonHttpClient(timeout_seconds=1)
    assert isinstance(client._proxy_handler, proof.urllib.request.ProxyHandler)
    assert client._proxy_handler.proxies == {}
    redirect_handlers = [
        handler
        for handler in client._opener.handlers
        if isinstance(handler, proof._RejectRedirects)
    ]
    assert len(redirect_handlers) == 1
    assert client._redirect_handler is redirect_handlers[0]
    assert client._redirect_handler.redirect_request() is None


def test_unconfigured_source_topology_and_tool_hash_fail_closed() -> None:
    topology, _ = proof._load_json(proof.SOURCE_TOPOLOGY_PATH, name="topology")
    with pytest.raises(proof.MonitoringProofError, match="immutable digest"):
        proof.validate_topology(topology, require_configured=True)
    proof.validate_topology(topology, require_configured=False)

    tools, _ = proof._load_json(proof.SOURCE_TOOL_MANIFEST_PATH, name="tools")
    slo, _ = proof._load_json(proof.DEFAULT_SLO_PATH, name="SLO")
    with pytest.raises(proof.MonitoringProofError, match="UNCONFIGURED"):
        proof.load_tool_identities(tools, slo=slo)


def test_runtime_proof_requires_every_replica_and_delivers_only_synthetic_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proof, "assert_tool_identity", lambda identity: None)
    monkeypatch.setattr(proof, "load_tool_identities", _fixture_tool_identities)
    client = FakeHttpClient()
    runner = FakeCommandRunner()
    config = _config(tmp_path)

    result = proof.run_monitoring_proof(
        config=config,
        http_client=client,
        command_runner=runner,
        clock=lambda: NOW,
        sleeper=lambda _: None,
        signature_provider=AUTHORITY.sign,
    )

    assert result["prometheus"]["expected_replica_ids"] == REPLICA_IDS
    assert [item["replica_id"] for item in result["prometheus"]["targets"]] == REPLICA_IDS
    assert result["alertmanager"] == {
        "loaded_config_sha256": AUTHORITY.challenge.policy_hashes[
            "alertmanager_config_sha256"
        ],
        "status": "ready",
        "proof_secret_configured": True,
    }
    assert client.injected is not None
    assert client.injected[0]["labels"]["proof"] == "propertyquarry-release"
    assert client.injected[0]["labels"]["release_commit_sha"] == RELEASE_SHA
    assert not any("operator" in base_url for _, base_url, _, _ in client.calls)
    assert stat.S_IMODE(config.receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.alert_delivery_receipt_path.stat().st_mode) == 0o600
    assert all(Path(call[0]).is_absolute() for call in runner.calls)
    assert any(
        call[1:4] == ("check", "config", "--syntax-only")
        for call in runner.calls
    )


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (FakeHttpClient(target_health="down"), "not healthy"),
        (FakeHttpClient(omit_gauge_for="api-b"), "gauge is missing"),
    ],
)
def test_runtime_proof_rejects_unhealthy_or_missing_replica_evidence(
    tmp_path: Path,
    client: FakeHttpClient,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proof, "assert_tool_identity", lambda identity: None)
    monkeypatch.setattr(proof, "load_tool_identities", _fixture_tool_identities)
    with pytest.raises(proof.MonitoringProofError, match=message):
        proof.run_monitoring_proof(
            config=_config(tmp_path),
            http_client=client,
            command_runner=FakeCommandRunner(),
            clock=lambda: NOW,
            sleeper=lambda _: None,
            signature_provider=AUTHORITY.sign,
        )
    assert client.injected is None


def test_existing_output_refuses_before_tools_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proof, "assert_tool_identity", lambda identity: None)
    monkeypatch.setattr(proof, "load_tool_identities", _fixture_tool_identities)
    config = _config(tmp_path)
    config.receipt_path.write_text("existing", encoding="utf-8")
    client = FakeHttpClient()
    runner = FakeCommandRunner()

    with pytest.raises(proof.MonitoringProofError, match="already exists"):
        proof.run_monitoring_proof(
            config=config,
            http_client=client,
            command_runner=runner,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            signature_provider=AUTHORITY.sign,
        )

    assert client.calls == []
    assert runner.calls == []


@pytest.mark.parametrize("field", ["slo_path", "flagship_operations_path"])
def test_runtime_proof_rejects_caller_selected_policy_path(
    tmp_path: Path,
    field: str,
) -> None:
    config = replace(
        _config(tmp_path),
        **{field: tmp_path / f"lax-{field}.json"},
    )
    with pytest.raises(proof.MonitoringProofError, match="policy path override"):
        proof._normalize_release(config)


def test_runtime_proof_rejects_policy_path_alias_collision_before_tools_or_network(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        slo_path=proof.DEFAULT_FLAGSHIP_OPERATIONS_PATH,
        flagship_operations_path=proof.DEFAULT_FLAGSHIP_OPERATIONS_PATH,
    )
    client = FakeHttpClient()
    runner = FakeCommandRunner()

    with pytest.raises(
        proof.MonitoringProofError,
        match="policy path override",
    ):
        proof.run_monitoring_proof(
            config=config,
            http_client=client,
            command_runner=runner,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            signature_provider=AUTHORITY.sign,
        )

    assert client.calls == []
    assert runner.calls == []


def test_runtime_proof_rejects_flagship_operations_hash_drift_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    original_sha256_file = proof.sha256_file
    hashed_paths: list[Path] = []

    def drifted_sha256_file(path: Path) -> str:
        hashed_paths.append(path)
        if path == config.flagship_operations_path:
            return "0" * 64
        return original_sha256_file(path)

    monkeypatch.setattr(proof, "sha256_file", drifted_sha256_file)
    client = FakeHttpClient()
    runner = FakeCommandRunner()

    with pytest.raises(
        proof.MonitoringProofError,
        match="policy hashes differ from the signed challenge",
    ):
        proof.run_monitoring_proof(
            config=config,
            http_client=client,
            command_runner=runner,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            signature_provider=AUTHORITY.sign,
        )

    assert config.flagship_operations_path in hashed_paths
    assert client.calls == []
    assert runner.calls == []


def test_cli_binds_canonical_flagship_operations_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[proof.ProofConfig] = []

    def fake_run_monitoring_proof(**kwargs: object) -> dict[str, object]:
        config = kwargs["config"]
        assert isinstance(config, proof.ProofConfig)
        captured.append(config)
        return {}

    monkeypatch.setattr(proof, "run_monitoring_proof", fake_run_monitoring_proof)
    monkeypatch.setattr(proof, "PrivateJsonHttpClient", lambda **_kwargs: object())
    monkeypatch.setattr(proof, "SubprocessCommandRunner", lambda: object())

    assert (
        proof.main(
            [
                "--execute",
                "--release-sha",
                RELEASE_SHA,
                "--image-digest",
                IMAGE_DIGEST,
                "--receipt",
                str(tmp_path / "monitoring.json"),
                "--alert-delivery-receipt",
                str(tmp_path / "alert.json"),
                "--metrics-snapshot",
                str(tmp_path / "metrics.json"),
                "--flagship-operations-policy",
                str(proof.DEFAULT_FLAGSHIP_OPERATIONS_PATH),
            ]
        )
        == 0
    )
    assert len(captured) == 1
    assert (
        captured[0].flagship_operations_path
        == proof.DEFAULT_FLAGSHIP_OPERATIONS_PATH
    )


@pytest.mark.parametrize(
    "alias",
    [
        "https://10.0.0.30:9443",
        "https://10.0.0.30:9443/",
        "https://[::ffff:10.0.0.30]:9443",
    ],
)
def test_topology_rejects_same_operator_and_proof_endpoint_aliases(
    tmp_path: Path,
    alias: str,
) -> None:
    topology_path, _manifest = _configured_inputs(tmp_path)
    payload = json.loads(topology_path.read_text(encoding="utf-8"))
    payload["endpoints"]["proof_receiver_base_url"] = alias
    payload["identities"]["proof_receiver"]["endpoint_origin"] = alias
    with pytest.raises(proof.MonitoringProofError, match="must be distinct"):
        proof.validate_topology(
            payload,
            operator_gateway_trust=GATEWAY.trust,
        )


def test_topology_rejects_loopback_alias_key_audience_and_secret_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global GATEWAY
    GATEWAY = install_test_operator_gateway(
        monkeypatch,
        evidence_authority=AUTHORITY,
        endpoint_origin="https://localhost:9443",
    )
    topology_path, _manifest = _configured_inputs(tmp_path)
    payload = json.loads(topology_path.read_text(encoding="utf-8"))
    payload["endpoints"]["proof_receiver_base_url"] = "http://127.0.0.1:9443"
    payload["identities"]["proof_receiver"]["endpoint_origin"] = "http://127.0.0.1:9443"
    with pytest.raises(proof.MonitoringProofError, match="must be distinct"):
        proof.validate_topology(payload, operator_gateway_trust=GATEWAY.trust)


def test_webhook_secret_content_must_target_pinned_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_path, _manifest = _configured_inputs(tmp_path)
    topology = proof.validate_topology(
        json.loads(topology_path.read_text(encoding="utf-8")),
        operator_gateway_trust=GATEWAY.trust,
    )
    assert topology is not None
    proof.validate_webhook_secret_bindings(topology)

    def local_receiver_secret(
        path_text: str,
        *,
        field: str,
        _test_allow_insecure: bool = False,
    ) -> str:
        del field, _test_allow_insecure
        if path_text.endswith("propertyquarry_alert_webhook_url"):
            return "https://127.0.0.1:9199/v1/alerts"
        return "http://127.0.0.1:9199/v1/alerts"

    monkeypatch.setattr(proof, "_load_secret", local_receiver_secret)
    with pytest.raises(proof.MonitoringProofError, match="pinned endpoint"):
        proof.validate_webhook_secret_bindings(topology)

    payload = json.loads(topology_path.read_text(encoding="utf-8"))
    payload["identities"]["proof_receiver"]["key_id"] = GATEWAY.trust.key_id
    payload["identities"]["proof_receiver"]["audience"] = GATEWAY.trust.audience
    payload["secrets"]["proof_webhook_url_file"] = payload["secrets"][
        "operator_webhook_url_file"
    ]
    with pytest.raises(proof.MonitoringProofError, match="must be distinct"):
        proof.validate_topology(payload, operator_gateway_trust=GATEWAY.trust)


def test_secret_loader_rejects_attacker_owned_and_symlink_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker = tmp_path / "attacker-token"
    attacker.write_text("secret", encoding="utf-8")
    attacker.chmod(0o600)
    monkeypatch.setattr(contract, "assert_secure_external_parent", lambda *args, **kwargs: None)
    if os.geteuid() != 0:
        with pytest.raises(proof.MonitoringProofError, match="ownership"):
            REAL_LOAD_SECRET(str(attacker), field="attacker token")

    real = tmp_path / "real-parent"
    real.mkdir()
    token = real / "token"
    token.write_text("secret", encoding="utf-8")
    token.chmod(0o600)
    linked = tmp_path / "linked-parent"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.undo()
    with pytest.raises(proof.MonitoringProofError, match="unsafe"):
        REAL_LOAD_SECRET(str(linked / "token"), field="symlinked token")


def test_secret_loader_detects_path_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "token"
    token.write_text("original", encoding="utf-8")
    token.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_text("forged!!", encoding="utf-8")
    replacement.chmod(0o600)
    original_read = proof.os.read

    def replacing_read(fd: int, size: int) -> bytes:
        raw = original_read(fd, size)
        token.unlink()
        replacement.rename(token)
        return raw

    monkeypatch.setattr(proof.os, "read", replacing_read)
    with pytest.raises(proof.MonitoringProofError, match="changed while it was read"):
        REAL_LOAD_SECRET(
            str(token),
            field="raced token",
            _test_allow_insecure=True,
        )


@pytest.mark.parametrize("reuse", ["hardlink", "identical_value"])
def test_operator_and_proof_tokens_require_distinct_inode_and_value(
    tmp_path: Path,
    reuse: str,
) -> None:
    topology_path, _manifest = _configured_inputs(tmp_path)
    topology = proof.validate_topology(
        json.loads(topology_path.read_text(encoding="utf-8")),
        operator_gateway_trust=GATEWAY.trust,
    )
    assert topology is not None
    operator = tmp_path / "operator-token"
    receiver_token = tmp_path / "proof-token"
    operator.write_text("same-secret", encoding="utf-8")
    operator.chmod(0o600)
    if reuse == "hardlink":
        receiver_token.hardlink_to(operator)
    else:
        receiver_token.write_text("same-secret", encoding="utf-8")
        receiver_token.chmod(0o600)
    configured = replace(
        topology,
        operator_gateway_api_token_file=str(operator),
        proof_receiver_token_file=str(receiver_token),
    )
    with pytest.raises(proof.MonitoringProofError, match="distinct files and values"):
        REAL_VALIDATE_DISTINCT_TOKENS(
            configured,
            _test_allow_insecure=True,
        )


def test_operator_and_proof_tokens_accept_distinct_secure_files(tmp_path: Path) -> None:
    topology_path, _manifest = _configured_inputs(tmp_path)
    topology = proof.validate_topology(
        json.loads(topology_path.read_text(encoding="utf-8")),
        operator_gateway_trust=GATEWAY.trust,
    )
    assert topology is not None
    operator = tmp_path / "operator-token"
    receiver_token = tmp_path / "proof-token"
    operator.write_text("operator-secret", encoding="utf-8")
    receiver_token.write_text("proof-secret", encoding="utf-8")
    operator.chmod(0o600)
    receiver_token.chmod(0o600)
    REAL_VALIDATE_DISTINCT_TOKENS(
        replace(
            topology,
            operator_gateway_api_token_file=str(operator),
            proof_receiver_token_file=str(receiver_token),
        ),
        _test_allow_insecure=True,
    )


def test_token_separation_detects_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_path, _manifest = _configured_inputs(tmp_path)
    topology = proof.validate_topology(
        json.loads(topology_path.read_text(encoding="utf-8")),
        operator_gateway_trust=GATEWAY.trust,
    )
    assert topology is not None
    operator = tmp_path / "operator-token"
    receiver_token = tmp_path / "proof-token"
    replacement = tmp_path / "replacement-token"
    operator.write_text("operator-secret", encoding="utf-8")
    receiver_token.write_text("proof-secret", encoding="utf-8")
    replacement.write_text("attacker-value", encoding="utf-8")
    for path in (operator, receiver_token, replacement):
        path.chmod(0o600)
    original_read = proof.os.read
    replaced = False

    def replacing_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        raw = original_read(fd, size)
        if not replaced:
            replaced = True
            operator.unlink()
            replacement.rename(operator)
        return raw

    monkeypatch.setattr(proof.os, "read", replacing_read)
    with pytest.raises(proof.MonitoringProofError, match="changed while it was read"):
        REAL_VALIDATE_DISTINCT_TOKENS(
            replace(
                topology,
                operator_gateway_api_token_file=str(operator),
                proof_receiver_token_file=str(receiver_token),
            ),
            _test_allow_insecure=True,
        )
