#!/usr/bin/env python3
"""Produce and strictly verify bounded local PropertyQuarry capacity evidence.

This lane is deliberately a lab measurement, not a production sizing claim.  It
can load only a loopback HTTP endpoint, can query only a loopback PostgreSQL
endpoint in read-only transactions, and exercises the real in-memory
PropertyQuarry search-work queue state machine.  Production traffic, providers,
and browser/render fleets are outside this producer's authority.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Mapping, Sequence


ROOT: Final = Path(__file__).resolve().parents[1]
EA_ROOT: Final = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

SCHEMA_VERSION: Final = "propertyquarry.local_capacity_evidence.v1"
PRODUCER: Final = "propertyquarry-local-capacity-evidence"
VERIFIER: Final = "propertyquarry-local-capacity-evidence-verifier"
PROFILE_NAME: Final = "propertyquarry-bounded-local-capacity-lab-v1"
GIT_BINARY: Final = Path("/usr/bin/git")
MAX_RECEIPT_BYTES: Final = 4 * 1024 * 1024
MAX_SOURCE_FILES: Final = 100_000
MAX_SOURCE_FILE_BYTES: Final = 128 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES: Final = 4 * 1024 * 1024
COMMIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

SCOPE: Final[dict[str, object]] = {
    "kind": "bounded_local_lab",
    "production_capacity_established": False,
    "production_traffic_contacted": False,
    "external_services_contacted": False,
    "claim": "local_measurement_only_not_production_sizing",
    "limitations": [
        "Results describe one bounded local sample and are not an extrapolation of production capacity.",
        "The queue workload uses PropertyQuarry's real in-memory lease state machine, not the production durable queue.",
        "PostgreSQL measurements use a probe-owned bounded pool and read-only SELECT 1 transactions, not the application production pool.",
        "The loopback API URL hash is not an independently attested runtime-image identity; deployed-image binding remains required.",
        "Browser/render worker capacity and provider quota capacity require separate governed runtime evidence.",
        "Host and cgroup counters can include other processes in the same execution boundary where explicitly identified.",
    ],
}

PROFILE: Final[dict[str, object]] = {
    "name": PROFILE_NAME,
    "api": {
        "method": "GET",
        "request_count": 40,
        "concurrency": 4,
        "timeout_ms": 2_000,
        "maximum_response_bytes": MAX_HTTP_RESPONSE_BYTES,
    },
    "postgres": {
        "connection_count": 4,
        "pool_size": 4,
        "query_count": 40,
        "concurrency": 4,
        "connect_timeout_ms": 3_000,
        "statement_timeout_ms": 2_000,
        "transaction_mode": "read_only",
        "query_shape": "SELECT 1",
    },
    "queue_scheduler_worker": {
        "job_count": 240,
        "worker_count": 4,
        "lease_seconds": 30,
        "implementation": "app.product.property_search_work_queue.InMemoryPropertySearchWorkQueue",
        "durability": "in_memory_fixture_not_production_queue",
    },
    "host_sampler": {
        "interval_ms": 10,
        "filesystem_scope": "repository_filesystem",
        "process_scope": "capacity_producer_process",
        "cgroup_scope": "current_execution_boundary_when_available",
    },
}

THRESHOLDS: Final[dict[str, object]] = {
    "api": {
        "p95_latency_ms_maximum": 750.0,
        "error_count_maximum": 0,
        "requests_per_second_minimum": 5.0,
        "response_bytes_maximum": 32 * 1024 * 1024,
    },
    "postgres": {
        "connect_p95_ms_maximum": 1_500.0,
        "query_p95_ms_maximum": 300.0,
        "connection_error_count_maximum": 0,
        "query_error_count_maximum": 0,
        "queries_per_second_minimum": 5.0,
        "open_connections_after_maximum": 0,
    },
    "queue_scheduler_worker": {
        "scheduler_items_per_second_minimum": 20.0,
        "worker_items_per_second_minimum": 20.0,
        "error_count_maximum": 0,
        "final_depth_maximum": 0,
    },
    "host": {
        "normalized_cpu_percent_maximum": 95.0,
        "rss_growth_bytes_maximum": 128 * 1024 * 1024,
        "thread_count_maximum": 64,
        "disk_free_bytes_minimum": 512 * 1024 * 1024,
        "disk_free_percent_minimum": 2.0,
        "cgroup_pid_headroom_minimum": 16,
        "cgroup_memory_headroom_bytes_minimum": 128 * 1024 * 1024,
    },
}


class CapacityEvidenceError(RuntimeError):
    """The local capacity receipt is unsafe, malformed, or inconsistent."""


def _canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CapacityEvidenceError("value is not canonical JSON") from exc
    return rendered.encode("utf-8")


def _payload_sha256(payload: Mapping[str, object]) -> str:
    unhashed = copy.deepcopy(dict(payload))
    unhashed.pop("payload_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(unhashed)).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip() or not value.endswith("Z"):
        raise CapacityEvidenceError(f"{field} must be a trimmed UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CapacityEvidenceError(f"{field} is not an ISO-8601 timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _reject_constant(value: str) -> object:
    raise CapacityEvidenceError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityEvidenceError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def load_receipt(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CapacityEvidenceError(f"receipt is unreadable: {path}") from exc
    if not raw or len(raw) > MAX_RECEIPT_BYTES:
        raise CapacityEvidenceError("receipt size is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapacityEvidenceError("receipt is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CapacityEvidenceError("receipt root must be an object")
    return value


def _atomic_write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _run_git(repo_root: Path, arguments: Sequence[str]) -> bytes:
    if not GIT_BINARY.is_file():
        raise CapacityEvidenceError(f"pinned Git binary is missing: {GIT_BINARY}")
    completed = subprocess.run(
        [str(GIT_BINARY), "-C", str(repo_root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()[:240]
        raise CapacityEvidenceError(f"pinned Git command failed: {error or completed.returncode}")
    return completed.stdout


def _source_path_list(repo_root: Path) -> list[str]:
    raw = _run_git(
        repo_root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    )
    chunks = raw.split(b"\0")
    if chunks and chunks[-1] == b"":
        chunks.pop()
    if not chunks or len(chunks) > MAX_SOURCE_FILES:
        raise CapacityEvidenceError("source file inventory size is invalid")
    paths: list[str] = []
    for raw_path in chunks:
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapacityEvidenceError("source path is not UTF-8") from exc
        if (
            not path
            or len(path.encode("utf-8")) > 1_024
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise CapacityEvidenceError("source path inventory contains an unsafe path")
        paths.append(path)
    if paths != sorted(set(paths)):
        paths = sorted(set(paths))
    return paths


def _hash_regular_file(path: Path, metadata: os.stat_result) -> tuple[str, int]:
    if metadata.st_size < 0 or metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise CapacityEvidenceError(f"source file is too large: {path.name}")
    digest = hashlib.sha256()
    observed = 0
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CapacityEvidenceError(f"source file cannot be opened safely: {path.name}") from exc
    opened = os.fstat(descriptor)
    identity_before = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    identity_opened = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    if identity_before != identity_opened or not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise CapacityEvidenceError(f"source file changed before safe open: {path.name}")
    with os.fdopen(descriptor, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > MAX_SOURCE_FILE_BYTES:
                raise CapacityEvidenceError(f"source file grew beyond the limit: {path.name}")
            digest.update(chunk)
        opened_after = os.fstat(handle.fileno())
    after = path.lstat()
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    identity_opened_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_mode,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    )
    if identity_before != identity_after or identity_before != identity_opened_after or observed != after.st_size:
        raise CapacityEvidenceError(f"source file changed during identity capture: {path.name}")
    return digest.hexdigest(), observed


def collect_source_identity(repo_root: Path) -> dict[str, object]:
    root = repo_root.expanduser().resolve(strict=True)
    commit_sha = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip()
    if not COMMIT_SHA_RE.fullmatch(commit_sha):
        raise CapacityEvidenceError("repository HEAD is not a full lowercase commit SHA")

    status_before = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    paths_before = _source_path_list(root)
    manifest: list[dict[str, object]] = []
    total_bytes = 0
    for relative in paths_before:
        path = root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            manifest.append({"path": relative, "kind": "missing", "mode": 0, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()})
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            digest, size = _hash_regular_file(path, metadata)
            kind = "file"
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            encoded = target.encode("utf-8")
            if len(encoded) > 4_096:
                raise CapacityEvidenceError("source symlink target is too large")
            after = path.lstat()
            if (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise CapacityEvidenceError("source symlink changed during identity capture")
            digest, size, kind = hashlib.sha256(encoded).hexdigest(), len(encoded), "symlink"
        else:
            raise CapacityEvidenceError(f"unsupported source file type: {relative}")
        total_bytes += size
        if total_bytes > MAX_SOURCE_TOTAL_BYTES:
            raise CapacityEvidenceError("source inventory exceeds the total byte limit")
        manifest.append({"path": relative, "kind": kind, "mode": mode, "bytes": size, "sha256": digest})

    paths_after = _source_path_list(root)
    status_after = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if paths_before != paths_after or status_before != status_after:
        raise CapacityEvidenceError("source inventory changed during identity capture")
    return {
        "commit_sha": commit_sha,
        "source_tree_sha256": hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest(),
        "source_tree_method": "git_tracked_and_nonignored_untracked_current_content_manifest_v1",
        "source_file_count": len(manifest),
        "source_total_bytes": total_bytes,
        "working_tree_clean": status_after == b"",
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


def _duration_seconds(start: float, end: float) -> float:
    return round(max(0.000001, end - start), 6)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


def _loopback_http_target(raw: str) -> tuple[str, dict[str, object]]:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise CapacityEvidenceError("API URL must be a non-empty trimmed string")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise CapacityEvidenceError("API URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise CapacityEvidenceError("API URL must be an absolute query-free HTTP(S) URL")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise CapacityEvidenceError("API URL must use a loopback IP literal") from exc
    if not address.is_loopback:
        raise CapacityEvidenceError("API URL must use a loopback IP literal")
    if len(parsed.path.encode("utf-8")) > 1_024 or "\\" in parsed.path or any(char.isspace() or ord(char) < 32 for char in parsed.path):
        raise CapacityEvidenceError("API URL path is unsafe")
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return normalized, {
        "scheme": parsed.scheme,
        "host_class": "loopback",
        "port": effective_port,
        "path": parsed.path,
        "target_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _empty_api(reason: str) -> dict[str, object]:
    api_profile = PROFILE["api"]
    assert isinstance(api_profile, dict)
    return {
        "state": "not_measured",
        "reason": reason,
        "target": {"scheme": "", "host_class": "", "port": 0, "path": "", "target_sha256": ""},
        "workload": copy.deepcopy(api_profile),
        "sample": {"started_at": "", "completed_at": "", "window_seconds": 0.0},
        "observations": {
            "attempted_requests": 0,
            "successful_requests": 0,
            "error_count": 0,
            "status_counts": {},
            "response_bytes": 0,
            "latency_samples_ms": [],
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "max_latency_ms": 0.0,
            "requests_per_second": 0.0,
        },
    }


def measure_api(api_url: str | None) -> dict[str, object]:
    if not api_url:
        return _empty_api("loopback_api_url_not_supplied")
    target_url, target = _loopback_http_target(api_url)
    api_profile = PROFILE["api"]
    assert isinstance(api_profile, dict)
    request_count = int(api_profile["request_count"])
    concurrency = int(api_profile["concurrency"])
    timeout_seconds = int(api_profile["timeout_ms"]) / 1_000.0
    maximum_bytes = int(api_profile["maximum_response_bytes"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request_once() -> tuple[int, int, float, str]:
        started = time.perf_counter()
        status_code = 0
        response_bytes = 0
        error = ""
        try:
            request = urllib.request.Request(
                target_url,
                method="GET",
                headers={"Accept": "application/json,text/plain;q=0.9,*/*;q=0.1", "User-Agent": PRODUCER},
            )
            with opener.open(request, timeout=timeout_seconds) as response:
                status_code = int(response.status)
                body = response.read(maximum_bytes + 1)
                response_bytes = len(body)
                if response_bytes > maximum_bytes:
                    error = "response_too_large"
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            try:
                response_bytes = len(exc.read(maximum_bytes + 1))
            except Exception:
                response_bytes = 0
            error = "http_status_error"
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            error = type(exc).__name__
        latency_ms = round((time.perf_counter() - started) * 1_000.0, 3)
        if not 200 <= status_code < 300 and not error:
            error = "non_success_status"
        return status_code, response_bytes, latency_ms, error

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="pq-capacity-api") as executor:
        rows = list(executor.map(lambda _index: request_once(), range(request_count)))
    completed = time.perf_counter()
    completed_at = datetime.now(timezone.utc)
    window = _duration_seconds(started, completed)
    latencies = [row[2] for row in rows]
    status_counts: dict[str, int] = {}
    for status_code, _size, _latency, _error in rows:
        key = str(status_code)
        status_counts[key] = status_counts.get(key, 0) + 1
    successes = sum(1 for status_code, _size, _latency, error in rows if 200 <= status_code < 300 and not error)
    return {
        "state": "measured",
        "reason": "",
        "target": target,
        "workload": copy.deepcopy(api_profile),
        "sample": {"started_at": _iso(started_at), "completed_at": _iso(completed_at), "window_seconds": window},
        "observations": {
            "attempted_requests": len(rows),
            "successful_requests": successes,
            "error_count": len(rows) - successes,
            "status_counts": dict(sorted(status_counts.items())),
            "response_bytes": sum(row[1] for row in rows),
            "latency_samples_ms": latencies,
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "max_latency_ms": round(max(latencies, default=0.0), 3),
            "requests_per_second": round(len(rows) / window, 3),
        },
    }


def _read_private_dsn(path: Path) -> str:
    candidate = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise CapacityEvidenceError("PostgreSQL DSN file cannot be opened safely") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise CapacityEvidenceError("PostgreSQL DSN file must be a regular no-follow file")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise CapacityEvidenceError("PostgreSQL DSN file must be owned by the caller and mode 0600 or stricter")
    if metadata.st_size <= 0 or metadata.st_size > 4_096:
        os.close(descriptor)
        raise CapacityEvidenceError("PostgreSQL DSN file size is invalid")
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(4_097)
        after = os.fstat(handle.fileno())
    if (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise CapacityEvidenceError("PostgreSQL DSN file changed during read")
    try:
        dsn = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CapacityEvidenceError("PostgreSQL DSN file is not UTF-8") from exc
    if not dsn or "\n" in dsn or "\r" in dsn:
        raise CapacityEvidenceError("PostgreSQL DSN file must contain exactly one non-empty line")
    return dsn


def _empty_postgres(reason: str) -> dict[str, object]:
    pg_profile = PROFILE["postgres"]
    assert isinstance(pg_profile, dict)
    return {
        "state": "not_measured",
        "reason": reason,
        "target": {"transport": "", "port": 0, "database_name_sha256": "", "server_version_num": ""},
        "workload": copy.deepcopy(pg_profile),
        "sample": {"started_at": "", "completed_at": "", "window_seconds": 0.0},
        "observations": {
            "connection_attempts": 0,
            "connected": 0,
            "connection_error_count": 0,
            "connect_latency_samples_ms": [],
            "connect_p95_ms": 0.0,
            "query_attempts": 0,
            "query_successes": 0,
            "query_error_count": 0,
            "pool_acquire_latency_samples_ms": [],
            "query_latency_samples_ms": [],
            "query_p95_ms": 0.0,
            "queries_per_second": 0.0,
            "peak_checked_out": 0,
        },
        "cleanup": {"connections_closed": 0, "open_connections_after": 0},
    }


def _postgres_target(dsn: str) -> tuple[dict[str, object], Mapping[str, str]]:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as exc:
        raise CapacityEvidenceError("psycopg is unavailable") from exc
    try:
        values = conninfo_to_dict(dsn)
    except Exception as exc:
        raise CapacityEvidenceError("PostgreSQL DSN is invalid") from exc
    host = str(values.get("host") or "").strip()
    hosts = [item.strip() for item in host.split(",") if item.strip()]
    if len(hosts) != 1:
        raise CapacityEvidenceError("PostgreSQL DSN must name exactly one loopback host")
    hostname = hosts[0].rstrip(".").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise CapacityEvidenceError("PostgreSQL DSN host must be a loopback IP literal") from exc
    if not address.is_loopback:
        raise CapacityEvidenceError("PostgreSQL DSN host must be a loopback IP literal")
    hostaddr = str(values.get("hostaddr") or "").strip()
    if hostaddr:
        addresses = [item.strip() for item in hostaddr.split(",") if item.strip()]
        if len(addresses) != 1:
            raise CapacityEvidenceError("PostgreSQL DSN hostaddr must name exactly one loopback IP literal")
        try:
            parsed_hostaddr = ipaddress.ip_address(addresses[0])
        except ValueError as exc:
            raise CapacityEvidenceError("PostgreSQL DSN hostaddr must be a loopback IP literal") from exc
        if not parsed_hostaddr.is_loopback:
            raise CapacityEvidenceError("PostgreSQL DSN hostaddr must be a loopback IP literal")
    raw_port = str(values.get("port") or "5432").split(",", 1)[0]
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise CapacityEvidenceError("PostgreSQL DSN port is invalid") from exc
    if port < 1 or port > 65535:
        raise CapacityEvidenceError("PostgreSQL DSN port is invalid")
    database = str(values.get("dbname") or "").strip()
    if not database:
        raise CapacityEvidenceError("PostgreSQL DSN must explicitly name a database")
    return {
        "transport": "loopback_tcp",
        "port": port,
        "database_name_sha256": hashlib.sha256(database.encode("utf-8")).hexdigest(),
        "server_version_num": "",
    }, values


def measure_postgres(dsn_file: Path | None) -> dict[str, object]:
    if dsn_file is None:
        return _empty_postgres("loopback_postgres_dsn_file_not_supplied")
    try:
        import psycopg
    except ImportError:
        return _empty_postgres("psycopg_unavailable")
    dsn = _read_private_dsn(dsn_file)
    target, _values = _postgres_target(dsn)
    pg_profile = PROFILE["postgres"]
    assert isinstance(pg_profile, dict)
    pool_size = int(pg_profile["pool_size"])
    connection_count = int(pg_profile["connection_count"])
    query_count = int(pg_profile["query_count"])
    concurrency = int(pg_profile["concurrency"])
    connect_timeout = int(pg_profile["connect_timeout_ms"]) // 1_000
    statement_timeout = int(pg_profile["statement_timeout_ms"])
    connections: list[object] = []
    connect_latencies: list[float] = []
    connection_errors = 0
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    server_version = ""
    pool: queue.Queue[object] = queue.Queue(maxsize=pool_size)
    connections_closed = 0
    checked_out = 0
    peak_checked_out = 0
    checked_out_lock = threading.Lock()
    result: dict[str, object] | None = None

    try:
        for _index in range(connection_count):
            connection_started = time.perf_counter()
            connection: object | None = None
            try:
                connection = psycopg.connect(
                    dsn,
                    autocommit=True,
                    connect_timeout=max(1, connect_timeout),
                    options=(
                        "-c default_transaction_read_only=on "
                        f"-c statement_timeout={statement_timeout} "
                        "-c lock_timeout=500 -c idle_in_transaction_session_timeout=2000 "
                        "-c application_name=propertyquarry_capacity_evidence"
                    ),
                )
                with connection.cursor() as cursor:
                    cursor.execute("SHOW default_transaction_read_only")
                    row = cursor.fetchone()
                    if not row or str(row[0]).lower() != "on":
                        raise CapacityEvidenceError("PostgreSQL connection is not default read-only")
                    cursor.execute("SHOW server_version_num")
                    version_row = cursor.fetchone()
                    version = str(version_row[0] if version_row else "")
                    if not server_version:
                        server_version = version
                    elif server_version != version:
                        raise CapacityEvidenceError("PostgreSQL pool spans different server versions")
                connections.append(connection)
                pool.put(connection)
            except CapacityEvidenceError:
                if connection is not None:
                    try:
                        connection.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                raise
            except Exception:
                connection_errors += 1
                if connection is not None:
                    try:
                        connection.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
            finally:
                connect_latencies.append(round((time.perf_counter() - connection_started) * 1_000.0, 3))

        acquire_latencies: list[float] = []
        query_latencies: list[float] = []

        def query_once() -> bool:
            nonlocal checked_out, peak_checked_out
            acquire_started = time.perf_counter()
            try:
                connection = pool.get(timeout=max(1.0, statement_timeout / 1_000.0))
            except queue.Empty:
                acquire_latencies.append(round((time.perf_counter() - acquire_started) * 1_000.0, 3))
                return False
            acquire_latencies.append(round((time.perf_counter() - acquire_started) * 1_000.0, 3))
            with checked_out_lock:
                checked_out += 1
                peak_checked_out = max(peak_checked_out, checked_out)
            query_started = time.perf_counter()
            try:
                with connection.transaction():  # type: ignore[attr-defined]
                    with connection.cursor() as cursor:  # type: ignore[attr-defined]
                        cursor.execute("SET TRANSACTION READ ONLY")
                        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(statement_timeout),))
                        cursor.execute("SHOW transaction_read_only")
                        mode = cursor.fetchone()
                        if not mode or str(mode[0]).lower() != "on":
                            raise CapacityEvidenceError("PostgreSQL probe transaction is not read-only")
                        cursor.execute("SELECT 1")
                        row = cursor.fetchone()
                        if row != (1,):
                            raise CapacityEvidenceError("PostgreSQL probe query returned an unexpected value")
                return True
            except CapacityEvidenceError:
                raise
            except Exception:
                return False
            finally:
                query_latencies.append(round((time.perf_counter() - query_started) * 1_000.0, 3))
                with checked_out_lock:
                    checked_out -= 1
                pool.put(connection)

        if connections:
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="pq-capacity-pg") as executor:
                query_results = list(executor.map(lambda _index: query_once(), range(query_count)))
        else:
            query_results = [False] * query_count
            acquire_latencies = [0.0] * query_count
            query_latencies = [0.0] * query_count
        completed = time.perf_counter()
        completed_at = datetime.now(timezone.utc)
        window = _duration_seconds(started, completed)
        target["server_version_num"] = server_version
        result = {
            "state": "measured",
            "reason": "",
            "target": target,
            "workload": copy.deepcopy(pg_profile),
            "sample": {"started_at": _iso(started_at), "completed_at": _iso(completed_at), "window_seconds": window},
            "observations": {
                "connection_attempts": connection_count,
                "connected": len(connections),
                "connection_error_count": connection_errors,
                "connect_latency_samples_ms": connect_latencies,
                "connect_p95_ms": _percentile(connect_latencies, 0.95),
                "query_attempts": len(query_results),
                "query_successes": sum(1 for result in query_results if result),
                "query_error_count": sum(1 for result in query_results if not result),
                "pool_acquire_latency_samples_ms": acquire_latencies,
                "query_latency_samples_ms": query_latencies,
                "query_p95_ms": _percentile(query_latencies, 0.95),
                "queries_per_second": round(sum(1 for result in query_results if result) / window, 3),
                "peak_checked_out": peak_checked_out,
            },
            "cleanup": {"connections_closed": 0, "open_connections_after": len(connections)},
        }
    finally:
        for connection in connections:
            try:
                connection.close()  # type: ignore[attr-defined]
                connections_closed += 1
            except Exception:
                pass
    if result is None:
        raise CapacityEvidenceError("PostgreSQL measurement did not produce a result")
    result["cleanup"] = {
        "connections_closed": connections_closed,
        "open_connections_after": len(connections) - connections_closed,
    }
    return result


def measure_queue_scheduler_worker() -> dict[str, object]:
    from app.product.property_search_work_queue import InMemoryPropertySearchWorkQueue

    profile = PROFILE["queue_scheduler_worker"]
    assert isinstance(profile, dict)
    job_count = int(profile["job_count"])
    worker_count = int(profile["worker_count"])
    lease_seconds = int(profile["lease_seconds"])
    work_queue = InMemoryPropertySearchWorkQueue()
    errors: list[str] = []

    scheduler_started_at = datetime.now(timezone.utc)
    scheduler_started = time.perf_counter()
    scheduled = 0
    for index in range(job_count):
        try:
            result = work_queue.enqueue_run(
                run_record={"principal_id": "capacity-fixture", "run_id": f"capacity-{index:06d}"},
                payload_json={"kind": "capacity_fixture", "ordinal": index},
                idempotency_key=f"capacity-fixture:{index:06d}",
                max_attempts=1,
            )
            if result.created:
                scheduled += 1
        except Exception as exc:
            errors.append(type(exc).__name__)
    scheduler_completed = time.perf_counter()
    scheduler_completed_at = datetime.now(timezone.utc)
    after_schedule = work_queue.observability_snapshot()

    completed_count = 0
    completed_lock = threading.Lock()
    drain_started_at = datetime.now(timezone.utc)
    drain_started = time.perf_counter()

    def worker(worker_index: int) -> int:
        owner = f"capacity-worker-{worker_index}"
        local_completed = 0
        while True:
            job = work_queue.claim(lease_owner=owner, lease_seconds=lease_seconds)
            if job is None:
                break
            finished = work_queue.complete(job_id=job.job_id, lease_owner=owner)
            if finished is None:
                errors.append("complete_returned_none")
                break
            local_completed += 1
        return local_completed

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pq-capacity-worker") as executor:
        worker_results = list(executor.map(worker, range(worker_count)))
    with completed_lock:
        completed_count += sum(worker_results)
    drain_completed = time.perf_counter()
    drain_completed_at = datetime.now(timezone.utc)
    final_snapshot = work_queue.observability_snapshot()
    scheduler_window = _duration_seconds(scheduler_started, scheduler_completed)
    drain_window = _duration_seconds(drain_started, drain_completed)
    return {
        "state": "measured",
        "reason": "",
        "workload": copy.deepcopy(profile),
        "scheduler_sample": {
            "started_at": _iso(scheduler_started_at),
            "completed_at": _iso(scheduler_completed_at),
            "window_seconds": scheduler_window,
        },
        "worker_sample": {
            "started_at": _iso(drain_started_at),
            "completed_at": _iso(drain_completed_at),
            "window_seconds": drain_window,
        },
        "observations": {
            "initial_depth": 0,
            "scheduled": scheduled,
            "peak_depth": after_schedule.depth,
            "completed": completed_count,
            "final_depth": final_snapshot.depth,
            "error_count": len(errors),
            "scheduler_items_per_second": round(scheduled / scheduler_window, 3),
            "worker_items_per_second": round(completed_count / drain_window, 3),
        },
        "cleanup": {"active_jobs_after": final_snapshot.depth, "fixture_released": True},
    }


def _proc_status() -> tuple[int | None, int | None]:
    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    rss_bytes: int | None = None
    threads: int | None = None
    for line in lines:
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                rss_bytes = int(parts[1]) * 1024
        elif line.startswith("Threads:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                threads = int(parts[1])
    return rss_bytes, threads


def _proc_io() -> tuple[int | None, int | None]:
    try:
        lines = Path("/proc/self/io").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    values: dict[str, int] = {}
    for line in lines:
        key, separator, raw = line.partition(":")
        if separator and raw.strip().isdigit():
            values[key] = int(raw.strip())
    return values.get("read_bytes"), values.get("write_bytes")


def _current_cgroup_v2_directory() -> Path | None:
    try:
        rows = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    paths = [row.split("::", 1)[1] for row in rows if row.startswith("0::") and "::" in row]
    if len(paths) != 1:
        return None
    raw_path = paths[0]
    parts = [part for part in raw_path.split("/") if part]
    if not raw_path.startswith("/") or any(part in {".", ".."} for part in parts):
        return None
    root = Path("/sys/fs/cgroup")
    candidate = root.joinpath(*parts)
    try:
        metadata = candidate.stat()
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    return candidate


def _read_cgroup_number(name: str) -> tuple[int | None, str]:
    if name not in {"memory.current", "memory.max", "pids.current", "pids.max"}:
        return None, "invalid"
    directory = _current_cgroup_v2_directory()
    if directory is None:
        return None, "unavailable"
    path = directory / name
    try:
        raw = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None, "unavailable"
    if raw == "max":
        return None, "unbounded"
    try:
        value = int(raw)
    except ValueError:
        return None, "invalid"
    if value < 0:
        return None, "invalid"
    return value, "measured"


@dataclass
class _HostBaseline:
    captured_at: datetime
    monotonic: float
    process_cpu_seconds: float
    rss_bytes: int | None
    threads: int | None
    disk_total: int
    disk_free: int
    read_bytes: int | None
    write_bytes: int | None
    cgroup_memory_current: int | None
    cgroup_memory_maximum: int | None
    cgroup_memory_maximum_state: str
    cgroup_pids_current: int | None
    cgroup_pids_maximum: int | None
    cgroup_pids_maximum_state: str


def _host_baseline(repo_root: Path) -> _HostBaseline:
    rss, threads = _proc_status()
    disk = shutil.disk_usage(repo_root)
    read_bytes, write_bytes = _proc_io()
    memory_current, _memory_current_state = _read_cgroup_number("memory.current")
    memory_maximum, memory_maximum_state = _read_cgroup_number("memory.max")
    pids_current, _pids_current_state = _read_cgroup_number("pids.current")
    pids_maximum, pids_maximum_state = _read_cgroup_number("pids.max")
    return _HostBaseline(
        captured_at=datetime.now(timezone.utc),
        monotonic=time.perf_counter(),
        process_cpu_seconds=time.process_time(),
        rss_bytes=rss,
        threads=threads,
        disk_total=disk.total,
        disk_free=disk.free,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        cgroup_memory_current=memory_current,
        cgroup_memory_maximum=memory_maximum,
        cgroup_memory_maximum_state=memory_maximum_state,
        cgroup_pids_current=pids_current,
        cgroup_pids_maximum=pids_maximum,
        cgroup_pids_maximum_state=pids_maximum_state,
    )


class HostSampler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.max_rss_bytes = 0
        self.max_threads = 0
        self.max_cgroup_memory_current = 0
        self.max_cgroup_pids_current = 0

    def start(self) -> None:
        if self._thread is not None:
            raise CapacityEvidenceError("host sampler cannot be started twice")
        self._thread = threading.Thread(target=self._sample_loop, name="pq-capacity-host-sampler", daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        interval = int(PROFILE["host_sampler"]["interval_ms"]) / 1_000.0  # type: ignore[index]
        while not self._stop.is_set():
            rss, threads = _proc_status()
            memory_current, _state = _read_cgroup_number("memory.current")
            pids_current, _pids_state = _read_cgroup_number("pids.current")
            self.max_rss_bytes = max(self.max_rss_bytes, int(rss or 0))
            self.max_threads = max(self.max_threads, int(threads or 0))
            self.max_cgroup_memory_current = max(self.max_cgroup_memory_current, int(memory_current or 0))
            self.max_cgroup_pids_current = max(self.max_cgroup_pids_current, int(pids_current or 0))
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise CapacityEvidenceError("host sampler did not stop")


def build_host_measurement(before: _HostBaseline, after: _HostBaseline, sampler: HostSampler) -> dict[str, object]:
    window = _duration_seconds(before.monotonic, after.monotonic)
    cpu_seconds = max(0.0, after.process_cpu_seconds - before.process_cpu_seconds)
    logical_cpus = max(1, os.cpu_count() or 1)
    normalized_cpu = round((cpu_seconds / window) * 100.0 / logical_cpus, 3)
    rss_before = int(before.rss_bytes or 0)
    rss_after = int(after.rss_bytes or 0)
    rss_peak = max(rss_before, rss_after, sampler.max_rss_bytes)
    disk_free_percent = round((after.disk_free / after.disk_total) * 100.0, 3) if after.disk_total else 0.0
    read_delta = None if before.read_bytes is None or after.read_bytes is None else max(0, after.read_bytes - before.read_bytes)
    write_delta = None if before.write_bytes is None or after.write_bytes is None else max(0, after.write_bytes - before.write_bytes)
    cgroup_memory_peak = max(int(before.cgroup_memory_current or 0), int(after.cgroup_memory_current or 0), sampler.max_cgroup_memory_current)
    cgroup_memory_headroom = (
        max(0, int(after.cgroup_memory_maximum) - cgroup_memory_peak)
        if after.cgroup_memory_maximum is not None
        else None
    )
    cgroup_pids_peak = max(int(before.cgroup_pids_current or 0), int(after.cgroup_pids_current or 0), sampler.max_cgroup_pids_current)
    cgroup_pids_headroom = (
        max(0, int(after.cgroup_pids_maximum) - cgroup_pids_peak)
        if after.cgroup_pids_maximum is not None
        else None
    )
    return {
        "state": "measured",
        "reason": "",
        "workload": copy.deepcopy(PROFILE["host_sampler"]),
        "sample": {"started_at": _iso(before.captured_at), "completed_at": _iso(after.captured_at), "window_seconds": window},
        "cpu": {
            "logical_cpu_count": logical_cpus,
            "process_cpu_seconds": round(cpu_seconds, 6),
            "normalized_cpu_percent": normalized_cpu,
            "normalization": "process_cpu_seconds_per_wall_second_divided_by_logical_cpu_count",
        },
        "memory": {
            "rss_before_bytes": rss_before,
            "rss_peak_bytes": rss_peak,
            "rss_after_bytes": rss_after,
            "rss_growth_bytes": max(0, rss_after - rss_before),
            "cgroup_current_peak_bytes": cgroup_memory_peak if cgroup_memory_peak else None,
            "cgroup_maximum_bytes": after.cgroup_memory_maximum,
            "cgroup_maximum_state": after.cgroup_memory_maximum_state,
            "cgroup_headroom_bytes": cgroup_memory_headroom,
        },
        "processes": {
            "producer_pid_count": 1,
            "thread_count_before": int(before.threads or 0),
            "thread_count_peak": max(int(before.threads or 0), int(after.threads or 0), sampler.max_threads),
            "thread_count_after": int(after.threads or 0),
            "cgroup_pids_current_peak": cgroup_pids_peak if cgroup_pids_peak else None,
            "cgroup_pids_maximum": after.cgroup_pids_maximum,
            "cgroup_pids_maximum_state": after.cgroup_pids_maximum_state,
            "cgroup_pids_headroom": cgroup_pids_headroom,
        },
        "disk": {
            "filesystem_scope": "repository_filesystem",
            "total_bytes": after.disk_total,
            "free_bytes_before": before.disk_free,
            "free_bytes_after": after.disk_free,
            "free_percent_after": disk_free_percent,
            "process_read_bytes_delta": read_delta,
            "process_write_bytes_delta": write_delta,
            "io_scope": "producer_process_proc_io_when_available",
        },
    }


def build_network_measurement(api: Mapping[str, object]) -> dict[str, object]:
    observations = api["observations"]
    assert isinstance(observations, dict)
    if api["state"] != "measured":
        return {
            "state": "not_measured",
            "reason": "loopback_api_workload_not_measured",
            "scope": "application_level_loopback_http_client",
            "request_count": 0,
            "response_bytes": 0,
            "error_count": 0,
            "request_bytes_state": "not_observable_with_standard_library_client",
            "interface_bytes_state": "not_process_isolated",
        }
    return {
        "state": "measured",
        "reason": "",
        "scope": "application_level_loopback_http_client",
        "request_count": observations["attempted_requests"],
        "response_bytes": observations["response_bytes"],
        "error_count": observations["error_count"],
        "request_bytes_state": "not_observable_with_standard_library_client",
        "interface_bytes_state": "not_process_isolated",
    }


def _check(
    name: str,
    status_value: str,
    observed: object,
    operator: str,
    threshold: object,
    unit: str,
    *,
    required: bool,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status_value,
        "observed": observed,
        "operator": operator,
        "threshold": threshold,
        "unit": unit,
        "required_for_local_pass": required,
    }


def evaluate_checks(measurements: Mapping[str, object]) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []

    api = measurements["api"]
    assert isinstance(api, dict)
    api_thresholds = THRESHOLDS["api"]
    assert isinstance(api_thresholds, dict)
    if api["state"] == "measured":
        observed = api["observations"]
        assert isinstance(observed, dict)
        values = (
            ("api_p95_latency", observed["p95_latency_ms"], "<=", api_thresholds["p95_latency_ms_maximum"], "ms"),
            ("api_errors", observed["error_count"], "<=", api_thresholds["error_count_maximum"], "requests"),
            ("api_throughput", observed["requests_per_second"], ">=", api_thresholds["requests_per_second_minimum"], "requests/second"),
            ("network_response_bytes", observed["response_bytes"], "<=", api_thresholds["response_bytes_maximum"], "bytes"),
        )
        for name, value, operator, threshold, unit in values:
            ok = value <= threshold if operator == "<=" else value >= threshold  # type: ignore[operator]
            checks.append(_check(name, "pass" if ok else "fail", value, operator, threshold, unit, required=True))
    else:
        for name, unit in (("api_p95_latency", "ms"), ("api_errors", "requests"), ("api_throughput", "requests/second"), ("network_response_bytes", "bytes")):
            checks.append(_check(name, "not_measured", api["reason"], "", None, unit, required=True))

    postgres = measurements["postgres"]
    assert isinstance(postgres, dict)
    pg_thresholds = THRESHOLDS["postgres"]
    assert isinstance(pg_thresholds, dict)
    if postgres["state"] == "measured":
        observed = postgres["observations"]
        cleanup = postgres["cleanup"]
        assert isinstance(observed, dict) and isinstance(cleanup, dict)
        values = (
            ("postgres_connect_p95", observed["connect_p95_ms"], "<=", pg_thresholds["connect_p95_ms_maximum"], "ms"),
            ("postgres_query_p95", observed["query_p95_ms"], "<=", pg_thresholds["query_p95_ms_maximum"], "ms"),
            ("postgres_connection_errors", observed["connection_error_count"], "<=", pg_thresholds["connection_error_count_maximum"], "connections"),
            ("postgres_query_errors", observed["query_error_count"], "<=", pg_thresholds["query_error_count_maximum"], "queries"),
            ("postgres_query_throughput", observed["queries_per_second"], ">=", pg_thresholds["queries_per_second_minimum"], "queries/second"),
            ("postgres_cleanup", cleanup["open_connections_after"], "<=", pg_thresholds["open_connections_after_maximum"], "connections"),
        )
        for name, value, operator, threshold, unit in values:
            ok = value <= threshold if operator == "<=" else value >= threshold  # type: ignore[operator]
            checks.append(_check(name, "pass" if ok else "fail", value, operator, threshold, unit, required=True))
    else:
        for name, unit in (("postgres_connect_p95", "ms"), ("postgres_query_p95", "ms"), ("postgres_connection_errors", "connections"), ("postgres_query_errors", "queries"), ("postgres_query_throughput", "queries/second"), ("postgres_cleanup", "connections")):
            checks.append(_check(name, "not_measured", postgres["reason"], "", None, unit, required=True))

    queue_measurement = measurements["queue_scheduler_worker"]
    assert isinstance(queue_measurement, dict)
    queue_observed = queue_measurement["observations"]
    queue_thresholds = THRESHOLDS["queue_scheduler_worker"]
    assert isinstance(queue_observed, dict) and isinstance(queue_thresholds, dict)
    queue_values = (
        ("queue_scheduler_throughput", queue_observed["scheduler_items_per_second"], ">=", queue_thresholds["scheduler_items_per_second_minimum"], "items/second"),
        ("queue_worker_throughput", queue_observed["worker_items_per_second"], ">=", queue_thresholds["worker_items_per_second_minimum"], "items/second"),
        ("queue_errors", queue_observed["error_count"], "<=", queue_thresholds["error_count_maximum"], "items"),
        ("queue_final_depth", queue_observed["final_depth"], "<=", queue_thresholds["final_depth_maximum"], "items"),
    )
    for name, value, operator, threshold, unit in queue_values:
        ok = value <= threshold if operator == "<=" else value >= threshold  # type: ignore[operator]
        checks.append(_check(name, "pass" if ok else "fail", value, operator, threshold, unit, required=True))

    host = measurements["host"]
    assert isinstance(host, dict)
    cpu = host["cpu"]
    memory = host["memory"]
    processes = host["processes"]
    disk = host["disk"]
    host_thresholds = THRESHOLDS["host"]
    assert all(isinstance(value, dict) for value in (cpu, memory, processes, disk, host_thresholds))
    host_values = (
        ("host_normalized_cpu", cpu["normalized_cpu_percent"], "<=", host_thresholds["normalized_cpu_percent_maximum"], "percent"),  # type: ignore[index]
        ("host_rss_growth", memory["rss_growth_bytes"], "<=", host_thresholds["rss_growth_bytes_maximum"], "bytes"),  # type: ignore[index]
        ("host_thread_count", processes["thread_count_peak"], "<=", host_thresholds["thread_count_maximum"], "threads"),  # type: ignore[index]
        ("host_disk_free_bytes", disk["free_bytes_after"], ">=", host_thresholds["disk_free_bytes_minimum"], "bytes"),  # type: ignore[index]
        ("host_disk_free_percent", disk["free_percent_after"], ">=", host_thresholds["disk_free_percent_minimum"], "percent"),  # type: ignore[index]
    )
    for name, value, operator, threshold, unit in host_values:
        ok = value <= threshold if operator == "<=" else value >= threshold  # type: ignore[operator]
        checks.append(_check(name, "pass" if ok else "fail", value, operator, threshold, unit, required=True))

    pids_headroom = processes["cgroup_pids_headroom"]  # type: ignore[index]
    if pids_headroom is None:
        checks.append(_check("cgroup_pid_headroom", "not_measured", processes["cgroup_pids_maximum_state"], "", None, "PIDs", required=False))  # type: ignore[index]
    else:
        threshold = host_thresholds["cgroup_pid_headroom_minimum"]  # type: ignore[index]
        checks.append(_check("cgroup_pid_headroom", "pass" if pids_headroom >= threshold else "fail", pids_headroom, ">=", threshold, "PIDs", required=False))
    memory_headroom = memory["cgroup_headroom_bytes"]  # type: ignore[index]
    if memory_headroom is None:
        checks.append(_check("cgroup_memory_headroom", "not_measured", memory["cgroup_maximum_state"], "", None, "bytes", required=False))  # type: ignore[index]
    else:
        threshold = host_thresholds["cgroup_memory_headroom_bytes_minimum"]  # type: ignore[index]
        checks.append(_check("cgroup_memory_headroom", "pass" if memory_headroom >= threshold else "fail", memory_headroom, ">=", threshold, "bytes", required=False))
    if disk["process_read_bytes_delta"] is None or disk["process_write_bytes_delta"] is None:  # type: ignore[index]
        checks.append(_check("process_disk_io_counters", "not_measured", "proc_io_unavailable", "", None, "bytes", required=False))
    else:
        checks.append(_check("process_disk_io_counters", "pass", int(disk["process_read_bytes_delta"]) + int(disk["process_write_bytes_delta"]), ">=", 0, "bytes", required=False))  # type: ignore[index]

    external = measurements["external_capacity"]
    assert isinstance(external, dict)
    for key, name in (("browser_render_workers", "browser_render_worker_capacity"), ("provider_quotas", "provider_quota_capacity")):
        item = external[key]
        assert isinstance(item, dict)
        checks.append(_check(name, "not_measured", item["reason"], "", None, "", required=False))
    return checks


def _summary(checks: Sequence[Mapping[str, object]]) -> dict[str, object]:
    required = [row for row in checks if row["required_for_local_pass"] is True]
    if any(row["status"] == "fail" for row in required):
        local_status = "local_thresholds_failed"
    elif any(row["status"] == "not_measured" for row in required):
        local_status = "partial_local_measurement"
    else:
        local_status = "local_thresholds_passed"
    return {
        "local_status": local_status,
        "production_status": "not_established",
        "production_capacity_established": False,
        "check_count": len(checks),
        "pass_count": sum(row["status"] == "pass" for row in checks),
        "fail_count": sum(row["status"] == "fail" for row in checks),
        "not_measured_count": sum(row["status"] == "not_measured" for row in checks),
        "required_not_measured_count": sum(row["status"] == "not_measured" for row in required),
    }


def build_capacity_receipt(
    *,
    source_identity: Mapping[str, object],
    api: Mapping[str, object],
    postgres: Mapping[str, object],
    queue_scheduler_worker: Mapping[str, object],
    host: Mapping[str, object],
    generated_at: datetime | None = None,
) -> dict[str, object]:
    measurements: dict[str, object] = {
        "api": copy.deepcopy(dict(api)),
        "postgres": copy.deepcopy(dict(postgres)),
        "queue_scheduler_worker": copy.deepcopy(dict(queue_scheduler_worker)),
        "host": copy.deepcopy(dict(host)),
        "network": build_network_measurement(api),
        "external_capacity": {
            "browser_render_workers": {
                "state": "not_measured",
                "reason": "requires_separate_governed_browser_and_render_runtime_fixture",
                "required_for_production": True,
            },
            "provider_quotas": {
                "state": "not_measured",
                "reason": "external_provider_contact_is_forbidden_in_this_local_lane",
                "required_for_production": True,
            },
        },
    }
    checks = evaluate_checks(measurements)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "scope": copy.deepcopy(SCOPE),
        "source_identity": copy.deepcopy(dict(source_identity)),
        "profile": copy.deepcopy(PROFILE),
        "thresholds": copy.deepcopy(THRESHOLDS),
        "measurements": measurements,
        "checks": checks,
        "summary": _summary(checks),
    }
    receipt["payload_sha256"] = _payload_sha256(receipt)
    return receipt


def _path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def produce_capacity_receipt(
    *,
    repo_root: Path,
    api_url: str | None,
    postgres_dsn_file: Path | None,
    require_clean_source: bool = False,
) -> dict[str, object]:
    resolved_repo_root = repo_root.expanduser().resolve(strict=True)
    if postgres_dsn_file is not None:
        resolved_dsn = Path(os.path.abspath(os.fspath(postgres_dsn_file.expanduser())))
        if _path_is_within(resolved_dsn, resolved_repo_root):
            raise CapacityEvidenceError("PostgreSQL DSN file must remain outside the source repository")
        postgres_dsn_file = resolved_dsn
    source_identity = collect_source_identity(resolved_repo_root)
    if require_clean_source and source_identity["working_tree_clean"] is not True:
        raise CapacityEvidenceError("source tree is not clean")
    before = _host_baseline(resolved_repo_root)
    sampler = HostSampler()
    sampler.start()
    try:
        api = measure_api(api_url)
        postgres = measure_postgres(postgres_dsn_file)
        queue_measurement = measure_queue_scheduler_worker()
    finally:
        sampler.stop()
    after = _host_baseline(resolved_repo_root)
    host = build_host_measurement(before, after, sampler)
    source_identity_after = collect_source_identity(resolved_repo_root)
    if source_identity_after != source_identity:
        raise CapacityEvidenceError("source candidate changed during the capacity workload")
    return build_capacity_receipt(
        source_identity=source_identity,
        api=api,
        postgres=postgres,
        queue_scheduler_worker=queue_measurement,
        host=host,
    )


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CapacityEvidenceError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], keys: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != keys:
        raise CapacityEvidenceError(
            f"{field} keys do not match the v1 contract; missing={sorted(keys - actual)}, unexpected={sorted(actual - keys)}"
        )


def _integer(value: object, *, field: str, minimum: int = 0, maximum: int = 10**12) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise CapacityEvidenceError(f"{field} is not a bounded integer")
    return value


def _number(value: object, *, field: str, minimum: float = 0.0, maximum: float = 10**15) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)) or float(value) < minimum or float(value) > maximum:
        raise CapacityEvidenceError(f"{field} is not a bounded finite number")
    return float(value)


def _sample(value: object, *, field: str, measured: bool) -> None:
    sample = _mapping(value, field=field)
    _exact_keys(sample, {"started_at", "completed_at", "window_seconds"}, field=field)
    window = _number(sample["window_seconds"], field=f"{field}.window_seconds", maximum=600.0)
    if not measured:
        if sample != {"started_at": "", "completed_at": "", "window_seconds": 0.0}:
            raise CapacityEvidenceError(f"{field} must be empty when not measured")
        return
    started = _parse_timestamp(sample["started_at"], field=f"{field}.started_at")
    completed = _parse_timestamp(sample["completed_at"], field=f"{field}.completed_at")
    actual = (completed - started).total_seconds()
    if actual < 0 or abs(actual - window) > 0.25:
        raise CapacityEvidenceError(f"{field}.window_seconds does not match timestamps")


def _validate_latency_samples(value: object, *, field: str, maximum_count: int) -> list[float]:
    if not isinstance(value, list) or len(value) > maximum_count:
        raise CapacityEvidenceError(f"{field} is not a bounded array")
    return [_number(item, field=f"{field}[{index}]", maximum=120_000.0) for index, item in enumerate(value)]


def _validate_api(value: object) -> None:
    api = _mapping(value, field="measurements.api")
    _exact_keys(api, {"state", "reason", "target", "workload", "sample", "observations"}, field="measurements.api")
    if api["state"] not in {"measured", "not_measured"} or not isinstance(api["reason"], str):
        raise CapacityEvidenceError("measurements.api state or reason is invalid")
    if api["workload"] != PROFILE["api"]:
        raise CapacityEvidenceError("measurements.api.workload differs from the fixed profile")
    measured = api["state"] == "measured"
    if measured != (api["reason"] == ""):
        raise CapacityEvidenceError("measurements.api reason does not match state")
    _sample(api["sample"], field="measurements.api.sample", measured=measured)
    target = _mapping(api["target"], field="measurements.api.target")
    _exact_keys(target, {"scheme", "host_class", "port", "path", "target_sha256"}, field="measurements.api.target")
    observations = _mapping(api["observations"], field="measurements.api.observations")
    _exact_keys(observations, {"attempted_requests", "successful_requests", "error_count", "status_counts", "response_bytes", "latency_samples_ms", "p50_latency_ms", "p95_latency_ms", "max_latency_ms", "requests_per_second"}, field="measurements.api.observations")
    attempted = _integer(observations["attempted_requests"], field="api.attempted_requests", maximum=1_000)
    successful = _integer(observations["successful_requests"], field="api.successful_requests", maximum=attempted)
    errors = _integer(observations["error_count"], field="api.error_count", maximum=attempted)
    response_bytes = _integer(observations["response_bytes"], field="api.response_bytes", maximum=MAX_HTTP_RESPONSE_BYTES * 1_000)
    samples = _validate_latency_samples(observations["latency_samples_ms"], field="api.latency_samples_ms", maximum_count=1_000)
    p50 = _number(observations["p50_latency_ms"], field="api.p50_latency_ms", maximum=120_000.0)
    p95 = _number(observations["p95_latency_ms"], field="api.p95_latency_ms", maximum=120_000.0)
    maximum_latency = _number(observations["max_latency_ms"], field="api.max_latency_ms", maximum=120_000.0)
    throughput = _number(observations["requests_per_second"], field="api.requests_per_second", maximum=10_000_000.0)
    status_counts = _mapping(observations["status_counts"], field="api.status_counts")
    for key, count in status_counts.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) < 0 or int(key) > 599:
            raise CapacityEvidenceError("API status count key is invalid")
        _integer(count, field=f"api.status_counts.{key}", maximum=attempted)
    if measured:
        target_sha = target["target_sha256"]
        if target["scheme"] not in {"http", "https"} or target["host_class"] != "loopback" or not isinstance(target["path"], str) or not str(target["path"]).startswith("/") or not isinstance(target_sha, str) or not SHA256_RE.fullmatch(target_sha):
            raise CapacityEvidenceError("measured API target is invalid")
        _integer(target["port"], field="api.target.port", minimum=1, maximum=65535)
        if attempted != int(PROFILE["api"]["request_count"]) or successful + errors != attempted or len(samples) != attempted or sum(int(value) for value in status_counts.values()) != attempted:  # type: ignore[index]
            raise CapacityEvidenceError("measured API observation counts are inconsistent")
        expected = (_percentile(samples, 0.50), _percentile(samples, 0.95), round(max(samples), 3))
        actual = (p50, p95, maximum_latency)
        if expected != actual:
            raise CapacityEvidenceError("API latency aggregates do not match raw samples")
        window = float(_mapping(api["sample"], field="api.sample")["window_seconds"])
        if abs(throughput - round(attempted / window, 3)) > 0.001:
            raise CapacityEvidenceError("API throughput does not match count and window")
    else:
        if api["reason"] != "loopback_api_url_not_supplied":
            raise CapacityEvidenceError("unmeasured API reason is not recognized")
        if target != {"scheme": "", "host_class": "", "port": 0, "path": "", "target_sha256": ""}:
            raise CapacityEvidenceError("unmeasured API target must be empty")
        if attempted or successful or errors or response_bytes or samples or status_counts or p50 or p95 or maximum_latency or throughput:
            raise CapacityEvidenceError("unmeasured API observations must be empty")


def _validate_postgres(value: object) -> None:
    pg = _mapping(value, field="measurements.postgres")
    _exact_keys(pg, {"state", "reason", "target", "workload", "sample", "observations", "cleanup"}, field="measurements.postgres")
    if pg["state"] not in {"measured", "not_measured"} or not isinstance(pg["reason"], str):
        raise CapacityEvidenceError("measurements.postgres state or reason is invalid")
    if pg["workload"] != PROFILE["postgres"]:
        raise CapacityEvidenceError("measurements.postgres.workload differs from the fixed profile")
    measured = pg["state"] == "measured"
    if measured != (pg["reason"] == ""):
        raise CapacityEvidenceError("measurements.postgres reason does not match state")
    _sample(pg["sample"], field="measurements.postgres.sample", measured=measured)
    target = _mapping(pg["target"], field="measurements.postgres.target")
    _exact_keys(target, {"transport", "port", "database_name_sha256", "server_version_num"}, field="measurements.postgres.target")
    observations = _mapping(pg["observations"], field="measurements.postgres.observations")
    _exact_keys(observations, {"connection_attempts", "connected", "connection_error_count", "connect_latency_samples_ms", "connect_p95_ms", "query_attempts", "query_successes", "query_error_count", "pool_acquire_latency_samples_ms", "query_latency_samples_ms", "query_p95_ms", "queries_per_second", "peak_checked_out"}, field="measurements.postgres.observations")
    cleanup = _mapping(pg["cleanup"], field="measurements.postgres.cleanup")
    _exact_keys(cleanup, {"connections_closed", "open_connections_after"}, field="measurements.postgres.cleanup")
    connection_attempts = _integer(observations["connection_attempts"], field="postgres.connection_attempts", maximum=64)
    connected = _integer(observations["connected"], field="postgres.connected", maximum=64)
    connection_errors = _integer(observations["connection_error_count"], field="postgres.connection_error_count", maximum=64)
    connect_samples = _validate_latency_samples(observations["connect_latency_samples_ms"], field="postgres.connect_latency_samples_ms", maximum_count=64)
    query_attempts = _integer(observations["query_attempts"], field="postgres.query_attempts", maximum=1_000)
    query_successes = _integer(observations["query_successes"], field="postgres.query_successes", maximum=1_000)
    query_errors = _integer(observations["query_error_count"], field="postgres.query_error_count", maximum=1_000)
    acquire_samples = _validate_latency_samples(observations["pool_acquire_latency_samples_ms"], field="postgres.pool_acquire_latency_samples_ms", maximum_count=1_000)
    query_samples = _validate_latency_samples(observations["query_latency_samples_ms"], field="postgres.query_latency_samples_ms", maximum_count=1_000)
    connect_p95 = _number(observations["connect_p95_ms"], field="postgres.connect_p95_ms", maximum=120_000.0)
    query_p95 = _number(observations["query_p95_ms"], field="postgres.query_p95_ms", maximum=120_000.0)
    throughput = _number(observations["queries_per_second"], field="postgres.queries_per_second", maximum=10_000_000.0)
    peak = _integer(observations["peak_checked_out"], field="postgres.peak_checked_out", maximum=64)
    closed = _integer(cleanup["connections_closed"], field="postgres.connections_closed", maximum=64)
    open_after = _integer(cleanup["open_connections_after"], field="postgres.open_connections_after", maximum=64)
    if measured:
        server_version = target["server_version_num"]
        if target["transport"] != "loopback_tcp" or not isinstance(target["database_name_sha256"], str) or not SHA256_RE.fullmatch(target["database_name_sha256"]) or not isinstance(server_version, str) or (connected > 0 and not server_version.isdigit()) or (connected == 0 and server_version != ""):
            raise CapacityEvidenceError("measured PostgreSQL target is invalid")
        _integer(target["port"], field="postgres.target.port", minimum=1, maximum=65535)
        expected_connections = int(PROFILE["postgres"]["connection_count"])  # type: ignore[index]
        expected_queries = int(PROFILE["postgres"]["query_count"])  # type: ignore[index]
        if connection_attempts != expected_connections or connected + connection_errors != connection_attempts or len(connect_samples) != connection_attempts:
            raise CapacityEvidenceError("PostgreSQL connection observations are inconsistent")
        if query_attempts != expected_queries or query_successes + query_errors != expected_queries or len(acquire_samples) != query_attempts or len(query_samples) != query_attempts:
            raise CapacityEvidenceError("PostgreSQL query observations are inconsistent")
        if peak > int(PROFILE["postgres"]["pool_size"]) or closed + open_after != connected:  # type: ignore[index]
            raise CapacityEvidenceError("PostgreSQL pool or cleanup observations are inconsistent")
        if connect_p95 != _percentile(connect_samples, 0.95) or query_p95 != _percentile(query_samples, 0.95):
            raise CapacityEvidenceError("PostgreSQL latency aggregates do not match raw samples")
        window = float(_mapping(pg["sample"], field="postgres.sample")["window_seconds"])
        if abs(throughput - round(query_successes / window, 3)) > 0.001:
            raise CapacityEvidenceError("PostgreSQL throughput does not match count and window")
    else:
        if pg["reason"] not in {"loopback_postgres_dsn_file_not_supplied", "psycopg_unavailable"}:
            raise CapacityEvidenceError("unmeasured PostgreSQL reason is not recognized")
        if target != {"transport": "", "port": 0, "database_name_sha256": "", "server_version_num": ""}:
            raise CapacityEvidenceError("unmeasured PostgreSQL target must be empty")
        if any((connection_attempts, connected, connection_errors, connect_samples, connect_p95, query_attempts, query_successes, query_errors, acquire_samples, query_samples, query_p95, throughput, peak, closed, open_after)):
            raise CapacityEvidenceError("unmeasured PostgreSQL observations must be empty")


def _validate_queue(value: object) -> None:
    item = _mapping(value, field="measurements.queue_scheduler_worker")
    _exact_keys(item, {"state", "reason", "workload", "scheduler_sample", "worker_sample", "observations", "cleanup"}, field="measurements.queue_scheduler_worker")
    if item["state"] != "measured" or item["reason"] != "" or item["workload"] != PROFILE["queue_scheduler_worker"]:
        raise CapacityEvidenceError("queue workload must be the fixed measured fixture")
    _sample(item["scheduler_sample"], field="queue.scheduler_sample", measured=True)
    _sample(item["worker_sample"], field="queue.worker_sample", measured=True)
    observed = _mapping(item["observations"], field="queue.observations")
    _exact_keys(observed, {"initial_depth", "scheduled", "peak_depth", "completed", "final_depth", "error_count", "scheduler_items_per_second", "worker_items_per_second"}, field="queue.observations")
    cleanup = _mapping(item["cleanup"], field="queue.cleanup")
    _exact_keys(cleanup, {"active_jobs_after", "fixture_released"}, field="queue.cleanup")
    job_count = int(PROFILE["queue_scheduler_worker"]["job_count"])  # type: ignore[index]
    integer_values = {key: _integer(observed[key], field=f"queue.{key}", maximum=job_count) for key in ("initial_depth", "scheduled", "peak_depth", "completed", "final_depth", "error_count")}
    _number(observed["scheduler_items_per_second"], field="queue.scheduler_items_per_second", maximum=10_000_000)
    _number(observed["worker_items_per_second"], field="queue.worker_items_per_second", maximum=10_000_000)
    if integer_values["initial_depth"] != 0 or integer_values["scheduled"] > job_count or integer_values["peak_depth"] > job_count or integer_values["completed"] > integer_values["scheduled"] or cleanup["active_jobs_after"] != integer_values["final_depth"] or cleanup["fixture_released"] is not True:
        raise CapacityEvidenceError("queue observations or cleanup are inconsistent")
    scheduler_window = float(_mapping(item["scheduler_sample"], field="queue.scheduler_sample")["window_seconds"])
    worker_window = float(_mapping(item["worker_sample"], field="queue.worker_sample")["window_seconds"])
    if abs(float(observed["scheduler_items_per_second"]) - round(integer_values["scheduled"] / scheduler_window, 3)) > 0.001 or abs(float(observed["worker_items_per_second"]) - round(integer_values["completed"] / worker_window, 3)) > 0.001:
        raise CapacityEvidenceError("queue throughput does not match count and window")


def _validate_host(value: object) -> None:
    host = _mapping(value, field="measurements.host")
    _exact_keys(host, {"state", "reason", "workload", "sample", "cpu", "memory", "processes", "disk"}, field="measurements.host")
    if host["state"] != "measured" or host["reason"] != "" or host["workload"] != PROFILE["host_sampler"]:
        raise CapacityEvidenceError("host workload must be the fixed measured sampler")
    _sample(host["sample"], field="host.sample", measured=True)
    expected_keys = {
        "cpu": {"logical_cpu_count", "process_cpu_seconds", "normalized_cpu_percent", "normalization"},
        "memory": {"rss_before_bytes", "rss_peak_bytes", "rss_after_bytes", "rss_growth_bytes", "cgroup_current_peak_bytes", "cgroup_maximum_bytes", "cgroup_maximum_state", "cgroup_headroom_bytes"},
        "processes": {"producer_pid_count", "thread_count_before", "thread_count_peak", "thread_count_after", "cgroup_pids_current_peak", "cgroup_pids_maximum", "cgroup_pids_maximum_state", "cgroup_pids_headroom"},
        "disk": {"filesystem_scope", "total_bytes", "free_bytes_before", "free_bytes_after", "free_percent_after", "process_read_bytes_delta", "process_write_bytes_delta", "io_scope"},
    }
    sections = {name: _mapping(host[name], field=f"host.{name}") for name in expected_keys}
    for name, keys in expected_keys.items():
        _exact_keys(sections[name], keys, field=f"host.{name}")
    cpu, memory, processes, disk = sections["cpu"], sections["memory"], sections["processes"], sections["disk"]
    logical = _integer(cpu["logical_cpu_count"], field="host.logical_cpu_count", minimum=1, maximum=65_536)
    process_cpu = _number(cpu["process_cpu_seconds"], field="host.process_cpu_seconds", maximum=600.0)
    normalized = _number(cpu["normalized_cpu_percent"], field="host.normalized_cpu_percent", maximum=10_000.0)
    window = float(_mapping(host["sample"], field="host.sample")["window_seconds"])
    if cpu["normalization"] != "process_cpu_seconds_per_wall_second_divided_by_logical_cpu_count" or abs(normalized - round((process_cpu / window) * 100.0 / logical, 3)) > 0.01:
        raise CapacityEvidenceError("host CPU normalization is inconsistent")
    rss_before = _integer(memory["rss_before_bytes"], field="host.rss_before", maximum=10**15)
    rss_peak = _integer(memory["rss_peak_bytes"], field="host.rss_peak", maximum=10**15)
    rss_after = _integer(memory["rss_after_bytes"], field="host.rss_after", maximum=10**15)
    rss_growth = _integer(memory["rss_growth_bytes"], field="host.rss_growth", maximum=10**15)
    if rss_peak < max(rss_before, rss_after) or rss_growth != max(0, rss_after - rss_before):
        raise CapacityEvidenceError("host RSS observations are inconsistent")
    for key in ("cgroup_current_peak_bytes", "cgroup_maximum_bytes", "cgroup_headroom_bytes"):
        if memory[key] is not None:
            _integer(memory[key], field=f"host.memory.{key}", maximum=10**18)
    if memory["cgroup_maximum_state"] not in {"measured", "unbounded", "unavailable", "invalid"}:
        raise CapacityEvidenceError("cgroup memory maximum state is invalid")
    if (memory["cgroup_maximum_state"] == "measured") != (memory["cgroup_maximum_bytes"] is not None):
        raise CapacityEvidenceError("cgroup memory maximum state is inconsistent")
    if memory["cgroup_headroom_bytes"] is not None and memory["cgroup_maximum_bytes"] is not None and memory["cgroup_current_peak_bytes"] is not None and memory["cgroup_headroom_bytes"] != max(0, int(memory["cgroup_maximum_bytes"]) - int(memory["cgroup_current_peak_bytes"])):
        raise CapacityEvidenceError("cgroup memory headroom is inconsistent")
    if processes["producer_pid_count"] != 1:
        raise CapacityEvidenceError("producer PID count must be one")
    thread_before = _integer(processes["thread_count_before"], field="host.threads_before", maximum=100_000)
    thread_peak = _integer(processes["thread_count_peak"], field="host.threads_peak", maximum=100_000)
    thread_after = _integer(processes["thread_count_after"], field="host.threads_after", maximum=100_000)
    if thread_peak < max(thread_before, thread_after):
        raise CapacityEvidenceError("host thread peak is inconsistent")
    for key in ("cgroup_pids_current_peak", "cgroup_pids_maximum", "cgroup_pids_headroom"):
        if processes[key] is not None:
            _integer(processes[key], field=f"host.processes.{key}", maximum=10**9)
    if processes["cgroup_pids_maximum_state"] not in {"measured", "unbounded", "unavailable", "invalid"}:
        raise CapacityEvidenceError("cgroup PID maximum state is invalid")
    if (processes["cgroup_pids_maximum_state"] == "measured") != (processes["cgroup_pids_maximum"] is not None):
        raise CapacityEvidenceError("cgroup PID maximum state is inconsistent")
    if processes["cgroup_pids_headroom"] is not None and processes["cgroup_pids_maximum"] is not None and processes["cgroup_pids_current_peak"] is not None and processes["cgroup_pids_headroom"] != max(0, int(processes["cgroup_pids_maximum"]) - int(processes["cgroup_pids_current_peak"])):
        raise CapacityEvidenceError("cgroup PID headroom is inconsistent")
    total = _integer(disk["total_bytes"], field="host.disk.total", minimum=1, maximum=10**19)
    free_after = _integer(disk["free_bytes_after"], field="host.disk.free_after", maximum=total)
    _integer(disk["free_bytes_before"], field="host.disk.free_before", maximum=total)
    free_percent = _number(disk["free_percent_after"], field="host.disk.free_percent", maximum=100.0)
    if abs(free_percent - round((free_after / total) * 100.0, 3)) > 0.001 or disk["filesystem_scope"] != "repository_filesystem" or disk["io_scope"] != "producer_process_proc_io_when_available":
        raise CapacityEvidenceError("host disk observations are inconsistent")
    for key in ("process_read_bytes_delta", "process_write_bytes_delta"):
        if disk[key] is not None:
            _integer(disk[key], field=f"host.disk.{key}", maximum=10**18)


def _validate_network_and_external(measurements: Mapping[str, object]) -> None:
    network = _mapping(measurements["network"], field="measurements.network")
    _exact_keys(network, {"state", "reason", "scope", "request_count", "response_bytes", "error_count", "request_bytes_state", "interface_bytes_state"}, field="measurements.network")
    if network["scope"] != "application_level_loopback_http_client" or network["request_bytes_state"] != "not_observable_with_standard_library_client" or network["interface_bytes_state"] != "not_process_isolated":
        raise CapacityEvidenceError("network measurement scope is invalid")
    api = _mapping(measurements["api"], field="measurements.api")
    expected_network = build_network_measurement(api)
    if network != expected_network:
        raise CapacityEvidenceError("network measurement does not match API observations")
    external = _mapping(measurements["external_capacity"], field="measurements.external_capacity")
    _exact_keys(external, {"browser_render_workers", "provider_quotas"}, field="measurements.external_capacity")
    expected_external = {
        "browser_render_workers": {"state": "not_measured", "reason": "requires_separate_governed_browser_and_render_runtime_fixture", "required_for_production": True},
        "provider_quotas": {"state": "not_measured", "reason": "external_provider_contact_is_forbidden_in_this_local_lane", "required_for_production": True},
    }
    if external != expected_external:
        raise CapacityEvidenceError("external capacity states differ from the fail-closed contract")


def _validate_sample_recency(
    sample: object,
    *,
    field: str,
    generated_at: datetime,
) -> None:
    value = _mapping(sample, field=field)
    if not value["started_at"]:
        return
    started = _parse_timestamp(value["started_at"], field=f"{field}.started_at")
    completed = _parse_timestamp(value["completed_at"], field=f"{field}.completed_at")
    if started < generated_at - timedelta(minutes=15) or completed > generated_at + timedelta(minutes=5):
        raise CapacityEvidenceError(f"{field} is not contemporaneous with receipt generation")


def validate_capacity_receipt(
    payload: Mapping[str, object],
    *,
    expected_commit_sha: str,
    expected_source_tree_sha256: str,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(hours=24),
) -> dict[str, object]:
    _exact_keys(payload, {"schema_version", "producer", "generated_at", "scope", "source_identity", "profile", "thresholds", "measurements", "checks", "summary", "payload_sha256"}, field="receipt")
    if payload["schema_version"] != SCHEMA_VERSION or payload["producer"] != PRODUCER:
        raise CapacityEvidenceError("receipt schema or producer is invalid")
    if payload["scope"] != SCOPE:
        raise CapacityEvidenceError("receipt scope differs from the local-only contract")
    if payload["profile"] != PROFILE or payload["thresholds"] != THRESHOLDS:
        raise CapacityEvidenceError("receipt profile or thresholds differ from v1")
    if not isinstance(expected_commit_sha, str) or not COMMIT_SHA_RE.fullmatch(expected_commit_sha):
        raise CapacityEvidenceError("expected commit SHA is invalid")
    if not isinstance(expected_source_tree_sha256, str) or not SHA256_RE.fullmatch(expected_source_tree_sha256):
        raise CapacityEvidenceError("expected source tree SHA-256 is invalid")
    if maximum_age < timedelta(seconds=1) or maximum_age > timedelta(days=7):
        raise CapacityEvidenceError("maximum receipt age is outside the bounded verifier policy")
    generated_at = _parse_timestamp(payload["generated_at"], field="generated_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if generated_at > current + timedelta(minutes=5) or current - generated_at > maximum_age:
        raise CapacityEvidenceError("receipt is future-dated or stale")

    source = _mapping(payload["source_identity"], field="source_identity")
    _exact_keys(source, {"commit_sha", "source_tree_sha256", "source_tree_method", "source_file_count", "source_total_bytes", "working_tree_clean"}, field="source_identity")
    if source["commit_sha"] != expected_commit_sha or source["source_tree_sha256"] != expected_source_tree_sha256:
        raise CapacityEvidenceError("receipt belongs to a different source candidate")
    if source["source_tree_method"] != "git_tracked_and_nonignored_untracked_current_content_manifest_v1" or type(source["working_tree_clean"]) is not bool:
        raise CapacityEvidenceError("source identity method or cleanliness marker is invalid")
    _integer(source["source_file_count"], field="source_file_count", minimum=1, maximum=MAX_SOURCE_FILES)
    _integer(source["source_total_bytes"], field="source_total_bytes", maximum=MAX_SOURCE_TOTAL_BYTES)

    measurements = _mapping(payload["measurements"], field="measurements")
    _exact_keys(measurements, {"api", "postgres", "queue_scheduler_worker", "host", "network", "external_capacity"}, field="measurements")
    _validate_api(measurements["api"])
    _validate_postgres(measurements["postgres"])
    _validate_queue(measurements["queue_scheduler_worker"])
    _validate_host(measurements["host"])
    _validate_network_and_external(measurements)
    api = _mapping(measurements["api"], field="measurements.api")
    postgres = _mapping(measurements["postgres"], field="measurements.postgres")
    queue_measurement = _mapping(
        measurements["queue_scheduler_worker"],
        field="measurements.queue_scheduler_worker",
    )
    host = _mapping(measurements["host"], field="measurements.host")
    for sample_value, field in (
        (api["sample"], "measurements.api.sample"),
        (postgres["sample"], "measurements.postgres.sample"),
        (queue_measurement["scheduler_sample"], "measurements.queue_scheduler_worker.scheduler_sample"),
        (queue_measurement["worker_sample"], "measurements.queue_scheduler_worker.worker_sample"),
        (host["sample"], "measurements.host.sample"),
    ):
        _validate_sample_recency(
            sample_value,
            field=field,
            generated_at=generated_at,
        )

    recomputed_checks = evaluate_checks(measurements)
    if payload["checks"] != recomputed_checks:
        raise CapacityEvidenceError("stored checks do not match recomputed measurements")
    recomputed_summary = _summary(recomputed_checks)
    if payload["summary"] != recomputed_summary:
        raise CapacityEvidenceError("stored summary does not match recomputed checks")
    stored_hash = payload["payload_sha256"]
    if not isinstance(stored_hash, str) or not SHA256_RE.fullmatch(stored_hash) or stored_hash != _payload_sha256(payload):
        raise CapacityEvidenceError("payload_sha256 does not match canonical receipt content")
    return {
        "schema_version": "propertyquarry.local_capacity_evidence_verification.v1",
        "verifier": VERIFIER,
        "verified_at": _iso(current),
        "status": "verified_local_measurement",
        "local_status": recomputed_summary["local_status"],
        "production_capacity_established": False,
        "commit_sha": expected_commit_sha,
        "source_tree_sha256": expected_source_tree_sha256,
        "receipt_payload_sha256": stored_hash,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    measure = subparsers.add_parser("measure", help="run the bounded local measurement")
    measure.add_argument("--repo-root", default=str(ROOT))
    measure.add_argument("--api-url", default="")
    measure.add_argument("--postgres-dsn-file", default="")
    measure.add_argument("--require-clean-source", action="store_true")
    measure.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify", help="strictly verify a receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--expected-commit-sha", required=True)
    verify.add_argument("--expected-source-tree-sha256", required=True)
    verify.add_argument("--maximum-age-seconds", type=int, default=86_400)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "measure":
            resolved_repo_root = Path(args.repo_root).expanduser().resolve(strict=True)
            resolved_output = Path(args.output).expanduser().resolve()
            if _path_is_within(resolved_output, resolved_repo_root):
                raise CapacityEvidenceError("capacity receipt output must remain outside the source repository")
            receipt = produce_capacity_receipt(
                repo_root=resolved_repo_root,
                api_url=args.api_url or None,
                postgres_dsn_file=Path(args.postgres_dsn_file) if args.postgres_dsn_file else None,
                require_clean_source=bool(args.require_clean_source),
            )
            _atomic_write_private_json(resolved_output, receipt)
            print(json.dumps(receipt["summary"], sort_keys=True))
            return 0 if receipt["summary"]["local_status"] == "local_thresholds_passed" else 2  # type: ignore[index]
        if args.maximum_age_seconds < 1 or args.maximum_age_seconds > 604_800:
            raise CapacityEvidenceError("maximum age must be between 1 and 604800 seconds")
        receipt = load_receipt(Path(args.receipt))
        verification = validate_capacity_receipt(
            receipt,
            expected_commit_sha=args.expected_commit_sha,
            expected_source_tree_sha256=args.expected_source_tree_sha256,
            maximum_age=timedelta(seconds=args.maximum_age_seconds),
        )
        print(json.dumps(verification, sort_keys=True))
        return 0
    except (CapacityEvidenceError, OSError) as exc:
        print(f"propertyquarry_capacity_evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
