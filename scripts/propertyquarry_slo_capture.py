#!/usr/bin/env python3
"""Capture Docker-bound, authenticated PropertyQuarry SLO snapshot evidence."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


PROBE_SCHEMA = "propertyquarry.metrics_probe.v2"
PROBE_BUNDLE_SCHEMA = "propertyquarry.metrics_probe_bundle.v2"
SNAPSHOT_BUNDLE_SCHEMA = "propertyquarry.metrics_snapshot_bundle.v2"
CAPTURE_TOOL = "propertyquarry.slo_metrics_capture.v2"
DOCKER_API_SERVICE = "propertyquarry-api"
DOCKER_API_PORT = "8090/tcp"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
DISCOVERED_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
HOST_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
MAX_METRICS_BYTES = 8 * 1024 * 1024
MAX_VERSION_BYTES = 256 * 1024
MAX_REPLICAS = 32
PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class CaptureError(RuntimeError):
    """The capture request or private evidence contract is invalid."""


class ResponseLike(Protocol):
    headers: Mapping[str, str]

    def getcode(self) -> int: ...

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "ResponseLike": ...

    def __exit__(self, *args: object) -> object: ...


OpenUrl = Callable[..., ResponseLike]
CommandRunner = Callable[..., object]
Sleeper = Callable[[float], None]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _open_without_redirects(
    request: urllib.request.Request,
    *,
    timeout: int,
) -> ResponseLike:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True)
class CaptureConfig:
    base_url: str
    release_commit_sha: str
    release_image_digest: str
    metrics_snapshot_path: Path
    metrics_probe_path: Path
    # Deprecated expectations remain accepted for the existing operator wrapper.
    # Docker discovery/inspect remains the only provenance authority.
    replica_id: str = ""
    replica_count: int = 0
    token_env: str = "EA_API_TOKEN"
    principal_id: str = "propertyquarry-metrics"
    host_header: str = ""
    timeout_seconds: int = 20
    snapshot_interval_seconds: int = 60
    overwrite: bool = False


@dataclass(frozen=True)
class ContainerIdentity:
    container_id: str
    container_image_id: str
    replica_id: str
    release_commit_sha: str
    release_image_digest: str
    peer_ip: str
    peer_port: int
    scheme: str
    inspect_sha256: str

    def binding(self) -> dict[str, object]:
        return {
            "container_id": self.container_id,
            "container_image_id": self.container_image_id,
            "replica_id": self.replica_id,
            "release_commit_sha": self.release_commit_sha,
            "release_image_digest": self.release_image_digest,
            "docker_inspect_sha256": self.inspect_sha256,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def with_payload_hash(payload: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized["payload_sha256"] = sha256_bytes(canonical_bytes(normalized))
    return normalized


def normalize_release_sha(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(value):
        raise CaptureError("release commit must be a full 40-character Git SHA")
    return value


def normalize_image_digest(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not IMAGE_DIGEST_RE.fullmatch(value):
        raise CaptureError("release image digest must be sha256 followed by 64 hexadecimal characters")
    return value


def positive_int(raw: object, *, field_name: str, allow_zero: bool = False) -> int:
    value = str(raw if raw is not None else "").strip()
    if not value.isdigit():
        raise CaptureError(f"{field_name} must be a positive integer")
    parsed = int(value)
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise CaptureError(f"{field_name} must be a positive integer")
    return parsed


def _safe_header_value(raw: str, *, field_name: str, pattern: re.Pattern[str]) -> str:
    value = str(raw or "").strip()
    if not value or not pattern.fullmatch(value):
        raise CaptureError(f"{field_name} is invalid")
    return value


def _private_base_url(base_url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(str(base_url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise CaptureError("metrics base URL is invalid") from exc
    if parsed.scheme not in {"http", "https"}:
        raise CaptureError("metrics base URL must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise CaptureError("metrics base URL must contain an IP literal and no credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise CaptureError("metrics base URL must not contain a path, query, or fragment")
    try:
        address = ipaddress.ip_address(parsed.hostname.rstrip("."))
    except ValueError as exc:
        raise CaptureError("metrics capture target must be an IP literal") from exc
    if not (address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)):
        raise CaptureError("metrics capture target must be a loopback or private IP literal")
    if parsed.scheme == "http" and not address.is_loopback:
        raise CaptureError("plain HTTP metrics capture is restricted to literal loopback")
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, address.compressed, effective_port


def _target_url(identity: ContainerIdentity, path: str) -> str:
    host = f"[{identity.peer_ip}]" if ":" in identity.peer_ip else identity.peer_ip
    return urllib.parse.urlunsplit(
        (identity.scheme, f"{host}:{identity.peer_port}", path, "", "")
    )


def _cache_control_has_no_store(raw: str) -> bool:
    return any(
        directive.strip().lower() == "no-store"
        for directive in str(raw or "").split(",")
    )


def _validate_output_destination(path: Path, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise CaptureError(f"output already exists: {path}; choose a new path or use --overwrite")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _run_docker(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout_seconds: int,
) -> bytes:
    try:
        result = runner(
            list(argv),
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        raise CaptureError("Docker provenance query failed") from None
    if int(getattr(result, "returncode", 1)) != 0:
        raise CaptureError("Docker provenance query failed")
    stdout = getattr(result, "stdout", b"")
    if isinstance(stdout, str):
        return stdout.encode("utf-8")
    if not isinstance(stdout, bytes):
        raise CaptureError("Docker provenance query returned invalid output")
    return stdout


def _container_environment(raw: object) -> dict[str, str]:
    if not isinstance(raw, list):
        raise CaptureError("Docker inspect did not expose the container environment")
    values: dict[str, str] = {}
    for item in raw:
        key, separator, value = str(item or "").partition("=")
        if not separator or not key or key in values:
            raise CaptureError("Docker inspect container environment is ambiguous")
        values[key] = value
    return values


def _published_peer(inspect: Mapping[str, object], *, scheme: str) -> tuple[str, int]:
    network = inspect.get("NetworkSettings")
    if not isinstance(network, Mapping):
        raise CaptureError("Docker inspect is missing network settings")
    ports = network.get("Ports")
    if not isinstance(ports, Mapping):
        raise CaptureError("Docker inspect is missing published API ports")
    bindings = ports.get(DOCKER_API_PORT)
    if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], Mapping):
        raise CaptureError("each API replica must expose exactly one inspected host port")
    binding = bindings[0]
    host_ip = str(binding.get("HostIp") or "").strip()
    if host_ip in {"", "0.0.0.0"}:
        host_ip = "127.0.0.1"
    elif host_ip == "::":
        host_ip = "::1"
    try:
        address = ipaddress.ip_address(host_ip)
    except ValueError as exc:
        raise CaptureError("Docker inspect published peer must be an IP literal") from exc
    if not (address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)):
        raise CaptureError("Docker inspect published peer is not private")
    if scheme == "http" and not address.is_loopback:
        raise CaptureError("plain HTTP metrics capture is restricted to literal loopback")
    port = positive_int(binding.get("HostPort"), field_name="Docker published API port")
    if port > 65535:
        raise CaptureError("Docker published API port is invalid")
    return address.compressed, port


def _identity_from_inspect(
    raw: object,
    *,
    release_sha: str,
    image_digest: str,
    scheme: str,
) -> ContainerIdentity:
    if not isinstance(raw, Mapping):
        raise CaptureError("Docker inspect returned an invalid container object")
    container_id = str(raw.get("Id") or "").strip().lower()
    container_image_id = str(raw.get("Image") or "").strip().lower()
    state = raw.get("State")
    config = raw.get("Config")
    if (
        not CONTAINER_ID_RE.fullmatch(container_id)
        or not IMAGE_DIGEST_RE.fullmatch(container_image_id)
        or not isinstance(state, Mapping)
        or state.get("Running") is not True
        or not isinstance(config, Mapping)
    ):
        raise CaptureError("Docker inspect container identity is invalid or not running")
    labels = config.get("Labels")
    if not isinstance(labels, Mapping) or str(
        labels.get("com.docker.compose.service") or ""
    ) != DOCKER_API_SERVICE:
        raise CaptureError("Docker inspect container is not a PropertyQuarry API replica")
    replica_id = str(config.get("Hostname") or "").strip()
    if not REPLICA_ID_RE.fullmatch(replica_id):
        raise CaptureError("Docker inspect replica hostname is invalid")
    environment = _container_environment(config.get("Env"))
    if str(environment.get("EA_ROLE") or "").strip().lower() != "api":
        raise CaptureError("Docker inspect container does not run the API role")
    observed_release = normalize_release_sha(
        str(environment.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "")
    )
    observed_digest = normalize_image_digest(
        str(environment.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST") or "")
    )
    configured_image = str(config.get("Image") or "").strip().lower()
    configured_digest = configured_image.rsplit("@", 1)[-1]
    if not IMAGE_DIGEST_RE.fullmatch(configured_digest):
        raise CaptureError("Docker inspect configured image is not digest-pinned")
    if observed_release != release_sha:
        raise CaptureError("Docker container is not bound to the candidate release")
    if observed_digest != image_digest or configured_digest != image_digest:
        raise CaptureError("Docker container image does not match the candidate digest")
    image_revision = str(labels.get("org.opencontainers.image.revision") or "").strip().lower()
    if image_revision != release_sha:
        raise CaptureError("Docker image revision label diverges from the candidate release")
    peer_ip, peer_port = _published_peer(raw, scheme=scheme)
    safe_projection = {
        "container_id": container_id,
        "container_image_id": container_image_id,
        "replica_id": replica_id,
        "release_commit_sha": observed_release,
        "release_image_digest": observed_digest,
        "peer_ip": peer_ip,
        "peer_port": peer_port,
        "scheme": scheme,
        "service": DOCKER_API_SERVICE,
    }
    return ContainerIdentity(
        container_id=container_id,
        container_image_id=container_image_id,
        replica_id=replica_id,
        release_commit_sha=observed_release,
        release_image_digest=observed_digest,
        peer_ip=peer_ip,
        peer_port=peer_port,
        scheme=scheme,
        inspect_sha256=sha256_bytes(canonical_bytes(safe_projection)),
    )


def _discover_api_replicas(
    *,
    runner: CommandRunner,
    timeout_seconds: int,
    release_sha: str,
    image_digest: str,
    scheme: str,
) -> list[ContainerIdentity]:
    discovered_raw = _run_docker(
        runner,
        (
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={DOCKER_API_SERVICE}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ),
        timeout_seconds=timeout_seconds,
    )
    discovered = [line.strip().lower() for line in discovered_raw.decode("utf-8").splitlines() if line.strip()]
    if not discovered or len(discovered) > MAX_REPLICAS:
        raise CaptureError("Docker discovery must find a bounded non-empty API replica set")
    if len(set(discovered)) != len(discovered) or any(
        not DISCOVERED_ID_RE.fullmatch(item) for item in discovered
    ):
        raise CaptureError("Docker discovery returned duplicate or invalid container IDs")
    inspect_raw = _run_docker(
        runner,
        ("docker", "inspect", *discovered),
        timeout_seconds=timeout_seconds,
    )
    try:
        inspected = json.loads(inspect_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("Docker inspect returned invalid JSON") from exc
    if not isinstance(inspected, list) or len(inspected) != len(discovered):
        raise CaptureError("Docker inspect did not return every discovered API replica")
    identities = [
        _identity_from_inspect(
            item,
            release_sha=release_sha,
            image_digest=image_digest,
            scheme=scheme,
        )
        for item in inspected
    ]
    by_id = {identity.container_id: identity for identity in identities}
    if len(by_id) != len(identities) or any(
        sum(container_id.startswith(prefix) for container_id in by_id) != 1
        for prefix in discovered
    ):
        raise CaptureError("Docker discovery and inspect container identities diverge")
    replica_ids = [identity.replica_id for identity in identities]
    if len(set(replica_ids)) != len(replica_ids):
        raise CaptureError("Docker API replicas do not have distinct runtime identities")
    return sorted(identities, key=lambda item: item.container_id)


def _response_peer(response: ResponseLike, *, require_tls: bool) -> tuple[str, bool]:
    direct_peer = getattr(response, "peer_ip", None)
    direct_tls = getattr(response, "tls_verified", None)
    if direct_peer is not None:
        try:
            peer = ipaddress.ip_address(str(direct_peer)).compressed
        except ValueError as exc:
            raise CaptureError("HTTP response peer identity is invalid") from exc
        if require_tls and direct_tls is not True:
            raise CaptureError("HTTPS response did not prove a verified TLS peer")
        return peer, bool(direct_tls) if require_tls else False

    candidates = [
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(getattr(response, "raw", None), "_connection", None), "sock", None),
    ]
    for sock in candidates:
        if sock is None or not hasattr(sock, "getpeername"):
            continue
        try:
            peer = ipaddress.ip_address(str(sock.getpeername()[0])).compressed
        except (OSError, ValueError, TypeError, IndexError):
            continue
        if require_tls:
            try:
                if not sock.getpeercert():
                    raise CaptureError("HTTPS response did not prove a verified TLS peer")
            except AttributeError as exc:
                raise CaptureError("HTTPS response did not prove a verified TLS peer") from exc
        return peer, require_tls
    raise CaptureError("HTTP response did not expose its connected peer identity")


def _headers_reflect_token(headers: Mapping[str, str], token: str) -> bool:
    return any(token in str(value or "") for value in headers.values())


def _request_payload(
    *,
    identity: ContainerIdentity,
    path: str,
    token: str,
    principal_id: str,
    host_header: str,
    timeout_seconds: int,
    maximum_bytes: int,
    authenticated: bool,
    open_url: OpenUrl,
) -> tuple[bytes, dict[str, object]]:
    headers = {"Accept": "text/plain" if path == "/internal/metrics" else "application/json"}
    if authenticated:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-EA-Principal-ID"] = principal_id
    if host_header:
        headers["Host"] = host_header
    target_url = _target_url(identity, path)
    request = urllib.request.Request(target_url, headers=headers, method="GET")
    try:
        with open_url(request, timeout=timeout_seconds) as response:
            status = int(response.getcode())
            content_type = str(response.headers.get("Content-Type") or "").strip()
            cache_control = str(response.headers.get("Cache-Control") or "").strip()
            peer_ip, tls_verified = _response_peer(
                response,
                require_tls=identity.scheme == "https",
            )
            if peer_ip != identity.peer_ip:
                raise CaptureError("HTTP response connected peer does not match Docker inspect")
            payload = response.read(maximum_bytes + 1)
            reflected_header = _headers_reflect_token(response.headers, token)
    except urllib.error.HTTPError as exc:
        raise CaptureError(f"private runtime request returned HTTP {exc.code}") from None
    except CaptureError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CaptureError("private runtime request failed") from None
    except Exception:
        raise CaptureError("private runtime request failed") from None
    if status != 200:
        raise CaptureError(f"private runtime request returned HTTP {status}")
    if not payload:
        raise CaptureError("private runtime response is empty")
    if len(payload) > maximum_bytes:
        raise CaptureError("private runtime response exceeds the evidence size limit")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureError("private runtime response must be UTF-8") from exc
    if token.encode("utf-8") in payload or reflected_header:
        raise CaptureError("runtime response reflected the bearer credential")
    return payload, {
        "endpoint_path": path,
        "authenticated": authenticated,
        "private_route": True,
        "credential_persisted": False,
        "http_status": status,
        "content_type": content_type,
        "cache_control": cache_control,
        "connected_peer_ip": peer_ip,
        "tls_verified": tls_verified,
    }


def _validate_version(payload: bytes, identity: ContainerIdentity) -> dict[str, object]:
    try:
        version = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CaptureError("/version returned invalid JSON") from exc
    if not isinstance(version, Mapping):
        raise CaptureError("/version returned an invalid object")
    expected = {
        "release_commit_sha": identity.release_commit_sha,
        "release_image_digest": identity.release_image_digest,
        "replica_id": identity.replica_id,
        "role": "api",
    }
    if any(str(version.get(key) or "").strip() != value for key, value in expected.items()):
        raise CaptureError("/version identity diverges from Docker inspect")
    return {
        **expected,
        "response_sha256": sha256_bytes(payload),
    }


def _parse_metric_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    position = 0
    pattern = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"(?:,|$)')
    while position < len(raw):
        match = pattern.match(raw, position)
        if match is None:
            raise CaptureError("runtime build info labels are invalid")
        labels[match.group(1)] = (
            match.group(2).replace(r"\n", "\n").replace(r'\"', '"').replace(r"\\", "\\")
        )
        position = match.end()
    return labels


def _validate_metrics_identity(payload: bytes, identity: ContainerIdentity) -> None:
    matches: list[dict[str, str]] = []
    for line in payload.decode("utf-8").splitlines():
        prefix = "propertyquarry_runtime_build_info{"
        if not line.startswith(prefix):
            continue
        match = re.fullmatch(r"propertyquarry_runtime_build_info\{([^}]*)\}\s+1(?:\.0+)?", line.strip())
        if match is None:
            raise CaptureError("runtime build info metric is invalid")
        matches.append(_parse_metric_labels(match.group(1)))
    expected = {
        "release_commit_sha": identity.release_commit_sha,
        "release_image_digest": identity.release_image_digest,
        "replica_id": identity.replica_id,
    }
    if len(matches) != 1 or matches[0] != expected:
        raise CaptureError("metrics runtime identity diverges from Docker inspect")


def _snapshot_path(base: Path, identity: ContainerIdentity, phase: str) -> Path:
    short_id = identity.container_id[:12]
    return base.with_name(f"{base.stem}.{short_id}.{phase}.prom")


def _replica_probe_path(base: Path, identity: ContainerIdentity) -> Path:
    return base.with_name(f"{base.stem}.{identity.container_id[:12]}.json")


def _same_inventory(
    before: Sequence[ContainerIdentity],
    after: Sequence[ContainerIdentity],
) -> bool:
    return [item.__dict__ for item in before] == [item.__dict__ for item in after]


def capture_metrics(
    config: CaptureConfig,
    *,
    open_url: OpenUrl = _open_without_redirects,
    docker_runner: CommandRunner = subprocess.run,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    sleeper: Sleeper = time.sleep,
) -> dict[str, object]:
    release_sha = normalize_release_sha(config.release_commit_sha)
    image_digest = normalize_image_digest(config.release_image_digest)
    principal_id = _safe_header_value(
        config.principal_id,
        field_name="principal ID",
        pattern=PRINCIPAL_ID_RE,
    )
    timeout_seconds = positive_int(config.timeout_seconds, field_name="timeout")
    interval_seconds = positive_int(
        config.snapshot_interval_seconds,
        field_name="snapshot interval",
    )
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", str(config.token_env or "")):
        raise CaptureError("token environment variable name is invalid")
    scheme, requested_ip, requested_port = _private_base_url(config.base_url)
    if config.metrics_snapshot_path.resolve() == config.metrics_probe_path.resolve():
        raise CaptureError("metrics snapshot and probe receipt paths must be different")
    _validate_output_destination(config.metrics_snapshot_path, overwrite=config.overwrite)
    _validate_output_destination(config.metrics_probe_path, overwrite=config.overwrite)
    host_header = ""
    if config.host_header:
        host_header = _safe_header_value(
            config.host_header,
            field_name="host header",
            pattern=HOST_HEADER_RE,
        )
    env = environ if environ is not None else os.environ
    token = str(env.get(config.token_env) or "").strip()
    if not token:
        raise CaptureError(f"authenticated metrics token is missing from {config.token_env}")

    identities = _discover_api_replicas(
        runner=docker_runner,
        timeout_seconds=timeout_seconds,
        release_sha=release_sha,
        image_digest=image_digest,
        scheme=scheme,
    )
    if not any(
        item.peer_ip == requested_ip and item.peer_port == requested_port
        for item in identities
    ):
        raise CaptureError("metrics base URL does not match any Docker-inspected API endpoint")
    if config.replica_count:
        expected_count = positive_int(config.replica_count, field_name="deprecated replica count")
        if expected_count != len(identities):
            raise CaptureError("deprecated replica count diverges from Docker discovery")
    if config.replica_id:
        expected_id = _safe_header_value(
            config.replica_id,
            field_name="deprecated replica ID",
            pattern=REPLICA_ID_RE,
        )
        if len(identities) != 1 or expected_id not in {
            identities[0].replica_id,
            identities[0].container_id,
        }:
            raise CaptureError("deprecated replica ID diverges from Docker inspect")

    start_at = now.astimezone(timezone.utc) if now is not None else utc_now()
    captured: dict[str, dict[str, object]] = {}
    for identity in identities:
        version_payload, version_transport = _request_payload(
            identity=identity,
            path="/version",
            token=token,
            principal_id=principal_id,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
            maximum_bytes=MAX_VERSION_BYTES,
            authenticated=False,
            open_url=open_url,
        )
        version = _validate_version(version_payload, identity)
        metrics, transport = _request_payload(
            identity=identity,
            path="/internal/metrics",
            token=token,
            principal_id=principal_id,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
            maximum_bytes=MAX_METRICS_BYTES,
            authenticated=True,
            open_url=open_url,
        )
        if not str(transport["content_type"]).lower().startswith("text/plain"):
            raise CaptureError("metrics response content type is not Prometheus text")
        if not _cache_control_has_no_store(str(transport["cache_control"])):
            raise CaptureError("metrics response must include Cache-Control: no-store")
        _validate_metrics_identity(metrics, identity)
        captured[identity.container_id] = {
            "identity": identity,
            "version": version,
            "version_transport": version_transport,
            "start_metrics": metrics,
            "start_transport": transport,
        }

    sleeper(float(interval_seconds))
    end_identities = _discover_api_replicas(
        runner=docker_runner,
        timeout_seconds=timeout_seconds,
        release_sha=release_sha,
        image_digest=image_digest,
        scheme=scheme,
    )
    if not _same_inventory(identities, end_identities):
        raise CaptureError("Docker API replica inventory changed during the snapshot window")
    end_at = (
        start_at + timedelta(seconds=interval_seconds)
        if now is not None
        else utc_now()
    )
    if end_at <= start_at:
        raise CaptureError("metrics snapshot window did not advance")
    for identity in end_identities:
        version_payload, _version_transport = _request_payload(
            identity=identity,
            path="/version",
            token=token,
            principal_id=principal_id,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
            maximum_bytes=MAX_VERSION_BYTES,
            authenticated=False,
            open_url=open_url,
        )
        end_version = _validate_version(version_payload, identity)
        if end_version != captured[identity.container_id]["version"]:
            raise CaptureError("/version identity changed during the snapshot window")
        metrics, transport = _request_payload(
            identity=identity,
            path="/internal/metrics",
            token=token,
            principal_id=principal_id,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
            maximum_bytes=MAX_METRICS_BYTES,
            authenticated=True,
            open_url=open_url,
        )
        if not str(transport["content_type"]).lower().startswith("text/plain"):
            raise CaptureError("metrics response content type is not Prometheus text")
        if not _cache_control_has_no_store(str(transport["cache_control"])):
            raise CaptureError("metrics response must include Cache-Control: no-store")
        _validate_metrics_identity(metrics, identity)
        captured[identity.container_id]["end_metrics"] = metrics
        captured[identity.container_id]["end_transport"] = transport

    output_payloads: list[tuple[Path, bytes]] = []
    manifest_replicas: list[dict[str, object]] = []
    probe_replicas: list[dict[str, object]] = []
    for identity in identities:
        row = captured[identity.container_id]
        start_metrics = bytes(row["start_metrics"])
        end_metrics = bytes(row["end_metrics"])
        start_path = _snapshot_path(config.metrics_snapshot_path, identity, "start")
        end_path = _snapshot_path(config.metrics_snapshot_path, identity, "end")
        replica_probe_path = _replica_probe_path(config.metrics_probe_path, identity)
        for path in (start_path, end_path, replica_probe_path):
            _validate_output_destination(path, overwrite=config.overwrite)
        start_evidence = {
            "captured_at": isoformat(start_at),
            "path": start_path.name,
            "sha256": sha256_bytes(start_metrics),
            "bytes": len(start_metrics),
        }
        end_evidence = {
            "captured_at": isoformat(end_at),
            "path": end_path.name,
            "sha256": sha256_bytes(end_metrics),
            "bytes": len(end_metrics),
        }
        manifest_replicas.append(
            {
                **identity.binding(),
                "start": start_evidence,
                "end": end_evidence,
            }
        )
        replica_probe = with_payload_hash(
            {
                "schema": PROBE_SCHEMA,
                "capture_tool": CAPTURE_TOOL,
                "captured_at": isoformat(end_at),
                **identity.binding(),
                "version": row["version"],
                "version_transport": row["version_transport"],
                "snapshots": [
                    {**start_evidence, **dict(row["start_transport"])},
                    {**end_evidence, **dict(row["end_transport"])},
                ],
            }
        )
        replica_probe_bytes = (
            json.dumps(replica_probe, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        probe_replicas.append(
            {
                "replica_id": identity.replica_id,
                "container_id": identity.container_id,
                "path": replica_probe_path.name,
                "sha256": sha256_bytes(replica_probe_bytes),
                "bytes": len(replica_probe_bytes),
            }
        )
        output_payloads.extend(
            (
                (start_path, start_metrics),
                (end_path, end_metrics),
                (replica_probe_path, replica_probe_bytes),
            )
        )

    snapshot_manifest = with_payload_hash(
        {
            "schema": SNAPSHOT_BUNDLE_SCHEMA,
            "capture_tool": CAPTURE_TOOL,
            "release_commit_sha": release_sha,
            "release_image_digest": image_digest,
            "window_start": isoformat(start_at),
            "window_end": isoformat(end_at),
            "window_seconds": (end_at - start_at).total_seconds(),
            "replica_count": len(identities),
            "replicas": manifest_replicas,
        }
    )
    manifest_bytes = (
        json.dumps(snapshot_manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    probe_bundle = with_payload_hash(
        {
            "schema": PROBE_BUNDLE_SCHEMA,
            "capture_tool": CAPTURE_TOOL,
            "captured_at": isoformat(end_at),
            "release_commit_sha": release_sha,
            "release_image_digest": image_digest,
            "replica_count": len(identities),
            "snapshot_bundle_sha256": sha256_bytes(manifest_bytes),
            "snapshot_bundle_bytes": len(manifest_bytes),
            "replicas": probe_replicas,
            "credential_persisted": False,
        }
    )
    probe_bundle_bytes = (
        json.dumps(probe_bundle, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    output_payloads.extend(
        (
            (config.metrics_snapshot_path, manifest_bytes),
            (config.metrics_probe_path, probe_bundle_bytes),
        )
    )
    paths = [path.resolve() for path, _payload in output_payloads]
    if len(set(paths)) != len(paths):
        raise CaptureError("derived metrics evidence paths are not distinct")
    written: list[Path] = []
    try:
        for path, payload in output_payloads:
            _atomic_write(path, payload)
            written.append(path)
    except OSError:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise CaptureError("could not write private metrics evidence") from None
    return probe_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture Docker-bound private PropertyQuarry SLO metrics evidence."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--replica-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--replica-count", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--metrics-snapshot", type=Path, required=True)
    parser.add_argument("--metrics-probe", type=Path, required=True)
    parser.add_argument("--token-env", default="EA_API_TOKEN")
    parser.add_argument("--principal-id", default="propertyquarry-metrics")
    parser.add_argument("--host-header", default="")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--snapshot-interval-seconds", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = CaptureConfig(
        base_url=args.base_url,
        release_commit_sha=args.release_sha,
        release_image_digest=args.image_digest,
        replica_id=args.replica_id,
        replica_count=args.replica_count,
        metrics_snapshot_path=args.metrics_snapshot,
        metrics_probe_path=args.metrics_probe,
        token_env=args.token_env,
        principal_id=args.principal_id,
        host_header=args.host_header,
        timeout_seconds=args.timeout_seconds,
        snapshot_interval_seconds=args.snapshot_interval_seconds,
        overwrite=args.overwrite,
    )
    try:
        probe = capture_metrics(config)
    except CaptureError as exc:
        print(f"PropertyQuarry SLO capture failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "captured",
                "captured_at": probe["captured_at"],
                "replica_count": probe["replica_count"],
                "metrics_snapshot_bundle": str(config.metrics_snapshot_path),
                "metrics_probe_bundle": str(config.metrics_probe_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
