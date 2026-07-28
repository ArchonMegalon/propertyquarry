#!/usr/bin/env python3
"""Shared authenticated evidence contract for PropertyQuarry launch gates.

The public trust anchor and active challenge are deliberately resolved from
fixed absolute paths.  They cannot be selected through environment variables
or command-line arguments.  The corresponding Ed25519 private key belongs to
the external release-control authority and must never exist in the deploy
workspace.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TRUST_SCHEMA = "propertyquarry.evidence-trust.v1"
CHALLENGE_SCHEMA = "propertyquarry.evidence-challenge.v1"
CHALLENGE_PRODUCER = "propertyquarry-release-control"
AUTH_SCHEME = "Ed25519"
OPERATOR_GATEWAY_TRUST_SCHEMA = "propertyquarry.operator-gateway-trust.v1"
OPERATOR_GATEWAY_ACK_SCHEMA = "propertyquarry.operator-gateway-ack.v1"
OPERATOR_GATEWAY_ACK_PRODUCER = "propertyquarry-operator-gateway"
RANGE_RECEIPT_SCHEMA = "propertyquarry.prometheus-range-receipt.v2"
RANGE_RECEIPT_PRODUCER = "propertyquarry-prometheus-range-capture"
RANGE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "producer",
        "deployment_id",
        "challenge_nonce",
        "captured_at",
        "release",
        "snapshot_bundle_sha256",
        "query",
        "transport",
        "prometheus_config_sha256",
        "expected_replica_ids",
        "replicas",
        "series",
        "range_response_sha256",
        "range_response_bytes",
        "payload_sha256",
        "authentication",
    }
)
DEFAULT_TRUST_ANCHOR_PATH = Path("/etc/propertyquarry/evidence-trust.v1.json")
DEFAULT_OPERATOR_GATEWAY_TRUST_PATH = Path(
    "/etc/propertyquarry/operator-gateway-trust.v1.json"
)
DEFAULT_CHALLENGE_PATH = Path("/run/propertyquarry/evidence-challenge.v1.json")
DEFAULT_AUTHORITY_SOCKET_PATH = Path("/run/propertyquarry/evidence-authority.sock")
_CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config" / "monitoring"
DEFAULT_MONITORING_TOPOLOGY_PATH = Path(
    "/etc/propertyquarry/monitoring-topology.v1.json"
)
DEFAULT_MONITORING_TOOL_MANIFEST_PATH = Path(
    "/etc/propertyquarry/monitoring-tools.v1.json"
)
CANONICAL_POLICY_PATHS = {
    "slo_definition_sha256": _CONFIG_ROOT / "propertyquarry_slo.v1.json",
    "alert_rules_sha256": _CONFIG_ROOT / "propertyquarry_alert_rules.v1.yml",
    "alert_rule_tests_sha256": _CONFIG_ROOT / "propertyquarry_alert_rule_tests.v1.yml",
    "prometheus_config_sha256": _CONFIG_ROOT / "propertyquarry_prometheus.v1.yml",
    "alertmanager_config_sha256": _CONFIG_ROOT / "propertyquarry_alertmanager.v1.yml",
    "flagship_operations_sha256": _CONFIG_ROOT
    / "propertyquarry_flagship_operations.v1.json",
}
MAX_SECURE_ARTIFACT_BYTES = 64 * 1024
MAX_CHALLENGE_LIFETIME_SECONDS = 900
MAX_EVIDENCE_AGE_SECONDS = 900
MAX_FUTURE_SKEW_SECONDS = 30
RANGE_STEP_SECONDS = 300
RANGE_WINDOW_SECONDS = 30 * 24 * 60 * 60
PROMETHEUS_RANGE_QUERY = (
    '{__name__=~"propertyquarry_http_requests_total|'
    'propertyquarry_http_request_errors_total|'
    'propertyquarry_http_request_duration_seconds_bucket|'
    'propertyquarry_http_request_duration_seconds_sum|'
    'propertyquarry_http_request_duration_seconds_count|'
    'propertyquarry_ingress_admission_operations_total|'
    'propertyquarry_runtime_build_info"}'
)

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")

CHALLENGE_DOMAIN = "propertyquarry/evidence-challenge/v1"
MONITORING_DOMAIN = "propertyquarry/monitoring-runtime-proof/v1"
RANGE_DOMAIN = "propertyquarry/prometheus-range-receipt/v1"
ALERT_DOMAIN = "propertyquarry/operator-alert-ack/v1"
OPERATOR_GATEWAY_ACK_DOMAIN = "propertyquarry/operator-gateway-ack/v1"
DASHBOARD_RENDER_DOMAIN = "propertyquarry/dashboard-render-receipt/v1"
STRUCTURED_LOG_QUERY_DOMAIN = "propertyquarry/structured-log-query-receipt/v1"
DISTRIBUTED_TRACE_QUERY_DOMAIN = "propertyquarry/distributed-trace-query-receipt/v1"
SIGNATURE_PREFIX = b"PROPERTYQUARRY-AUTHENTICATED-EVIDENCE-V1\x00"


class EvidenceContractError(RuntimeError):
    """A trust anchor, challenge, signature, or evidence binding is invalid."""


class SignatureProvider(Protocol):
    def __call__(self, domain: str, unsigned_payload: Mapping[str, object]) -> str: ...


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError("evidence contains a non-canonical JSON value") from exc
    return rendered.encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(raw: str) -> object:
    raise EvidenceContractError(f"non-finite JSON constant is forbidden: {raw}")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceContractError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise EvidenceContractError(
            f"{field} keys do not match the v1 contract; "
            f"missing={sorted(expected - set(value))}, unexpected={sorted(set(value) - expected)}"
        )


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceContractError(f"{field} must be a non-empty trimmed string")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    text = _text(value, field=field)
    if not text.endswith("Z"):
        raise EvidenceContractError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceContractError(f"{field} must be UTC")
    try:
        finite = math.isfinite(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        finite = False
    if not finite:
        raise EvidenceContractError(f"{field} is outside the supported range")
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_base64url(value: object, *, expected_bytes: int, field: str) -> bytes:
    text = _text(value, field=field)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise EvidenceContractError(f"{field} must be unpadded base64url")
    padding = "=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.b64decode(text + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EvidenceContractError(f"{field} is not valid base64url") from exc
    if len(decoded) != expected_bytes:
        raise EvidenceContractError(f"{field} must decode to exactly {expected_bytes} bytes")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != text:
        raise EvidenceContractError(f"{field} is not canonical base64url")
    return decoded


def assert_secure_external_parent(path: Path, *, field: str) -> None:
    """Require every directory in a fixed authority path to be root-controlled."""

    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise EvidenceContractError(f"{field} parent directory is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise EvidenceContractError(
                f"{field} parent directories must be root-owned and non-writable"
            )
        if current == current.parent:
            break
        current = current.parent


def _secure_external_json(
    path: Path, *, field: str
) -> tuple[dict[str, object], bytes, os.stat_result]:
    """Read one fixed external authority artifact without path or inode races."""

    if not path.is_absolute():
        raise EvidenceContractError(f"{field} path must be absolute")
    assert_secure_external_parent(path, field=field)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvidenceContractError(f"{field} is unavailable at its fixed path") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceContractError(f"{field} must be a regular non-symlink file")
        if before.st_uid != 0 or stat.S_IMODE(before.st_mode) & 0o022:
            raise EvidenceContractError(f"{field} must be root-owned and not group/world writable")
        if not 0 < before.st_size <= MAX_SECURE_ARTIFACT_BYTES:
            raise EvidenceContractError(f"{field} size is invalid")
        chunks: list[bytes] = []
        remaining = MAX_SECURE_ARTIFACT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
            raise EvidenceContractError(f"{field} changed while it was being read")
        if len(raw) != before.st_size:
            raise EvidenceContractError(f"{field} read was incomplete")
    finally:
        os.close(fd)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceContractError(f"{field} is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise EvidenceContractError(f"{field} must be a JSON object")
    return parsed, raw, before


@dataclass(frozen=True)
class TrustAnchor:
    key_id: str
    public_key: Ed25519PublicKey
    file_sha256: str
    device: int
    inode: int


@dataclass(frozen=True)
class EvidenceChallenge:
    key_id: str
    deployment_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    release_commit_sha: str
    release_image_digest: str
    artifact_sha256: str
    policy_hashes: Mapping[str, str]


def canonical_release_proof_labels(
    *,
    release_commit_sha: str,
    release_image_digest: str,
    deployment_id: str,
    nonce: str,
    challenge_sha256: str,
) -> dict[str, str]:
    return {
        "alertname": "PropertyQuarryReleaseProof",
        "service": "propertyquarry",
        "severity": "info",
        "proof": "propertyquarry-release",
        "proof_nonce": nonce,
        "release_commit_sha": release_commit_sha,
        "release_image_digest": release_image_digest,
        "deployment_id": deployment_id,
        "evidence_challenge_sha256": challenge_sha256,
    }


def canonical_policy_hashes() -> dict[str, str]:
    try:
        return {
            name: sha256_bytes(
                _stable_file_bytes(
                    path,
                    field=f"canonical policy {name}",
                    maximum_bytes=4 * 1024 * 1024,
                )[0]
            )
            for name, path in CANONICAL_POLICY_PATHS.items()
        }
    except (OSError, EvidenceContractError) as exc:
        raise EvidenceContractError("canonical launch policy files are unavailable") from exc


def _stable_file_bytes(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
    require_root_executable: bool = False,
    require_root_controlled: bool = False,
    _test_allow_insecure: bool = False,
) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute():
        raise EvidenceContractError(f"{field} path must be absolute")
    try:
        path_before = os.stat(path, follow_symlinks=False)
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise EvidenceContractError(f"{field} is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or path_before.st_dev != before.st_dev
            or path_before.st_ino != before.st_ino
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise EvidenceContractError(f"{field} identity or size is invalid")
        if (require_root_executable or require_root_controlled) and not _test_allow_insecure and (
            before.st_uid != 0
            or stat.S_IMODE(before.st_mode) & 0o022
            or (require_root_executable and not before.st_mode & stat.S_IXUSR)
        ):
            raise EvidenceContractError(f"{field} ownership or mode is unsafe")
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
        raise EvidenceContractError(f"{field} changed while it was read") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        len(raw) != before.st_size
        or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
        or any(getattr(before, name) != getattr(path_after, name) for name in stable_fields)
    ):
        raise EvidenceContractError(f"{field} changed while it was read")
    return raw, before


def compute_canonical_monitoring_identity(
    *,
    topology_path: Path,
    tool_manifest_path: Path,
    _test_allow_insecure_tools: bool = False,
) -> dict[str, object]:
    topology_raw, _topology_stat = _stable_file_bytes(
        topology_path,
        field="canonical monitoring topology",
        maximum_bytes=MAX_SECURE_ARTIFACT_BYTES,
        require_root_controlled=True,
        _test_allow_insecure=_test_allow_insecure_tools,
    )
    manifest_raw, _manifest_stat = _stable_file_bytes(
        tool_manifest_path,
        field="canonical monitoring tool manifest",
        maximum_bytes=MAX_SECURE_ARTIFACT_BYTES,
        require_root_controlled=True,
        _test_allow_insecure=_test_allow_insecure_tools,
    )
    try:
        manifest = json.loads(
            manifest_raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceContractError("canonical monitoring tool manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise EvidenceContractError("canonical monitoring tool manifest must be an object")
    _exact_keys(manifest, {"schema_version", "tools"}, field="monitoring tool manifest")
    if manifest["schema_version"] != "propertyquarry.monitoring-tools.v1":
        raise EvidenceContractError("canonical monitoring tool manifest schema is invalid")
    tools = manifest["tools"]
    if not isinstance(tools, dict):
        raise EvidenceContractError("canonical monitoring tools must be an object")
    _exact_keys(tools, {"promtool", "amtool"}, field="canonical monitoring tools")
    tool_receipts: dict[str, dict[str, object]] = {}
    identity: dict[str, str] = {
        "topology_contract_sha256": sha256_bytes(topology_raw),
        "tool_manifest_sha256": sha256_bytes(manifest_raw),
        **canonical_policy_hashes(),
    }
    for name in ("promtool", "amtool"):
        spec = tools[name]
        if not isinstance(spec, dict):
            raise EvidenceContractError(f"canonical {name} manifest entry is invalid")
        _exact_keys(spec, {"path", "version", "sha256"}, field=f"canonical {name}")
        path_text = _text(spec["path"], field=f"canonical {name} path")
        version = _text(spec["version"], field=f"canonical {name} version")
        expected_hash = _text(spec["sha256"], field=f"canonical {name} hash")
        path = Path(path_text)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or not SHA256_RE.fullmatch(expected_hash)
        ):
            raise EvidenceContractError(f"canonical {name} identity is invalid")
        binary_raw, metadata = _stable_file_bytes(
            path,
            field=f"canonical {name} binary",
            maximum_bytes=512 * 1024 * 1024,
            require_root_executable=True,
            _test_allow_insecure=_test_allow_insecure_tools,
        )
        actual_hash = sha256_bytes(binary_raw)
        if actual_hash != expected_hash:
            raise EvidenceContractError(f"canonical {name} binary hash differs from manifest")
        descriptor: dict[str, object] = {
            "path": path_text,
            "version": version,
            "sha256": actual_hash,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        tool_receipts[name] = descriptor
        identity[f"{name}_binary_sha256"] = actual_hash
        identity[f"{name}_binary_identity_sha256"] = sha256_bytes(
            canonical_json_bytes(descriptor)
        )
    return {"identity": identity, "monitoring_tools": tool_receipts}


def load_canonical_monitoring_identity() -> dict[str, object]:
    assert_secure_external_parent(
        DEFAULT_MONITORING_TOPOLOGY_PATH,
        field="canonical monitoring topology",
    )
    assert_secure_external_parent(
        DEFAULT_MONITORING_TOOL_MANIFEST_PATH,
        field="canonical monitoring tool manifest",
    )
    return compute_canonical_monitoring_identity(
        topology_path=DEFAULT_MONITORING_TOPOLOGY_PATH,
        tool_manifest_path=DEFAULT_MONITORING_TOOL_MANIFEST_PATH,
    )


@dataclass(frozen=True)
class OperatorGatewayTrust:
    key_id: str
    audience: str
    endpoint_origin: str
    endpoint_socket_identity: tuple[str, int]
    tls_spki_sha256: str
    public_key: Ed25519PublicKey
    file_sha256: str


def canonical_endpoint_origin(
    value: object,
    *,
    field: str,
    require_https: bool,
) -> tuple[str, tuple[str, int]]:
    """Canonicalize a private endpoint and collapse loopback aliases."""

    raw = _text(value, field=field)
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise EvidenceContractError(f"{field} is not a valid endpoint origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or (require_https and parsed.scheme != "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise EvidenceContractError(f"{field} must be a credential-free private origin")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost":
        display_host = "localhost"
        socket_host = "loopback"
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise EvidenceContractError(
                f"{field} must use localhost or a private IP literal"
            ) from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_unspecified
            or address.is_multicast
            or not (address.is_loopback or address.is_private)
        ):
            raise EvidenceContractError(f"{field} must use a private non-wildcard endpoint")
        socket_host = "loopback" if address.is_loopback else address.compressed
        display_host = address.compressed
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    rendered_host = f"[{display_host}]" if ":" in display_host else display_host
    rendered_port = "" if effective_port == (443 if parsed.scheme == "https" else 80) else f":{effective_port}"
    origin = f"{parsed.scheme}://{rendered_host}{rendered_port}"
    return origin, (socket_host, effective_port)


def load_operator_gateway_trust(
    *,
    evidence_anchor: TrustAnchor | None = None,
) -> OperatorGatewayTrust:
    payload, raw, _metadata = _secure_external_json(
        DEFAULT_OPERATOR_GATEWAY_TRUST_PATH,
        field="PropertyQuarry operator gateway trust anchor",
    )
    _exact_keys(
        payload,
        {
            "schema_version",
            "algorithm",
            "key_id",
            "audience",
            "endpoint_origin",
            "tls_spki_sha256",
            "ed25519_public_key",
        },
        field="operator gateway trust anchor",
    )
    if (
        payload["schema_version"] != OPERATOR_GATEWAY_TRUST_SCHEMA
        or payload["algorithm"] != AUTH_SCHEME
    ):
        raise EvidenceContractError("operator gateway trust schema or algorithm is invalid")
    key_id = _text(payload["key_id"], field="operator gateway key_id")
    audience = _text(payload["audience"], field="operator gateway audience")
    if (
        key_id == "UNCONFIGURED"
        or audience == "UNCONFIGURED"
        or not KEY_ID_RE.fullmatch(key_id)
        or not DEPLOYMENT_ID_RE.fullmatch(audience)
    ):
        raise EvidenceContractError("operator gateway identity remains UNCONFIGURED or invalid")
    endpoint_origin, socket_identity = canonical_endpoint_origin(
        payload["endpoint_origin"],
        field="operator gateway endpoint",
        require_https=True,
    )
    tls_spki_sha256 = _text(
        payload["tls_spki_sha256"], field="operator gateway TLS SPKI hash"
    )
    if not SHA256_RE.fullmatch(tls_spki_sha256):
        raise EvidenceContractError("operator gateway TLS SPKI hash is invalid")
    public_raw = _decode_base64url(
        payload["ed25519_public_key"],
        expected_bytes=32,
        field="operator gateway public key",
    )
    public_key = Ed25519PublicKey.from_public_bytes(public_raw)
    if evidence_anchor is not None:
        evidence_raw = evidence_anchor.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if evidence_anchor.key_id == key_id or evidence_raw == public_raw:
            raise EvidenceContractError(
                "operator gateway and evidence authority must use distinct keys"
            )
    return OperatorGatewayTrust(
        key_id=key_id,
        audience=audience,
        endpoint_origin=endpoint_origin,
        endpoint_socket_identity=socket_identity,
        tls_spki_sha256=tls_spki_sha256,
        public_key=public_key,
        file_sha256=sha256_bytes(raw),
    )


def verify_operator_gateway_signature(
    payload: Mapping[str, object],
    *,
    trust: OperatorGatewayTrust,
    field: str,
) -> None:
    authentication = payload.get("authentication")
    if not isinstance(authentication, dict):
        raise EvidenceContractError(f"{field}.authentication must be an object")
    _exact_keys(
        authentication,
        {"scheme", "key_id", "signature"},
        field=f"{field}.authentication",
    )
    if (
        authentication["scheme"] != AUTH_SCHEME
        or authentication["key_id"] != trust.key_id
    ):
        raise EvidenceContractError(f"{field} was not signed by the pinned operator gateway")
    signature = _decode_base64url(
        authentication["signature"],
        expected_bytes=64,
        field=f"{field} signature",
    )
    unsigned = copy.deepcopy(dict(payload))
    unsigned_authentication = dict(authentication)
    del unsigned_authentication["signature"]
    unsigned["authentication"] = unsigned_authentication
    try:
        trust.public_key.verify(
            signature,
            _signature_message(OPERATOR_GATEWAY_ACK_DOMAIN, unsigned),
        )
    except InvalidSignature as exc:
        raise EvidenceContractError(f"{field} signature is invalid") from exc


def alert_fingerprint_sha256(
    *,
    labels: Mapping[str, str],
    sent_at: datetime,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "labels": dict(labels),
                "sent_at": isoformat(sent_at),
            }
        )
    )


def _signature_message(domain: str, payload: Mapping[str, object]) -> bytes:
    return SIGNATURE_PREFIX + domain.encode("ascii") + b"\x00" + canonical_json_bytes(payload)


def authenticated_signature_message(domain: str, unsigned_payload: Mapping[str, object]) -> bytes:
    """Return the exact domain-separated bytes an external authority signs."""

    return _signature_message(domain, unsigned_payload)


def load_trust_anchor() -> TrustAnchor:
    path = DEFAULT_TRUST_ANCHOR_PATH
    payload, raw, metadata = _secure_external_json(
        path, field="PropertyQuarry evidence trust anchor"
    )
    _exact_keys(
        payload,
        {"schema_version", "algorithm", "key_id", "ed25519_public_key"},
        field="evidence trust anchor",
    )
    if payload["schema_version"] != TRUST_SCHEMA or payload["algorithm"] != AUTH_SCHEME:
        raise EvidenceContractError("evidence trust anchor schema or algorithm is invalid")
    key_id = _text(payload["key_id"], field="evidence trust anchor key_id")
    if key_id == "UNCONFIGURED" or not KEY_ID_RE.fullmatch(key_id):
        raise EvidenceContractError("evidence trust anchor remains UNCONFIGURED or invalid")
    key_raw = _decode_base64url(
        payload["ed25519_public_key"],
        expected_bytes=32,
        field="evidence trust anchor public key",
    )
    try:
        public_key = Ed25519PublicKey.from_public_bytes(key_raw)
    except ValueError as exc:
        raise EvidenceContractError("evidence trust anchor public key is invalid") from exc
    return TrustAnchor(key_id, public_key, sha256_bytes(raw), metadata.st_dev, metadata.st_ino)


def load_evidence_challenge(
    *,
    expected_commit_sha: str,
    expected_image_digest: str,
    now: datetime,
) -> tuple[TrustAnchor, EvidenceChallenge]:
    anchor = load_trust_anchor()
    payload, raw, _metadata = _secure_external_json(
        DEFAULT_CHALLENGE_PATH,
        field="PropertyQuarry evidence challenge",
    )
    _exact_keys(
        payload,
        {
            "schema_version",
            "producer",
            "key_id",
            "deployment_id",
            "nonce",
            "issued_at",
            "expires_at",
            "release",
            "policy",
            "signature",
        },
        field="evidence challenge",
    )
    if payload["schema_version"] != CHALLENGE_SCHEMA or payload["producer"] != CHALLENGE_PRODUCER:
        raise EvidenceContractError("evidence challenge schema or producer is invalid")
    key_id = _text(payload["key_id"], field="evidence challenge key_id")
    if key_id != anchor.key_id:
        raise EvidenceContractError("evidence challenge signer is not the fixed trusted authority")
    deployment_id = _text(payload["deployment_id"], field="evidence challenge deployment_id")
    nonce = _text(payload["nonce"], field="evidence challenge nonce")
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id) or not NONCE_RE.fullmatch(nonce):
        raise EvidenceContractError("evidence challenge deployment ID or nonce is invalid")
    release = payload["release"]
    if not isinstance(release, dict):
        raise EvidenceContractError("evidence challenge release must be an object")
    _exact_keys(release, {"commit_sha", "image_digest"}, field="evidence challenge release")
    commit_sha = _text(release["commit_sha"], field="evidence challenge release commit")
    image_digest = _text(release["image_digest"], field="evidence challenge release image")
    if (
        not GIT_SHA_RE.fullmatch(commit_sha)
        or not IMAGE_DIGEST_RE.fullmatch(image_digest)
        or commit_sha != expected_commit_sha
        or image_digest != expected_image_digest
    ):
        raise EvidenceContractError("evidence challenge belongs to another release")
    policy = payload["policy"]
    if not isinstance(policy, dict):
        raise EvidenceContractError("evidence challenge policy must be an object")
    _exact_keys(policy, set(CANONICAL_POLICY_PATHS), field="evidence challenge policy")
    policy_hashes: dict[str, str] = {}
    for name in CANONICAL_POLICY_PATHS:
        value = _text(policy[name], field=f"evidence challenge policy {name}")
        if not SHA256_RE.fullmatch(value):
            raise EvidenceContractError("evidence challenge policy hash is invalid")
        policy_hashes[name] = value
    issued_at = _timestamp(payload["issued_at"], field="evidence challenge issued_at")
    expires_at = _timestamp(payload["expires_at"], field="evidence challenge expires_at")
    checked_at = now.astimezone(timezone.utc)
    if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > MAX_CHALLENGE_LIFETIME_SECONDS:
        raise EvidenceContractError("evidence challenge lifetime is invalid")
    if issued_at > checked_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise EvidenceContractError("evidence challenge is future-dated")
    if checked_at >= expires_at or (checked_at - issued_at).total_seconds() > MAX_CHALLENGE_LIFETIME_SECONDS:
        raise EvidenceContractError("evidence challenge is stale or expired")
    signature = _decode_base64url(
        payload["signature"], expected_bytes=64, field="evidence challenge signature"
    )
    unsigned = dict(payload)
    del unsigned["signature"]
    try:
        anchor.public_key.verify(signature, _signature_message(CHALLENGE_DOMAIN, unsigned))
    except InvalidSignature as exc:
        raise EvidenceContractError("evidence challenge signature is invalid") from exc
    return anchor, EvidenceChallenge(
        key_id=key_id,
        deployment_id=deployment_id,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
        release_commit_sha=commit_sha,
        release_image_digest=image_digest,
        artifact_sha256=sha256_bytes(raw),
        policy_hashes=policy_hashes,
    )


def verify_authenticated_payload(
    payload: Mapping[str, object],
    *,
    domain: str,
    anchor: TrustAnchor,
    challenge: EvidenceChallenge,
    field: str,
) -> None:
    authentication = payload.get("authentication")
    if not isinstance(authentication, dict):
        raise EvidenceContractError(f"{field}.authentication must be an object")
    _exact_keys(
        authentication,
        {"scheme", "key_id", "challenge_sha256", "signature"},
        field=f"{field}.authentication",
    )
    if authentication["scheme"] != AUTH_SCHEME or authentication["key_id"] != anchor.key_id:
        raise EvidenceContractError(f"{field} signer is not the fixed trusted authority")
    if authentication["challenge_sha256"] != challenge.artifact_sha256:
        raise EvidenceContractError(f"{field} is not bound to the active evidence challenge")
    if payload.get("deployment_id") != challenge.deployment_id or payload.get("challenge_nonce") != challenge.nonce:
        raise EvidenceContractError(f"{field} deployment or challenge nonce binding differs")
    signature = _decode_base64url(
        authentication["signature"], expected_bytes=64, field=f"{field} signature"
    )
    unsigned = copy.deepcopy(dict(payload))
    unsigned_authentication = dict(authentication)
    del unsigned_authentication["signature"]
    unsigned["authentication"] = unsigned_authentication
    try:
        anchor.public_key.verify(signature, _signature_message(domain, unsigned))
    except InvalidSignature as exc:
        raise EvidenceContractError(f"{field} signature is invalid") from exc


def request_release_control_signature(
    domain: str,
    unsigned_payload: Mapping[str, object],
    *,
    timeout_seconds: int = 10,
) -> str:
    """Request a signature from the fixed external release-control socket."""

    path = DEFAULT_AUTHORITY_SOCKET_PATH
    if not path.is_absolute():
        raise EvidenceContractError("evidence authority socket path must be absolute")
    assert_secure_external_parent(path, field="evidence authority socket")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceContractError("evidence authority socket is unavailable") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise EvidenceContractError(
            "evidence authority socket must be root-owned and root-only"
        )
    request = canonical_json_bytes(
        {
            "schema_version": "propertyquarry.evidence-sign-request.v1",
            "domain": domain,
            "payload": dict(unsigned_payload),
        }
    ) + b"\n"
    if len(request) > MAX_SECURE_ARTIFACT_BYTES:
        raise EvidenceContractError("evidence signing request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(path))
            connected_metadata = path.lstat()
            if any(
                getattr(metadata, name) != getattr(connected_metadata, name)
                for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_ctime_ns")
            ):
                raise EvidenceContractError(
                    "evidence authority socket changed during connection"
                )
            client.sendall(request)
            response = bytearray()
            while len(response) <= MAX_SECURE_ARTIFACT_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as exc:
        raise EvidenceContractError("evidence authority did not sign the proof") from exc
    raw = bytes(response).split(b"\n", 1)[0]
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceContractError("evidence authority returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise EvidenceContractError("evidence authority response must be an object")
    _exact_keys(parsed, {"schema_version", "signature"}, field="evidence authority response")
    if parsed["schema_version"] != "propertyquarry.evidence-sign-response.v1":
        raise EvidenceContractError("evidence authority response schema is invalid")
    _decode_base64url(parsed["signature"], expected_bytes=64, field="evidence authority signature")
    return str(parsed["signature"])


def validate_evidence_time(
    value: object,
    *,
    field: str,
    now: datetime,
    challenge: EvidenceChallenge,
    maximum_age_seconds: int = MAX_EVIDENCE_AGE_SECONDS,
) -> datetime:
    captured_at = _timestamp(value, field=field)
    checked_at = now.astimezone(timezone.utc)
    if captured_at < challenge.issued_at - timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise EvidenceContractError(f"{field} predates the active challenge")
    if captured_at > challenge.expires_at or captured_at > checked_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise EvidenceContractError(f"{field} is future-dated or outside the active challenge")
    if (checked_at - captured_at).total_seconds() > maximum_age_seconds:
        raise EvidenceContractError(f"{field} is stale")
    return captured_at


def validate_range_query_contract(
    value: object,
    *,
    expected_expression: str,
) -> tuple[datetime, datetime, int]:
    """Validate the single canonical query ABI shared by every launch gate."""

    if not isinstance(value, dict):
        raise EvidenceContractError("Prometheus range query must be an object")
    _exact_keys(
        value,
        {"expression", "start", "end", "step_seconds", "contract_sha256"},
        field="Prometheus range query",
    )
    if value["expression"] != expected_expression:
        raise EvidenceContractError("Prometheus range query expression is not canonical")
    step_seconds = value["step_seconds"]
    if isinstance(step_seconds, bool) or not isinstance(step_seconds, int):
        raise EvidenceContractError("Prometheus range step_seconds must be a JSON integer")
    if step_seconds != RANGE_STEP_SECONDS:
        raise EvidenceContractError(
            f"Prometheus range step_seconds must equal the canonical {RANGE_STEP_SECONDS} seconds"
        )
    start = _timestamp(value["start"], field="Prometheus range start")
    end = _timestamp(value["end"], field="Prometheus range end")
    window_seconds = (end - start).total_seconds()
    if window_seconds < RANGE_WINDOW_SECONDS:
        raise EvidenceContractError("Prometheus range window is shorter than 30 days")
    if not window_seconds.is_integer() or int(window_seconds) % step_seconds:
        raise EvidenceContractError(
            "Prometheus range window is not aligned to the canonical query step"
        )
    contract = {
        "expression": expected_expression,
        "start": isoformat(start),
        "end": isoformat(end),
        "step_seconds": step_seconds,
    }
    stored_hash = _text(value["contract_sha256"], field="Prometheus range contract hash")
    if not SHA256_RE.fullmatch(stored_hash) or stored_hash != sha256_bytes(canonical_json_bytes(contract)):
        raise EvidenceContractError("Prometheus range query contract hash does not match")
    return start, end, step_seconds
