#!/usr/bin/python3.12 -I
"""Run the single fail-closed PropertyQuarry global Launch/Core terminal gate."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, NoReturn, Sequence
from urllib.parse import urlsplit


SOURCE_PATH = Path(__file__).resolve()
INSTALLED_ENTRYPOINT = Path(
    "/usr/libexec/propertyquarry/propertyquarry-global-launch-terminal"
)
INSTALLED_RUNTIME_ROOT = Path("/usr/libexec/propertyquarry/runtime")
INSTALLED_BUNDLE_MANIFEST_PATH = Path(
    "/usr/libexec/propertyquarry/global-launch-terminal-bundle.v1.json"
)
INSTALLED_PYTHON_PATH = Path("/usr/bin/python3.12")
if SOURCE_PATH == INSTALLED_ENTRYPOINT:
    ROOT = INSTALLED_RUNTIME_ROOT
    BUNDLED_RUNTIME_LAYOUT = True
elif (
    SOURCE_PATH.name == INSTALLED_ENTRYPOINT.name
    and (SOURCE_PATH.parent / "runtime").is_dir()
):
    # Build-bundle validation only. Installed-authority verification still
    # rejects this path before any Gold execution.
    ROOT = SOURCE_PATH.parent / "runtime"
    BUNDLED_RUNTIME_LAYOUT = True
else:
    ROOT = SOURCE_PATH.parents[1]
    BUNDLED_RUNTIME_LAYOUT = False
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import propertyquarry_evidence_contract as evidence_contract  # noqa: E402
from scripts import propertyquarry_release_preflight_policy as preflight_policy  # noqa: E402


MANIFEST_SCHEMA = "propertyquarry.global_launch_terminal_manifest.v1"
RESULT_SCHEMA = "propertyquarry.global_launch_terminal_result.v1"
CONTROLLER_ATTESTATION_SCHEMA = (
    "propertyquarry.global_launch_terminal_controller_attestation.v1"
)
CONTROLLER_ATTESTATION_DOMAIN = "propertyquarry/global-launch-terminal/v1"
INVOCATION_CONTRACT_SCHEMA = "propertyquarry.global_launch_invocation_contract.v1"
PERFORMANCE_BROWSER_POLICY_ENGINE = "chromium"
PERFORMANCE_BROWSER_POLICY_KEYS = frozenset(
    {"engine", "executable_path", "executable_sha256"}
)
# The launch product currently supports these deliberately reviewed public
# origin registries.  Expanding this fail-closed set is a controller/runtime
# contract change, not an inference from an arbitrary syntactically valid TLD.
SUPPORTED_GLOBAL_LAUNCH_ORIGIN_TLDS = frozenset({"ai", "at", "com", "io"})
SPECIAL_USE_OR_PRIVATE_DNS_SUFFIXES = frozenset(
    {
        "alt",
        "arpa",
        "corp",
        "example",
        "home",
        "internal",
        "invalid",
        "lan",
        "local",
        "localhost",
        "localdomain",
        "onion",
        "private",
        "test",
    }
)
INSTALLED_BUNDLE_SCHEMA = "propertyquarry.global_launch_terminal_bundle.v1"
PRODUCTION_DEPLOYMENT_ID = "propertyquarry-production"
RUNTIME_DEPLOYMENT_ID_PREFIX = "propertyquarry-governed-deploy-"
GOLD_STATUS_SCHEMA = "propertyquarry.gold_status.v1"
PREFLIGHT_DECISION_SCHEMA = "propertyquarry.release_preflight_decision.v1"
CAPACITY_RECEIPT_SCHEMA = "propertyquarry.production_capacity_receipt.v2"
DISASTER_RECOVERY_RECEIPT_SCHEMA = "propertyquarry.postgres_dr_receipt.v3"
OBSERVABILITY_OPERATIONS_RECEIPT_SCHEMA = (
    "propertyquarry.observability_operations_receipt.v1"
)

GLOBAL_LAUNCH_MANIFEST_PATH = (
    "/run/propertyquarry/release-evidence/global-launch-core-manifest.v1.json"
)
GLOBAL_LAUNCH_TERMINAL_COMMAND = (
    f"{INSTALLED_ENTRYPOINT} "
    f"--manifest {GLOBAL_LAUNCH_MANIFEST_PATH}"
)

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_JSON_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RAW_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_GOLD_RESULT_BYTES = 32 * 1024 * 1024
GOLD_TIMEOUT_SECONDS = 900
INSTALLED_PYTHON_FLAGS = ("-I",)
MAX_SLO_REPLICAS = 32
MAX_INSTALLED_TREE_ENTRIES = 16_384


def _gold_fd_bootstrap(runtime_root: Path) -> str:
    """Return the fixed isolated bootstrap for one verified runtime tree.

    ``run_path(..., run_name='__main__')`` deliberately preserves FD-pinned
    execution, but it also selects Gold's standalone import branch.  That
    branch imports sibling support modules by bare name, so both the package
    root and its verified ``scripts`` directory must be explicit search roots.
    """

    runtime_text = str(runtime_root)
    scripts_text = str(runtime_root / "scripts")
    return (
        "import runpy,sys;"
        f"sys.path[:0]=[{runtime_text!r},{scripts_text!r}];"
        "target=sys.argv[1];sys.argv=sys.argv[1:];"
        "runpy.run_path(target,run_name='__main__')"
    )


GOLD_FD_BOOTSTRAP = _gold_fd_bootstrap(INSTALLED_RUNTIME_ROOT)
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTIFIER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TRACEPARENT_RE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01\Z")
PLACEHOLDER_HOST_RE = re.compile(
    r"(?:^|[.-])(?:example|placeholder|invalid|localhost|local|test|demo|dummy)(?:[.-]|$)",
    re.IGNORECASE,
)

CORE_RECEIPT_FLAGS: dict[str, str] = {
    "performance": "--performance-receipt",
    "continuous_ux": "--continuous-ux-receipt",
    "live_mobile": "--live-mobile-receipt",
    "accessibility": "--accessibility-receipt",
    "failure_states": "--failure-state-receipt",
    "activation_to_value": "--activation-to-value-receipt",
    "public_smoke": "--public-smoke-receipt",
    "authenticated_smoke": "--authenticated-smoke-receipt",
    "tour_control": "--tour-control-receipt",
    "billing": "--billing-receipt",
    "tour_provider_ownership": "--tour-provider-ownership-receipt",
    "whole_project_scope": "--whole-project-scope-receipt",
    "security_posture": "--security-posture-receipt",
    "release_hygiene": "--release-hygiene-receipt",
    "furniture_style_contract": "--furniture-style-contract-receipt",
    "bts_methodology_contract": "--bts-methodology-contract-receipt",
    "tour_delivery_contract": "--tour-delivery-contract-receipt",
    "map_preview_flagship": "--map-preview-flagship-receipt",
    "browser_3d_gate": "--browser-3d-gate-receipt",
    "runtime_reconstruction": "--runtime-reconstruction-receipt",
    "service_generated_reconstruction": "--service-generated-reconstruction-receipt",
    "id_austria": "--id-austria-receipt",
    "repair_canary": "--repair-canary-receipt",
    "provider_catalog": "--provider-catalog-receipt",
    "provider_matrix": "--provider-matrix-receipt",
    "evidence_overlay": "--evidence-overlay-receipt",
    "rybbit_evidence": "--rybbit-evidence-receipt",
    "global_market_envelope": "--global-market-envelope-receipt",
    "incident_support": "--incident-support-receipt",
    "global_experience": "--global-experience-receipt",
    "jurisdiction_privacy_rights": "--jurisdiction-privacy-rights-receipt",
}

GLOBAL_GOVERNANCE_RECEIPT_KEYS = (
    "global_market_envelope",
    "incident_support",
    "global_experience",
    "jurisdiction_privacy_rights",
)

RAW_OBSERVABILITY_FLAGS: dict[str, str] = {
    "slo_metrics_snapshot": "--slo-metrics-snapshot",
    "slo_metrics_probe": "--slo-metrics-probe",
    "monitoring_runtime_receipt": "--monitoring-runtime-receipt",
    "prometheus_range_receipt": "--prometheus-range-receipt",
    "prometheus_range_response": "--prometheus-range-response",
    "alert_delivery_receipt": "--alert-delivery-receipt",
}

RAW_OBSERVABILITY_COMPANION_BUNDLES = (
    "slo_metrics_snapshot",
    "slo_metrics_probe",
)
SLO_SNAPSHOT_BUNDLE_SCHEMA = "propertyquarry.metrics_snapshot_bundle.v2"
SLO_PROBE_BUNDLE_SCHEMA = "propertyquarry.metrics_probe_bundle.v2"
SLO_CAPTURE_TOOL = "propertyquarry.slo_metrics_capture.v2"
COMPANION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
SLO_SNAPSHOT_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "capture_tool",
        "release_commit_sha",
        "release_image_digest",
        "window_start",
        "window_end",
        "window_seconds",
        "replica_count",
        "replicas",
        "payload_sha256",
    }
)
SLO_SNAPSHOT_REPLICA_KEYS = frozenset(
    {
        "container_id",
        "container_image_id",
        "replica_id",
        "release_commit_sha",
        "release_image_digest",
        "docker_inspect_sha256",
        "start",
        "end",
    }
)
SLO_SNAPSHOT_REFERENCE_KEYS = frozenset(
    {"captured_at", "path", "sha256", "bytes"}
)
SLO_PROBE_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "capture_tool",
        "captured_at",
        "release_commit_sha",
        "release_image_digest",
        "replica_count",
        "snapshot_bundle_sha256",
        "snapshot_bundle_bytes",
        "replicas",
        "credential_persisted",
        "payload_sha256",
    }
)
SLO_PROBE_REPLICA_KEYS = frozenset(
    {"replica_id", "container_id", "path", "sha256", "bytes"}
)

PRODUCT_DATA_FLAGS: dict[str, str] = {
    "public_origin": "--expected-public-origin",
    "teable_origin": "--expected-teable-origin",
    "teable_base_id_sha256": "--expected-teable-base-id-sha256",
    "rybbit_origin": "--expected-rybbit-origin",
    "rybbit_site_id_sha256": "--expected-rybbit-site-id-sha256",
    "evidence_overlay_phase": "--expected-evidence-overlay-phase",
}

INSTALLED_RUNTIME_ARTIFACT_PATHS: dict[str, str] = {
    "entrypoint": str(INSTALLED_ENTRYPOINT),
    "python": str(INSTALLED_PYTHON_PATH),
    "gold": str(INSTALLED_RUNTIME_ROOT / "scripts/propertyquarry_gold_status.py"),
    "evidence_contract": str(
        INSTALLED_RUNTIME_ROOT / "scripts/propertyquarry_evidence_contract.py"
    ),
    "preflight_policy": str(
        INSTALLED_RUNTIME_ROOT / "scripts/propertyquarry_release_preflight_policy.py"
    ),
    "flagship_operations_policy": str(
        INSTALLED_RUNTIME_ROOT
        / "config/monitoring/propertyquarry_flagship_operations.v1.json"
    ),
    "bundle_manifest": str(INSTALLED_BUNDLE_MANIFEST_PATH),
}
MANDATORY_MONITORING_BUNDLE_RELATIVE_PATHS = {
    "runtime/config/monitoring/propertyquarry_alert_rule_tests.v1.yml",
    "runtime/config/monitoring/propertyquarry_alert_rules.v1.yml",
    "runtime/config/monitoring/propertyquarry_alertmanager.v1.yml",
    "runtime/config/monitoring/propertyquarry_flagship_operations.v1.json",
    "runtime/config/monitoring/propertyquarry_global_experience.v1.json",
    "runtime/config/monitoring/propertyquarry_incident_support.v1.json",
    "runtime/config/monitoring/propertyquarry_monitoring_tools.v1.json",
    "runtime/config/monitoring/propertyquarry_monitoring_topology.v1.json",
    "runtime/config/monitoring/propertyquarry_prometheus.v1.yml",
    "runtime/config/monitoring/propertyquarry_slo.v1.json",
    "runtime/config/monitoring/propertyquarry_targets.v1.example.json",
}
MANDATORY_BUNDLE_RELATIVE_PATHS = {
    "propertyquarry-global-launch-terminal",
    "runtime/schema/propertyquarry-production-capacity-receipt.v2.schema.json",
    (
        "runtime/config/compliance/"
        "propertyquarry_jurisdiction_privacy_rights.v1.json"
    ),
    "runtime/docs/propertyquarry_global_market_envelope.v1.json",
    "runtime/scripts/propertyquarry_gold_status.py",
    "runtime/scripts/propertyquarry_evidence_contract.py",
    "runtime/scripts/propertyquarry_release_preflight_policy.py",
    "runtime/docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json",
    *MANDATORY_MONITORING_BUNDLE_RELATIVE_PATHS,
}

_GOLD_ARGV_CONTRACT = {
    "profile": "launch",
    "claim_scope": "core",
    "required_browser_engines": ["chromium", "firefox", "webkit"],
    "installed_python_flags": list(INSTALLED_PYTHON_FLAGS),
    "fd_bootstrap_sha256": "sha256:"
    + hashlib.sha256(GOLD_FD_BOOTSTRAP.encode("utf-8")).hexdigest(),
    "receipt_flags": CORE_RECEIPT_FLAGS,
    "raw_observability_flags": RAW_OBSERVABILITY_FLAGS,
    "product_data_flags": PRODUCT_DATA_FLAGS,
    "release_binding_flags": {
        "commit_sha": "--expected-release-sha",
        "image_digest": "--expected-image-digest",
        "deployment_id": "--expected-release-deployment-id",
        "manifest_sha256": "--expected-release-manifest-sha256",
        "performance_chromium_executable_path": (
            "--expected-performance-chromium-executable-path"
        ),
        "performance_chromium_executable_sha256": (
            "--expected-performance-chromium-executable-sha256"
        ),
    },
    "performance_browser_policy_binding": {
        "source": "controller_attested_invocation_contract",
        "engine": PERFORMANCE_BROWSER_POLICY_ENGINE,
        "executable_sha256_format": "sha256_prefixed",
    },
    "fixed_tail_flags": [
        "--launch-evidence-dir",
        "--write",
        "--require-launch-evidence",
        "--fail-on-blocked",
    ],
}
GOLD_ARGV_CONTRACT_SHA256 = "sha256:" + hashlib.sha256(
    json.dumps(
        _GOLD_ARGV_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()

TERMINAL_AUTHORITY_KEYS = (
    "release_preflight",
    "disaster_recovery",
    "capacity",
    "observability_operations",
    "controller_attestation",
)
CAPACITY_RESOURCE_KINDS = (
    "api",
    "database",
    "queue",
    "scheduler",
    "browser_workers",
    "render_workers",
    "provider_quotas",
    "memory",
    "cpu",
    "pids",
    "disk",
    "network",
)
CAPACITY_RESOURCE_UNITS = {
    "api": "requests_per_second",
    "database": "active_connections",
    "queue": "queued_jobs",
    "scheduler": "jobs_per_minute",
    "browser_workers": "concurrent_workers",
    "render_workers": "concurrent_workers",
    "provider_quotas": "requests_per_quota_window",
    "memory": "mebibytes",
    "cpu": "millicores",
    "pids": "processes",
    "disk": "mebibytes_per_second",
    "network": "kilobits_per_second",
}
CAPACITY_RECEIPT_CONTRACT_BUNDLE_RELATIVE_PATH = (
    "runtime/schema/propertyquarry-production-capacity-receipt.v2.schema.json"
)
CAPACITY_RECEIPT_CONTRACT_PATH = (
    ROOT / "schema/propertyquarry-production-capacity-receipt.v2.schema.json"
    if BUNDLED_RUNTIME_LAYOUT
    else ROOT
    / "packaging/propertyquarry-global-launch-terminal/"
    "propertyquarry-production-capacity-receipt.v2.schema.json"
)
CAPACITY_MAXIMUM_AGE_SECONDS = 900
CAPACITY_MAXIMUM_FUTURE_SKEW_SECONDS = 30
CAPACITY_MINIMUM_WINDOW_SECONDS = 300
CAPACITY_MAXIMUM_WINDOW_SECONDS = 86_400
CAPACITY_MAXIMUM_OBSERVATION_LAG_SECONDS = 300
CAPACITY_MINIMUM_SAMPLE_COUNT = 2
CAPACITY_MINIMUM_HEADROOM_BASIS_POINTS = 2_500
CAPACITY_MAXIMUM_RECOVERY_SECONDS = 3_600
CAPACITY_MAXIMUM_INTEGER = (1 << 63) - 1
OBSERVABILITY_TRACE_BOUNDARIES = ("api", "search", "provider", "render")
OBSERVABILITY_DASHBOARD_SCOPES = ("core_slos", "queues", "providers")
OBSERVABILITY_RUNBOOK_OPERATIONS = (
    "incident_response",
    "alert_triage",
    "queue_recovery",
    "provider_degradation",
)
OBSERVABILITY_LIVE_RECEIPT_SCHEMAS = {
    "dashboard_render": "propertyquarry.dashboard_render_receipt.v1",
    "structured_log_query": "propertyquarry.structured_log_query_receipt.v1",
    "distributed_trace_query": "propertyquarry.distributed_trace_query_receipt.v1",
    "alert_delivery": "propertyquarry.alert_delivery_receipt.v1",
}
OBSERVABILITY_MAXIMUM_AGE_SECONDS = 900
OBSERVABILITY_MAXIMUM_FUTURE_SKEW_SECONDS = 30
FLAGSHIP_OPERATIONS_POLICY_PATH = (
    ROOT / "config/monitoring/propertyquarry_flagship_operations.v1.json"
)


class TerminalManifestError(ValueError):
    """A deterministic rejection that never contains manifest data."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


def _reject(code: str, field: str) -> NoReturn:
    raise TerminalManifestError(code, field)


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def _strict_json(raw: bytes, *, field: str) -> dict[str, object]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        ValueError,
        RecursionError,
    ):
        _reject("json_invalid", field)
    if type(payload) is not dict:
        _reject("json_object_required", field)
    try:
        json.dumps(payload, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject("json_invalid", field)
    return payload


def _closed(value: object, keys: set[str] | frozenset[str], *, field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(keys):
        _reject("closed_schema_mismatch", field)
    return value


def _path_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        _reject("path_invalid", field)
    if any(ord(character) < 32 for character in value):
        _reject("path_invalid", field)
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        _reject("path_not_canonical_absolute", field)
    if os.path.realpath(value) != value:
        _reject("path_symlink_rejected", field)
    return value


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@dataclass(frozen=True)
class StableArtifact:
    path: str
    raw: bytes
    sha256: str
    payload: dict[str, object] | None


def _stable_file(
    value: object,
    *,
    field: str,
    maximum: int,
    private: bool = False,
) -> tuple[str, bytes]:
    path = _path_text(value, field=field)
    descriptor = -1
    try:
        before_path = os.lstat(path)
        if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
            _reject("file_type_invalid", field)
        mode = stat.S_IMODE(before_path.st_mode)
        allowed_owners = {0, os.geteuid()}
        if (
            before_path.st_nlink != 1
            or before_path.st_uid not in allowed_owners
            or not 0 < before_path.st_size <= maximum
            or mode & 0o022
            or (private and mode not in {0o400, 0o600})
        ):
            _reject("file_metadata_unsafe", field)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(before_path):
            _reject("file_changed", field)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        after_path = os.lstat(path)
        if (
            len(raw) != before.st_size
            or _file_identity(after) != _file_identity(before)
            or _file_identity(after_path) != _file_identity(before)
        ):
            _reject("file_changed", field)
        return path, raw
    except TerminalManifestError:
        raise
    except OSError:
        _reject("file_unavailable", field)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _release_identity(value: object, *, field: str) -> dict[str, str]:
    identity = _closed(value, {"commit_sha", "image_digest"}, field=field)
    commit_sha = identity["commit_sha"]
    image_digest = identity["image_digest"]
    if (
        type(commit_sha) is not str
        or not GIT_SHA_RE.fullmatch(commit_sha)
        or len(set(commit_sha)) == 1
    ):
        _reject("release_commit_invalid", f"{field}.commit_sha")
    if (
        type(image_digest) is not str
        or not IMAGE_DIGEST_RE.fullmatch(image_digest)
        or len(set(image_digest.removeprefix("sha256:"))) == 1
    ):
        _reject("release_image_invalid", f"{field}.image_digest")
    return {"commit_sha": commit_sha, "image_digest": image_digest}


def _sha256(value: object, *, field: str, prefixed: bool = True) -> str:
    pattern = SHA256_RE if prefixed else IDENTIFIER_SHA256_RE
    if type(value) is not str or not pattern.fullmatch(value):
        _reject("sha256_invalid", field)
    digest = value.removeprefix("sha256:")
    if len(set(digest)) == 1:
        _reject("sha256_placeholder", field)
    return value


def _supported_public_hostname(hostname: object) -> bool:
    if type(hostname) is not str or not hostname or len(hostname) > 253:
        return False
    labels = hostname.split(".")
    return (
        hostname == hostname.lower()
        and hostname == hostname.rstrip(".")
        and len(labels) >= 2
        and labels[-1] in SUPPORTED_GLOBAL_LAUNCH_ORIGIN_TLDS
        and not any(
            ".".join(labels[-suffix_length:])
            in SPECIAL_USE_OR_PRIVATE_DNS_SUFFIXES
            for suffix_length in range(1, len(labels) + 1)
        )
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    )


def _origin(value: object, *, field: str) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or len(value) > 2048
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        _reject("origin_invalid", field)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        _reject("origin_invalid", field)
    hostname = str(parsed.hostname or "")
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
        or parsed.netloc != hostname
        or value != f"https://{hostname}"
        or is_ip_literal
        or not _supported_public_hostname(hostname)
        or PLACEHOLDER_HOST_RE.search(hostname)
    ):
        _reject("origin_invalid_or_placeholder", field)
    return value


def _utc_timestamp(value: object, *, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        _reject("timestamp_invalid", field)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _reject("timestamp_invalid", field)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _reject("timestamp_invalid", field)
    return parsed.astimezone(timezone.utc)


def _declared_identity_mismatches(
    payload: Mapping[str, object],
    expected: Mapping[str, str],
) -> bool:
    candidates: list[Mapping[str, object]] = [payload]
    for key in ("release_identity", "release", "candidate"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    commit_keys = (
        "commit_sha",
        "git_commit_sha",
        "release_commit_sha",
        "release_sha",
        "candidate_sha",
    )
    image_keys = (
        "image_digest",
        "release_image_digest",
        "candidate_image_digest",
    )
    for candidate in candidates:
        for key in commit_keys:
            observed = candidate.get(key)
            if observed not in (None, "") and str(observed).strip().lower() != expected["commit_sha"]:
                return True
        for key in image_keys:
            observed = candidate.get(key)
            if observed not in (None, "") and str(observed).strip().lower() != expected["image_digest"]:
                return True
    return False


def _artifact(
    value: object,
    *,
    field: str,
    release_identity: Mapping[str, str],
    json_required: bool,
) -> StableArtifact:
    descriptor = _closed(
        value,
        {"path", "sha256", "release_identity"},
        field=field,
    )
    artifact_identity = _release_identity(
        descriptor["release_identity"],
        field=f"{field}.release_identity",
    )
    if artifact_identity != dict(release_identity):
        _reject("artifact_release_identity_mismatch", f"{field}.release_identity")
    expected_sha256 = _sha256(descriptor["sha256"], field=f"{field}.sha256")
    path, raw = _stable_file(
        descriptor["path"],
        field=f"{field}.path",
        maximum=(MAX_JSON_ARTIFACT_BYTES if json_required else MAX_RAW_ARTIFACT_BYTES),
    )
    actual_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        _reject("artifact_digest_mismatch", f"{field}.sha256")
    payload = _strict_json(raw, field=f"{field}.payload") if json_required else None
    if payload is not None and _declared_identity_mismatches(payload, release_identity):
        _reject("receipt_release_identity_mismatch", f"{field}.payload")
    return StableArtifact(path=path, raw=raw, sha256=actual_sha256, payload=payload)


def _canonical_payload_sha256(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _companion_reference(
    value: object,
    *,
    keys: frozenset[str],
    field: str,
) -> tuple[str, str, int]:
    reference = _closed(value, keys, field=field)
    name = reference.get("path")
    if type(name) is not str or not COMPANION_NAME_RE.fullmatch(name):
        _reject("companion_path_invalid", f"{field}.path")
    sha256 = _sha256(
        reference.get("sha256"),
        field=f"{field}.sha256",
        prefixed=False,
    )
    byte_count = reference.get("bytes")
    if (
        type(byte_count) is not int
        or byte_count <= 0
        or byte_count > MAX_RAW_ARTIFACT_BYTES
    ):
        _reject("companion_size_invalid", f"{field}.bytes")
    return name, sha256, byte_count


def _raw_observability_companion_inventory(
    raw_observability: Mapping[str, StableArtifact],
    *,
    release_identity: Mapping[str, str],
) -> dict[str, dict[str, tuple[str, int]]]:
    snapshot_field = "raw_observability.slo_metrics_snapshot.payload"
    snapshot_artifact = raw_observability["slo_metrics_snapshot"]
    snapshot = _closed(
        _strict_json(snapshot_artifact.raw, field=snapshot_field),
        SLO_SNAPSHOT_BUNDLE_KEYS,
        field=snapshot_field,
    )
    if (
        snapshot.get("schema") != SLO_SNAPSHOT_BUNDLE_SCHEMA
        or snapshot.get("capture_tool") != SLO_CAPTURE_TOOL
        or snapshot.get("release_commit_sha") != release_identity["commit_sha"]
        or snapshot.get("release_image_digest") != release_identity["image_digest"]
        or _sha256(
            snapshot.get("payload_sha256"),
            field=f"{snapshot_field}.payload_sha256",
            prefixed=False,
        )
        != _canonical_payload_sha256(snapshot)
    ):
        _reject("raw_observability_bundle_invalid", snapshot_field)
    snapshot_rows = snapshot.get("replicas")
    snapshot_count = snapshot.get("replica_count")
    if (
        type(snapshot_rows) is not list
        or type(snapshot_count) is not int
        or not 1 <= snapshot_count <= MAX_SLO_REPLICAS
        or len(snapshot_rows) != snapshot_count
    ):
        _reject("raw_observability_replica_inventory_invalid", snapshot_field)

    snapshot_inventory: dict[str, tuple[str, int]] = {}
    for index, value in enumerate(snapshot_rows):
        row_field = f"{snapshot_field}.replicas[{index}]"
        row = _closed(value, SLO_SNAPSHOT_REPLICA_KEYS, field=row_field)
        for phase in ("start", "end"):
            name, sha256, byte_count = _companion_reference(
                row.get(phase),
                keys=SLO_SNAPSHOT_REFERENCE_KEYS,
                field=f"{row_field}.{phase}",
            )
            if name in snapshot_inventory:
                _reject("companion_name_collision", f"{row_field}.{phase}.path")
            snapshot_inventory[name] = (sha256, byte_count)

    probe_field = "raw_observability.slo_metrics_probe.payload"
    probe_artifact = raw_observability["slo_metrics_probe"]
    probe = _closed(
        _strict_json(probe_artifact.raw, field=probe_field),
        SLO_PROBE_BUNDLE_KEYS,
        field=probe_field,
    )
    if (
        probe.get("schema") != SLO_PROBE_BUNDLE_SCHEMA
        or probe.get("capture_tool") != SLO_CAPTURE_TOOL
        or probe.get("release_commit_sha") != release_identity["commit_sha"]
        or probe.get("release_image_digest") != release_identity["image_digest"]
        or probe.get("credential_persisted") is not False
        or _sha256(
            probe.get("payload_sha256"),
            field=f"{probe_field}.payload_sha256",
            prefixed=False,
        )
        != _canonical_payload_sha256(probe)
        or _sha256(
            probe.get("snapshot_bundle_sha256"),
            field=f"{probe_field}.snapshot_bundle_sha256",
            prefixed=False,
        )
        != snapshot_artifact.sha256.removeprefix("sha256:")
        or probe.get("snapshot_bundle_bytes") != len(snapshot_artifact.raw)
    ):
        _reject("raw_observability_bundle_invalid", probe_field)
    probe_rows = probe.get("replicas")
    probe_count = probe.get("replica_count")
    if (
        type(probe_rows) is not list
        or type(probe_count) is not int
        or not 1 <= probe_count <= MAX_SLO_REPLICAS
        or len(probe_rows) != probe_count
        or probe_count != snapshot_count
    ):
        _reject("raw_observability_replica_inventory_invalid", probe_field)

    probe_inventory: dict[str, tuple[str, int]] = {}
    for index, value in enumerate(probe_rows):
        row_field = f"{probe_field}.replicas[{index}]"
        name, sha256, byte_count = _companion_reference(
            value,
            keys=SLO_PROBE_REPLICA_KEYS,
            field=row_field,
        )
        if name in probe_inventory or name in snapshot_inventory:
            _reject("companion_name_collision", f"{row_field}.path")
        probe_inventory[name] = (sha256, byte_count)

    return {
        "slo_metrics_snapshot": snapshot_inventory,
        "slo_metrics_probe": probe_inventory,
    }


def _capture_raw_observability_companions(
    raw_observability: Mapping[str, StableArtifact],
    *,
    release_identity: Mapping[str, str],
    existing_artifact_bytes: int,
) -> dict[str, dict[str, StableArtifact]]:
    inventories = _raw_observability_companion_inventory(
        raw_observability,
        release_identity=release_identity,
    )
    reserved_names = {
        *(f"receipts--{name}.artifact" for name in CORE_RECEIPT_FLAGS),
        *(f"raw_observability--{name}.artifact" for name in RAW_OBSERVABILITY_FLAGS),
        *(f"terminal_authority--{name}.artifact" for name in TERMINAL_AUTHORITY_KEYS),
    }
    captured: dict[str, dict[str, StableArtifact]] = {}
    total_bytes = existing_artifact_bytes
    for bundle_name in RAW_OBSERVABILITY_COMPANION_BUNDLES:
        field = f"raw_observability.{bundle_name}.companions"
        inventory = inventories[bundle_name]
        bundle_artifacts: dict[str, StableArtifact] = {}
        for name, (referenced_sha256, referenced_bytes) in sorted(inventory.items()):
            descriptor_field = f"{field}.{name}"
            if name in reserved_names:
                _reject("companion_name_collision", descriptor_field)
            expected_path = os.path.join(
                os.path.dirname(raw_observability[bundle_name].path),
                name,
            )
            if total_bytes + referenced_bytes > MAX_TOTAL_ARTIFACT_BYTES:
                _reject("artifact_set_too_large", "manifest")
            path, raw = _stable_file(
                expected_path,
                field=f"{descriptor_field}.path",
                maximum=MAX_RAW_ARTIFACT_BYTES,
            )
            actual_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
            if len(raw) != referenced_bytes:
                _reject("companion_size_mismatch", f"{descriptor_field}.bytes")
            if actual_sha256.removeprefix("sha256:") != referenced_sha256:
                _reject("companion_digest_mismatch", f"{descriptor_field}.sha256")
            total_bytes += len(raw)
            bundle_artifacts[name] = StableArtifact(
                path=path,
                raw=raw,
                sha256=actual_sha256,
                payload=None,
            )
        captured[bundle_name] = bundle_artifacts
    return captured


def _new_output_path(value: object, *, field: str) -> str:
    path = _path_text(value, field=field)
    if os.path.lexists(path):
        _reject("output_already_exists", field)
    parent = os.path.dirname(path)
    descriptor = -1
    try:
        descriptor = _open_secure_directory_chain(
            parent,
            field=f"{field}.parent",
            root_only=False,
        )
    except TerminalManifestError:
        raise
    except OSError:
        _reject("output_parent_unavailable", field)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _open_secure_directory_chain(
    path: str,
    *,
    field: str,
    root_only: bool,
) -> int:
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        _reject("directory_path_invalid", field)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in PurePosixPath(path).parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            sticky_root_directory = (
                metadata.st_uid == 0
                and bool(metadata.st_mode & stat.S_ISVTX)
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or (metadata.st_uid != 0 if root_only else metadata.st_uid not in {0, os.geteuid()})
                or (mode & 0o022 and not (not root_only and sticky_root_directory))
            ):
                _reject("directory_chain_unsafe", field)
        return descriptor
    except TerminalManifestError:
        raise
    except OSError:
        _reject("directory_chain_unavailable", field)
    finally:
        if sys.exc_info()[0] is not None and descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class LaunchManifest:
    release_identity: dict[str, str]
    product_data: dict[str, str]
    receipts: dict[str, StableArtifact]
    raw_observability: dict[str, StableArtifact]
    raw_observability_companions: dict[str, dict[str, StableArtifact]]
    terminal_authority: dict[str, StableArtifact | None]
    missing_authority: tuple[str, ...]
    outputs: dict[str, str]
    flagship_operations_sha256: str
    invocation_contract: dict[str, object]

    def attested_artifact_digests(self) -> dict[str, str]:
        values = {
            **{
                f"receipts.{name}": artifact.sha256
                for name, artifact in self.receipts.items()
            },
            **{
                f"raw_observability.{name}": artifact.sha256
                for name, artifact in self.raw_observability.items()
            },
            **{
                f"raw_observability.{bundle_name}.companions.{name}": artifact.sha256
                for bundle_name, artifacts in self.raw_observability_companions.items()
                for name, artifact in artifacts.items()
            },
            **{
                f"terminal_authority.{name}": artifact.sha256
                for name, artifact in self.terminal_authority.items()
                if name != "controller_attestation" and artifact is not None
            },
        }
        return dict(sorted(values.items()))


def _validate_performance_browser_policy(
    value: object,
    *,
    field: str,
) -> dict[str, str]:
    policy = _closed(
        value,
        PERFORMANCE_BROWSER_POLICY_KEYS,
        field=field,
    )
    if policy.get("engine") != PERFORMANCE_BROWSER_POLICY_ENGINE:
        _reject("invocation_contract_mismatch", field)
    return {
        "engine": PERFORMANCE_BROWSER_POLICY_ENGINE,
        "executable_path": _path_text(
            policy.get("executable_path"),
            field=f"{field}.executable_path",
        ),
        "executable_sha256": _sha256(
            policy.get("executable_sha256"),
            field=f"{field}.executable_sha256",
        ),
    }


def _validate_invocation_contract(
    value: object,
    *,
    outputs: Mapping[str, str],
    flagship_operations_sha256: str,
) -> dict[str, object]:
    field = "invocation_contract"
    contract = _closed(
        value,
        {
            "schema",
            "terminal_command",
            "profile",
            "claim_scope",
            "required_browser_engines",
            "performance_browser_policy",
            "release_manifest_sha256",
            "output_paths",
            "gold_argv_contract_sha256",
            "runtime_artifacts",
            "runtime_artifact_set_sha256",
        },
        field=field,
    )
    performance_browser_policy = _validate_performance_browser_policy(
        contract.get("performance_browser_policy"),
        field=f"{field}.performance_browser_policy",
    )
    release_manifest_sha256 = _sha256(
        contract.get("release_manifest_sha256"),
        field=f"{field}.release_manifest_sha256",
    )
    output_paths = _closed(
        contract.get("output_paths"),
        set(outputs),
        field=f"{field}.output_paths",
    )
    runtime_values = _closed(
        contract.get("runtime_artifacts"),
        set(INSTALLED_RUNTIME_ARTIFACT_PATHS),
        field=f"{field}.runtime_artifacts",
    )
    runtime_artifacts: dict[str, dict[str, str]] = {}
    for name, expected_path in INSTALLED_RUNTIME_ARTIFACT_PATHS.items():
        descriptor = _closed(
            runtime_values[name],
            {"path", "sha256"},
            field=f"{field}.runtime_artifacts.{name}",
        )
        if descriptor.get("path") != expected_path:
            _reject("installed_artifact_path_mismatch", f"{field}.runtime_artifacts.{name}")
        runtime_artifacts[name] = {
            "path": expected_path,
            "sha256": _sha256(
                descriptor.get("sha256"),
                field=f"{field}.runtime_artifacts.{name}.sha256",
            ),
        }
    artifact_set_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            runtime_artifacts,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        contract.get("schema") != INVOCATION_CONTRACT_SCHEMA
        or contract.get("terminal_command") != GLOBAL_LAUNCH_TERMINAL_COMMAND
        or contract.get("profile") != "launch"
        or contract.get("claim_scope") != "core"
        or contract.get("required_browser_engines")
        != ["chromium", "firefox", "webkit"]
        or output_paths != dict(outputs)
        or contract.get("gold_argv_contract_sha256")
        != GOLD_ARGV_CONTRACT_SHA256
        or contract.get("runtime_artifact_set_sha256") != artifact_set_sha256
        or runtime_artifacts["flagship_operations_policy"]["sha256"]
        != f"sha256:{flagship_operations_sha256}"
    ):
        _reject("invocation_contract_mismatch", field)
    return {
        "schema": INVOCATION_CONTRACT_SCHEMA,
        "terminal_command": GLOBAL_LAUNCH_TERMINAL_COMMAND,
        "profile": "launch",
        "claim_scope": "core",
        "required_browser_engines": ["chromium", "firefox", "webkit"],
        "performance_browser_policy": performance_browser_policy,
        "release_manifest_sha256": release_manifest_sha256,
        "output_paths": dict(outputs),
        "gold_argv_contract_sha256": GOLD_ARGV_CONTRACT_SHA256,
        "runtime_artifacts": runtime_artifacts,
        "runtime_artifact_set_sha256": artifact_set_sha256,
    }


def _validate_preflight(payload: Mapping[str, object], identity: Mapping[str, str]) -> None:
    _closed(
        payload,
        {
            "schema",
            "status",
            "disposition",
            "required_check_set_digest",
            "passed_checks",
            "failed_checks",
            "indeterminate_checks",
            "release_identity",
        },
        field="terminal_authority.release_preflight",
    )
    if (
        payload.get("schema") != PREFLIGHT_DECISION_SCHEMA
        or payload.get("status") != "pass"
        or payload.get("disposition") != preflight_policy.READY
        or payload.get("required_check_set_digest")
        != preflight_policy.REQUIRED_CHECK_SET_DIGEST
        or payload.get("passed_checks") != list(preflight_policy.REQUIRED_CHECK_IDS)
        or payload.get("failed_checks") != []
        or payload.get("indeterminate_checks") != []
    ):
        _reject("release_preflight_not_ready", "terminal_authority.release_preflight")
    embedded = _release_identity(
        payload.get("release_identity"),
        field="terminal_authority.release_preflight.payload.release_identity",
    )
    if embedded != dict(identity):
        _reject("receipt_release_identity_mismatch", "terminal_authority.release_preflight")


def _validate_disaster_recovery(payload: Mapping[str, object], identity: Mapping[str, str]) -> None:
    release = payload.get("release")
    if not isinstance(release, Mapping):
        _reject("disaster_recovery_not_ready", "terminal_authority.disaster_recovery")
    observed = {
        "commit_sha": str(release.get("git_commit_sha") or "").strip().lower(),
        "image_digest": str(release.get("image_digest") or "").strip().lower(),
    }
    verification = payload.get("verification")
    required_checks = (
        "release_identity_exact",
        "encrypted_backup",
        "off_host_object_exact",
        "off_host_retrieval_exact",
        "disposable_restore",
        "rpo_met",
        "rto_met",
        "critical_data_exact_match",
        "evidence_fresh",
    )
    if (
        payload.get("schema") != DISASTER_RECOVERY_RECEIPT_SCHEMA
        or payload.get("operation") != "release_gate"
        or payload.get("status") != "pass"
        or observed != dict(identity)
        or not isinstance(verification, Mapping)
        or any(verification.get(check) is not True for check in required_checks)
    ):
        _reject("disaster_recovery_not_ready", "terminal_authority.disaster_recovery")


def _capacity_integer(
    value: object,
    *,
    field: str,
    minimum: int = 0,
    maximum: int = CAPACITY_MAXIMUM_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject("capacity_numeric_evidence_invalid", field)
    return value


def _capacity_window(
    value: object,
    *,
    field: str,
    with_samples: bool,
) -> tuple[datetime, datetime, int, int | None]:
    keys = {"started_at", "ended_at", "duration_seconds"}
    if with_samples:
        keys.add("sample_count")
    window = _closed(value, keys, field=field)
    started_at = _utc_timestamp(window.get("started_at"), field=f"{field}.started_at")
    ended_at = _utc_timestamp(window.get("ended_at"), field=f"{field}.ended_at")
    duration_seconds = _capacity_integer(
        window.get("duration_seconds"),
        field=f"{field}.duration_seconds",
        minimum=CAPACITY_MINIMUM_WINDOW_SECONDS,
        maximum=CAPACITY_MAXIMUM_WINDOW_SECONDS,
    )
    elapsed_seconds = (ended_at - started_at).total_seconds()
    if elapsed_seconds != duration_seconds:
        _reject("capacity_numeric_evidence_invalid", field)
    sample_count: int | None = None
    if with_samples:
        sample_count = _capacity_integer(
            window.get("sample_count"),
            field=f"{field}.sample_count",
            minimum=CAPACITY_MINIMUM_SAMPLE_COUNT,
        )
    return started_at, ended_at, duration_seconds, sample_count


def _validate_capacity(
    payload: Mapping[str, object],
    identity: Mapping[str, str],
    *,
    now: datetime,
    contract_sha256: str,
) -> None:
    field = "terminal_authority.capacity"
    _closed(
        payload,
        {
            "schema",
            "contract_sha256",
            "status",
            "evidence_level",
            "deployment_id",
            "observed_at",
            "release_identity",
            "measurement_window",
            "summary",
            "resources",
        },
        field=field,
    )
    embedded = _release_identity(
        payload.get("release_identity"),
        field=f"{field}.payload.release_identity",
    )
    if (
        payload.get("schema") != CAPACITY_RECEIPT_SCHEMA
        or payload.get("status") != "pass"
        or payload.get("evidence_level") != "protected_production"
        or payload.get("deployment_id") != PRODUCTION_DEPLOYMENT_ID
        or embedded != dict(identity)
    ):
        _reject("capacity_not_ready", field)
    if payload.get("contract_sha256") != contract_sha256:
        _reject("capacity_contract_mismatch", f"{field}.contract_sha256")

    observed_at = _utc_timestamp(
        payload.get("observed_at"), field=f"{field}.observed_at"
    )
    started_at, ended_at, duration_seconds, _sample_count = _capacity_window(
        payload.get("measurement_window"),
        field=f"{field}.measurement_window",
        with_samples=False,
    )
    checked_at = now.astimezone(timezone.utc)
    receipt_age = (checked_at - observed_at).total_seconds()
    window_age = (checked_at - ended_at).total_seconds()
    observation_lag = (observed_at - ended_at).total_seconds()
    if (
        receipt_age > CAPACITY_MAXIMUM_AGE_SECONDS
        or receipt_age < -CAPACITY_MAXIMUM_FUTURE_SKEW_SECONDS
        or window_age > CAPACITY_MAXIMUM_AGE_SECONDS
        or window_age < -CAPACITY_MAXIMUM_FUTURE_SKEW_SECONDS
        or observation_lag < 0
        or observation_lag > CAPACITY_MAXIMUM_OBSERVATION_LAG_SECONDS
    ):
        _reject("capacity_evidence_stale", field)

    rows = payload.get("resources")
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        _reject("capacity_resource_inventory_invalid", f"{field}.resources")
    checked_rows: dict[str, dict[str, object]] = {}
    evidence_digests: set[str] = set()
    sample_counts: list[int] = []
    headroom_values: list[int] = []
    recovery_values: list[int] = []
    for index, value in enumerate(rows):
        row_field = f"{field}.resources[{index}]"
        row = _closed(
            value,
            {
                "resource",
                "unit",
                "window",
                "demand",
                "capacity",
                "limit_test",
                "backpressure_test",
                "telemetry_evidence_sha256",
            },
            field=row_field,
        )
        resource = row.get("resource")
        if (
            type(resource) is not str
            or resource not in CAPACITY_RESOURCE_UNITS
            or resource in checked_rows
            or row.get("unit") != CAPACITY_RESOURCE_UNITS[resource]
        ):
            _reject("capacity_resource_inventory_invalid", row_field)
        checked_rows[resource] = row

        row_start, row_end, row_duration, row_samples = _capacity_window(
            row.get("window"), field=f"{row_field}.window", with_samples=True
        )
        if (
            row_start != started_at
            or row_end != ended_at
            or row_duration != duration_seconds
            or row_samples is None
        ):
            _reject("capacity_numeric_evidence_invalid", f"{row_field}.window")
        sample_counts.append(row_samples)

        demand = _closed(
            row.get("demand"),
            {"observed_peak", "forecast_peak", "required_peak"},
            field=f"{row_field}.demand",
        )
        observed_peak = _capacity_integer(
            demand.get("observed_peak"),
            field=f"{row_field}.demand.observed_peak",
            minimum=1,
        )
        forecast_peak = _capacity_integer(
            demand.get("forecast_peak"),
            field=f"{row_field}.demand.forecast_peak",
            minimum=1,
        )
        required_peak = _capacity_integer(
            demand.get("required_peak"),
            field=f"{row_field}.demand.required_peak",
            minimum=1,
        )
        if required_peak != max(observed_peak, forecast_peak):
            _reject("capacity_numeric_evidence_invalid", f"{row_field}.demand")

        capacity = _closed(
            row.get("capacity"),
            {
                "verified_sustainable_capacity",
                "operational_limit",
                "headroom_absolute",
                "headroom_basis_points",
                "sustainable_capacity_evidence_sha256",
            },
            field=f"{row_field}.capacity",
        )
        sustainable = _capacity_integer(
            capacity.get("verified_sustainable_capacity"),
            field=f"{row_field}.capacity.verified_sustainable_capacity",
            minimum=1,
        )
        operational_limit = _capacity_integer(
            capacity.get("operational_limit"),
            field=f"{row_field}.capacity.operational_limit",
            minimum=1,
        )
        headroom_absolute = _capacity_integer(
            capacity.get("headroom_absolute"),
            field=f"{row_field}.capacity.headroom_absolute",
            minimum=1,
        )
        headroom_basis_points = _capacity_integer(
            capacity.get("headroom_basis_points"),
            field=f"{row_field}.capacity.headroom_basis_points",
            minimum=CAPACITY_MINIMUM_HEADROOM_BASIS_POINTS,
        )
        expected_headroom = operational_limit - required_peak
        expected_basis_points = expected_headroom * 10_000 // required_peak
        if (
            sustainable < operational_limit
            or expected_headroom <= 0
            or headroom_absolute != expected_headroom
            or headroom_basis_points != expected_basis_points
        ):
            _reject("capacity_numeric_evidence_invalid", f"{row_field}.capacity")
        headroom_values.append(headroom_basis_points)

        limit_test = _closed(
            row.get("limit_test"),
            {
                "configured_limit",
                "attempted_over_limit",
                "accepted_over_limit",
                "controlled_over_limit",
                "evidence_sha256",
            },
            field=f"{row_field}.limit_test",
        )
        configured_limit = _capacity_integer(
            limit_test.get("configured_limit"),
            field=f"{row_field}.limit_test.configured_limit",
            minimum=1,
        )
        attempted_over_limit = _capacity_integer(
            limit_test.get("attempted_over_limit"),
            field=f"{row_field}.limit_test.attempted_over_limit",
            minimum=1,
        )
        accepted_over_limit = _capacity_integer(
            limit_test.get("accepted_over_limit"),
            field=f"{row_field}.limit_test.accepted_over_limit",
        )
        controlled_over_limit = _capacity_integer(
            limit_test.get("controlled_over_limit"),
            field=f"{row_field}.limit_test.controlled_over_limit",
            minimum=1,
        )
        if (
            configured_limit != operational_limit
            or accepted_over_limit != 0
            or controlled_over_limit != attempted_over_limit
        ):
            _reject("capacity_numeric_evidence_invalid", f"{row_field}.limit_test")

        backpressure = _closed(
            row.get("backpressure_test"),
            {
                "offered_at_saturation",
                "admitted_at_saturation",
                "deferred",
                "rejected",
                "uncontrolled_failures",
                "accepted_work_lost",
                "recovery_seconds",
                "evidence_sha256",
            },
            field=f"{row_field}.backpressure_test",
        )
        offered = _capacity_integer(
            backpressure.get("offered_at_saturation"),
            field=f"{row_field}.backpressure_test.offered_at_saturation",
            minimum=1,
        )
        admitted = _capacity_integer(
            backpressure.get("admitted_at_saturation"),
            field=f"{row_field}.backpressure_test.admitted_at_saturation",
            minimum=1,
        )
        deferred = _capacity_integer(
            backpressure.get("deferred"),
            field=f"{row_field}.backpressure_test.deferred",
        )
        rejected = _capacity_integer(
            backpressure.get("rejected"),
            field=f"{row_field}.backpressure_test.rejected",
        )
        uncontrolled_failures = _capacity_integer(
            backpressure.get("uncontrolled_failures"),
            field=f"{row_field}.backpressure_test.uncontrolled_failures",
        )
        accepted_work_lost = _capacity_integer(
            backpressure.get("accepted_work_lost"),
            field=f"{row_field}.backpressure_test.accepted_work_lost",
        )
        recovery_seconds = _capacity_integer(
            backpressure.get("recovery_seconds"),
            field=f"{row_field}.backpressure_test.recovery_seconds",
            maximum=CAPACITY_MAXIMUM_RECOVERY_SECONDS,
        )
        controlled_at_saturation = deferred + rejected
        if (
            offered <= operational_limit
            or admitted != operational_limit
            or controlled_at_saturation <= 0
            or controlled_at_saturation != offered - admitted
            or uncontrolled_failures != 0
            or accepted_work_lost != 0
        ):
            _reject(
                "capacity_numeric_evidence_invalid",
                f"{row_field}.backpressure_test",
            )
        recovery_values.append(recovery_seconds)

        for digest_field, digest_value in (
            ("telemetry_evidence_sha256", row.get("telemetry_evidence_sha256")),
            (
                "capacity.sustainable_capacity_evidence_sha256",
                capacity.get("sustainable_capacity_evidence_sha256"),
            ),
            ("limit_test.evidence_sha256", limit_test.get("evidence_sha256")),
            (
                "backpressure_test.evidence_sha256",
                backpressure.get("evidence_sha256"),
            ),
        ):
            checked_digest = _sha256(
                digest_value, field=f"{row_field}.{digest_field}"
            )
            if checked_digest in evidence_digests:
                _reject("capacity_evidence_digest_reused", f"{row_field}.{digest_field}")
            evidence_digests.add(checked_digest)

    if set(checked_rows) != set(CAPACITY_RESOURCE_KINDS):
        _reject("capacity_resource_inventory_invalid", f"{field}.resources")

    summary = _closed(
        payload.get("summary"),
        {
            "resource_count",
            "total_sample_count",
            "minimum_headroom_basis_points",
            "maximum_recovery_seconds",
        },
        field=f"{field}.summary",
    )
    resource_count = _capacity_integer(
        summary.get("resource_count"),
        field=f"{field}.summary.resource_count",
        minimum=1,
    )
    total_sample_count = _capacity_integer(
        summary.get("total_sample_count"),
        field=f"{field}.summary.total_sample_count",
        minimum=1,
    )
    minimum_headroom = _capacity_integer(
        summary.get("minimum_headroom_basis_points"),
        field=f"{field}.summary.minimum_headroom_basis_points",
        minimum=CAPACITY_MINIMUM_HEADROOM_BASIS_POINTS,
    )
    maximum_recovery = _capacity_integer(
        summary.get("maximum_recovery_seconds"),
        field=f"{field}.summary.maximum_recovery_seconds",
        maximum=CAPACITY_MAXIMUM_RECOVERY_SECONDS,
    )
    if (
        resource_count != len(CAPACITY_RESOURCE_KINDS)
        or total_sample_count != sum(sample_counts)
        or minimum_headroom != min(headroom_values)
        or maximum_recovery != max(recovery_values)
    ):
        _reject("capacity_numeric_evidence_invalid", f"{field}.summary")


def _validate_observability_operations(
    payload: Mapping[str, object],
    identity: Mapping[str, str],
    *,
    now: datetime,
    flagship_operations_sha256: str,
) -> None:
    field = "terminal_authority.observability_operations"
    _closed(
        payload,
        {
            "schema",
            "status",
            "observed_at",
            "flagship_operations_sha256",
            "release_identity",
            "log_ingestion",
            "trace_continuity",
            "dashboards",
            "alert_delivery",
            "runbooks",
            "authenticated_live_receipts",
        },
        field=field,
    )
    embedded = _release_identity(
        payload.get("release_identity"),
        field=f"{field}.payload.release_identity",
    )
    observed_at = _utc_timestamp(payload.get("observed_at"), field=f"{field}.observed_at")
    checked_at = now.astimezone(timezone.utc)
    age = (checked_at - observed_at).total_seconds()
    if (
        payload.get("schema") != OBSERVABILITY_OPERATIONS_RECEIPT_SCHEMA
        or payload.get("status") != "pass"
        or embedded != dict(identity)
        or payload.get("flagship_operations_sha256")
        != flagship_operations_sha256
        or age > OBSERVABILITY_MAXIMUM_AGE_SECONDS
        or age < -OBSERVABILITY_MAXIMUM_FUTURE_SKEW_SECONDS
    ):
        _reject("observability_operations_not_ready", field)

    live_rows = payload.get("authenticated_live_receipts")
    if type(live_rows) is not list or any(type(row) is not dict for row in live_rows):
        _reject("observability_live_receipts_not_ready", f"{field}.authenticated_live_receipts")
    live_receipts: dict[str, Mapping[str, object]] = {}
    for index, row in enumerate(live_rows):
        checked = _closed(
            row,
            {
                "kind",
                "schema",
                "captured_at",
                "sha256",
                "authentication_scheme",
                "authentication_key_id",
                "authentication_receipt_sha256",
                "release_filters",
            },
            field=f"{field}.authenticated_live_receipts[{index}]",
        )
        kind = checked.get("kind")
        if type(kind) is not str or kind in live_receipts:
            _reject("observability_live_receipts_not_ready", f"{field}.authenticated_live_receipts")
        live_receipts[kind] = checked
    if set(live_receipts) != set(OBSERVABILITY_LIVE_RECEIPT_SCHEMAS):
        _reject("observability_live_receipts_not_ready", f"{field}.authenticated_live_receipts")
    for kind, row in live_receipts.items():
        captured_at = _utc_timestamp(
            row.get("captured_at"),
            field=f"{field}.authenticated_live_receipts.{kind}.captured_at",
        )
        receipt_age = (checked_at - captured_at).total_seconds()
        key_id = row.get("authentication_key_id")
        release_filters = _closed(
            row.get("release_filters"),
            {"release_commit_sha", "release_image_digest", "replica_id"},
            field=f"{field}.authenticated_live_receipts.{kind}.release_filters",
        )
        if (
            row.get("schema") != OBSERVABILITY_LIVE_RECEIPT_SCHEMAS[kind]
            or row.get("authentication_scheme") != "Ed25519"
            or type(key_id) is not str
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", key_id)
            or receipt_age > OBSERVABILITY_MAXIMUM_AGE_SECONDS
            or receipt_age < -OBSERVABILITY_MAXIMUM_FUTURE_SKEW_SECONDS
            or release_filters.get("release_commit_sha") != identity["commit_sha"]
            or release_filters.get("release_image_digest") != identity["image_digest"]
            or type(release_filters.get("replica_id")) is not str
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                str(release_filters.get("replica_id")),
            )
        ):
            _reject("observability_live_receipts_not_ready", f"{field}.authenticated_live_receipts.{kind}")
        for digest_key in ("sha256", "authentication_receipt_sha256"):
            _sha256(
                row.get(digest_key),
                field=f"{field}.authenticated_live_receipts.{kind}.{digest_key}",
            )

    log_ingestion = _closed(
        payload.get("log_ingestion"),
        {
            "status",
            "correlation_id_sha256",
            "ingestion_receipt_sha256",
            "query_receipt_sha256",
            "matched_record_count",
        },
        field=f"{field}.log_ingestion",
    )
    matched_record_count = log_ingestion.get("matched_record_count")
    if (
        log_ingestion.get("status") != "pass"
        or type(matched_record_count) is not int
        or matched_record_count <= 0
    ):
        _reject("observability_log_query_not_ready", f"{field}.log_ingestion")
    for key in (
        "correlation_id_sha256",
        "ingestion_receipt_sha256",
        "query_receipt_sha256",
    ):
        _sha256(log_ingestion.get(key), field=f"{field}.log_ingestion.{key}")

    trace = _closed(
        payload.get("trace_continuity"),
        {"status", "traceparent", "boundaries", "span_evidence_sha256"},
        field=f"{field}.trace_continuity",
    )
    traceparent = trace.get("traceparent")
    match = TRACEPARENT_RE.fullmatch(traceparent) if type(traceparent) is str else None
    if (
        trace.get("status") != "pass"
        or match is None
        or set(match.group(1)) == {"0"}
        or set(match.group(2)) == {"0"}
        or trace.get("boundaries") != list(OBSERVABILITY_TRACE_BOUNDARIES)
    ):
        _reject("observability_trace_continuity_not_ready", f"{field}.trace_continuity")
    _sha256(
        trace.get("span_evidence_sha256"),
        field=f"{field}.trace_continuity.span_evidence_sha256",
    )

    dashboard_rows = payload.get("dashboards")
    if type(dashboard_rows) is not list or any(type(row) is not dict for row in dashboard_rows):
        _reject("observability_dashboards_not_ready", f"{field}.dashboards")
    dashboards: dict[str, Mapping[str, object]] = {}
    for index, row in enumerate(dashboard_rows):
        checked = _closed(
            row,
            {
                "scope",
                "status",
                "dashboard_id",
                "version_sha256",
                "availability_receipt_sha256",
            },
            field=f"{field}.dashboards[{index}]",
        )
        scope = checked.get("scope")
        if type(scope) is not str or scope in dashboards:
            _reject("observability_dashboards_not_ready", f"{field}.dashboards")
        dashboards[scope] = checked
    if set(dashboards) != set(OBSERVABILITY_DASHBOARD_SCOPES):
        _reject("observability_dashboards_not_ready", f"{field}.dashboards")
    for scope, row in dashboards.items():
        dashboard_id = row.get("dashboard_id")
        if (
            row.get("status") != "pass"
            or type(dashboard_id) is not str
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", dashboard_id)
        ):
            _reject("observability_dashboards_not_ready", f"{field}.dashboards.{scope}")
        for key in ("version_sha256", "availability_receipt_sha256"):
            _sha256(row.get(key), field=f"{field}.dashboards.{scope}.{key}")

    alert = _closed(
        payload.get("alert_delivery"),
        {
            "status",
            "delivered_at",
            "delivery_receipt_sha256",
            "route_config_sha256",
        },
        field=f"{field}.alert_delivery",
    )
    delivered_at = _utc_timestamp(
        alert.get("delivered_at"), field=f"{field}.alert_delivery.delivered_at"
    )
    delivery_age = (checked_at - delivered_at).total_seconds()
    if (
        alert.get("status") != "pass"
        or delivery_age > OBSERVABILITY_MAXIMUM_AGE_SECONDS
        or delivery_age < -OBSERVABILITY_MAXIMUM_FUTURE_SKEW_SECONDS
    ):
        _reject("observability_alert_delivery_not_ready", f"{field}.alert_delivery")
    for key in ("delivery_receipt_sha256", "route_config_sha256"):
        _sha256(alert.get(key), field=f"{field}.alert_delivery.{key}")

    runbook_rows = payload.get("runbooks")
    if type(runbook_rows) is not list or any(type(row) is not dict for row in runbook_rows):
        _reject("observability_runbooks_not_ready", f"{field}.runbooks")
    runbooks: dict[str, Mapping[str, object]] = {}
    for index, row in enumerate(runbook_rows):
        checked = _closed(
            row,
            {"operation", "immutable_uri", "sha256"},
            field=f"{field}.runbooks[{index}]",
        )
        operation = checked.get("operation")
        if type(operation) is not str or operation in runbooks:
            _reject("observability_runbooks_not_ready", f"{field}.runbooks")
        runbooks[operation] = checked
    if set(runbooks) != set(OBSERVABILITY_RUNBOOK_OPERATIONS):
        _reject("observability_runbooks_not_ready", f"{field}.runbooks")
    for operation, row in runbooks.items():
        uri = row.get("immutable_uri")
        try:
            parsed = urlsplit(uri) if type(uri) is str else None
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or identity["commit_sha"] not in parsed.path
            or not _supported_public_hostname(str(parsed.hostname))
            or PLACEHOLDER_HOST_RE.search(str(parsed.hostname))
        ):
            _reject("observability_runbooks_not_ready", f"{field}.runbooks.{operation}")
        _sha256(row.get("sha256"), field=f"{field}.runbooks.{operation}.sha256")
def load_manifest(path: Path, *, now: datetime | None = None) -> LaunchManifest:
    manifest_path, manifest_raw = _stable_file(
        str(path),
        field="manifest",
        maximum=MAX_MANIFEST_BYTES,
        private=True,
    )
    del manifest_path
    root = _strict_json(manifest_raw, field="manifest")
    root = _closed(
        root,
        {
            "schema",
            "version",
            "release_identity",
            "product_data",
            "receipts",
            "raw_observability",
            "terminal_authority",
            "outputs",
            "invocation_contract",
        },
        field="manifest",
    )
    if (
        root["schema"] != MANIFEST_SCHEMA
        or type(root["version"]) is not int
        or root["version"] != 1
    ):
        _reject("manifest_schema_invalid", "manifest")
    identity = _release_identity(root["release_identity"], field="release_identity")
    _policy_path, flagship_operations_raw = _stable_file(
        str(FLAGSHIP_OPERATIONS_POLICY_PATH),
        field="flagship_operations_policy",
        maximum=MAX_JSON_ARTIFACT_BYTES,
    )
    flagship_operations_policy = _strict_json(
        flagship_operations_raw,
        field="flagship_operations_policy",
    )
    if (
        flagship_operations_policy.get("schema_version")
        != "propertyquarry.flagship-operations.v1"
    ):
        _reject("flagship_operations_policy_invalid", "flagship_operations_policy")
    flagship_operations_sha256 = hashlib.sha256(
        flagship_operations_raw
    ).hexdigest()
    _capacity_contract_path, capacity_contract_raw = _stable_file(
        str(CAPACITY_RECEIPT_CONTRACT_PATH),
        field="capacity_receipt_contract",
        maximum=MAX_JSON_ARTIFACT_BYTES,
    )
    capacity_contract = _strict_json(
        capacity_contract_raw,
        field="capacity_receipt_contract",
    )
    capacity_schema_property = capacity_contract.get("properties")
    if (
        capacity_contract.get("$schema")
        != "https://json-schema.org/draft/2020-12/schema"
        or capacity_contract.get("$id")
        != "https://propertyquarry.at/schemas/"
        "propertyquarry-production-capacity-receipt.v2.schema.json"
        or not isinstance(capacity_schema_property, Mapping)
        or not isinstance(capacity_schema_property.get("schema"), Mapping)
        or capacity_schema_property["schema"].get("const")
        != CAPACITY_RECEIPT_SCHEMA
    ):
        _reject("capacity_contract_invalid", "capacity_receipt_contract")
    capacity_contract_sha256 = "sha256:" + hashlib.sha256(
        capacity_contract_raw
    ).hexdigest()

    product = _closed(
        root["product_data"],
        {
            "public_origin",
            "teable_origin",
            "teable_base_id_sha256",
            "rybbit_origin",
            "rybbit_site_id_sha256",
            "evidence_overlay_phase",
        },
        field="product_data",
    )
    product_data = {
        "public_origin": _origin(product["public_origin"], field="product_data.public_origin"),
        "teable_origin": _origin(product["teable_origin"], field="product_data.teable_origin"),
        "teable_base_id_sha256": _sha256(
            product["teable_base_id_sha256"],
            field="product_data.teable_base_id_sha256",
            prefixed=False,
        ),
        "rybbit_origin": _origin(product["rybbit_origin"], field="product_data.rybbit_origin"),
        "rybbit_site_id_sha256": _sha256(
            product["rybbit_site_id_sha256"],
            field="product_data.rybbit_site_id_sha256",
            prefixed=False,
        ),
        "evidence_overlay_phase": str(product["evidence_overlay_phase"]),
    }
    if product_data["evidence_overlay_phase"] not in {"staged", "active"}:
        _reject("evidence_overlay_phase_invalid", "product_data.evidence_overlay_phase")

    receipt_values = _closed(
        root["receipts"], set(CORE_RECEIPT_FLAGS), field="receipts"
    )
    receipts = {
        name: _artifact(
            receipt_values[name],
            field=f"receipts.{name}",
            release_identity=identity,
            json_required=True,
        )
        for name in CORE_RECEIPT_FLAGS
    }

    raw_values = _closed(
        root["raw_observability"],
        set(RAW_OBSERVABILITY_FLAGS),
        field="raw_observability",
    )
    raw_observability = {
        name: _artifact(
            raw_values[name],
            field=f"raw_observability.{name}",
            release_identity=identity,
            json_required=False,
        )
        for name in RAW_OBSERVABILITY_FLAGS
    }

    authority_values = _closed(
        root["terminal_authority"],
        set(TERMINAL_AUTHORITY_KEYS),
        field="terminal_authority",
    )
    authority: dict[str, StableArtifact | None] = {}
    missing: list[str] = []
    for name in TERMINAL_AUTHORITY_KEYS:
        value = authority_values[name]
        if value is None:
            authority[name] = None
            missing.append(name)
            continue
        authority[name] = _artifact(
            value,
            field=f"terminal_authority.{name}",
            release_identity=identity,
            json_required=True,
        )
    total_artifact_bytes = sum(
        len(artifact.raw)
        for group in (receipts, raw_observability, authority)
        for artifact in group.values()
        if artifact is not None
    )
    if total_artifact_bytes > MAX_TOTAL_ARTIFACT_BYTES:
        _reject("artifact_set_too_large", "manifest")
    raw_observability_companions = _capture_raw_observability_companions(
        raw_observability,
        release_identity=identity,
        existing_artifact_bytes=total_artifact_bytes,
    )
    if authority["release_preflight"] is not None:
        _validate_preflight(authority["release_preflight"].payload or {}, identity)
    if authority["disaster_recovery"] is not None:
        _validate_disaster_recovery(
            authority["disaster_recovery"].payload or {}, identity
        )
    if authority["capacity"] is not None:
        _validate_capacity(
            authority["capacity"].payload or {},
            identity,
            now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
            contract_sha256=capacity_contract_sha256,
        )
    if authority["observability_operations"] is not None:
        _validate_observability_operations(
            authority["observability_operations"].payload or {},
            identity,
            now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
            flagship_operations_sha256=flagship_operations_sha256,
        )

    output_values = _closed(
        root["outputs"],
        {
            "pinned_artifact_directory",
            "launch_evidence_directory",
            "gold_status_receipt",
            "terminal_status_receipt",
        },
        field="outputs",
    )
    outputs = {
        name: _new_output_path(output_values[name], field=f"outputs.{name}")
        for name in output_values
    }
    output_paths = tuple(outputs.values())
    if len(set(output_paths)) != len(output_paths):
        _reject("output_paths_overlap", "outputs")
    if any(
        os.path.commonpath((left, right)) in {left, right}
        for index, left in enumerate(output_paths)
        for right in output_paths[index + 1 :]
    ):
        _reject("output_paths_overlap", "outputs")

    invocation_contract = _validate_invocation_contract(
        root["invocation_contract"],
        outputs=outputs,
        flagship_operations_sha256=flagship_operations_sha256,
    )

    return LaunchManifest(
        release_identity=identity,
        product_data=product_data,
        receipts=receipts,
        raw_observability=raw_observability,
        raw_observability_companions=raw_observability_companions,
        terminal_authority=authority,
        missing_authority=tuple(missing),
        outputs=outputs,
        flagship_operations_sha256=flagship_operations_sha256,
        invocation_contract=invocation_contract,
    )


def _verify_controller_attestation(
    manifest: LaunchManifest,
    *,
    now: datetime | None = None,
) -> None:
    artifact = manifest.terminal_authority.get("controller_attestation")
    if artifact is None or artifact.payload is None:
        _reject("controller_attestation_missing", "terminal_authority.controller_attestation")
    payload = artifact.payload
    _closed(
        payload,
        {
            "schema",
            "deployment_id",
            "challenge_nonce",
            "release_identity",
            "product_data",
            "invocation_contract",
            "artifact_digests",
            "decisions",
            "authentication",
        },
        field="terminal_authority.controller_attestation.payload",
    )
    if payload.get("schema") != CONTROLLER_ATTESTATION_SCHEMA:
        _reject("controller_attestation_schema_invalid", "terminal_authority.controller_attestation")
    if payload.get("deployment_id") != PRODUCTION_DEPLOYMENT_ID:
        _reject("controller_deployment_mismatch", "terminal_authority.controller_attestation")
    embedded = _release_identity(
        payload.get("release_identity"),
        field="terminal_authority.controller_attestation.payload.release_identity",
    )
    if embedded != manifest.release_identity:
        _reject("receipt_release_identity_mismatch", "terminal_authority.controller_attestation")
    if payload.get("product_data") != manifest.product_data:
        _reject("controller_product_data_mismatch", "terminal_authority.controller_attestation")
    if payload.get("invocation_contract") != manifest.invocation_contract:
        _reject("controller_invocation_contract_mismatch", "terminal_authority.controller_attestation")
    if payload.get("artifact_digests") != manifest.attested_artifact_digests():
        _reject("controller_artifact_set_mismatch", "terminal_authority.controller_attestation")
    if payload.get("decisions") != {
        "release_preflight": "pass",
        "disaster_recovery": "pass",
        "capacity": "pass",
        "observability_operations": "pass",
    }:
        _reject("controller_decision_not_ready", "terminal_authority.controller_attestation")
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        observability = manifest.terminal_authority.get("observability_operations")
        if observability is None or observability.payload is None:
            raise evidence_contract.EvidenceContractError(
                "observability operations authority is missing"
            )
        _validate_observability_operations(
            observability.payload,
            manifest.release_identity,
            now=observed_now,
            flagship_operations_sha256=manifest.flagship_operations_sha256,
        )
        anchor, challenge = evidence_contract.load_evidence_challenge(
            expected_commit_sha=manifest.release_identity["commit_sha"],
            expected_image_digest=manifest.release_identity["image_digest"],
            now=observed_now,
        )
        if (
            challenge.deployment_id != PRODUCTION_DEPLOYMENT_ID
            or challenge.policy_hashes.get("flagship_operations_sha256")
            != manifest.flagship_operations_sha256
        ):
            raise evidence_contract.EvidenceContractError(
                "active challenge is not bound to production flagship operations policy"
            )
        live_receipts = (
            observability.payload.get("authenticated_live_receipts")
            if observability is not None and observability.payload is not None
            else None
        )
        if not isinstance(live_receipts, list) or any(
            not isinstance(row, Mapping)
            or row.get("authentication_key_id") == anchor.key_id
            for row in live_receipts
        ):
            raise evidence_contract.EvidenceContractError(
                "observability live receipts lack independent authentication"
            )
        evidence_contract.verify_authenticated_payload(
            payload,
            domain=CONTROLLER_ATTESTATION_DOMAIN,
            anchor=anchor,
            challenge=challenge,
            field="global launch terminal controller attestation",
        )
    except evidence_contract.EvidenceContractError:
        _reject("controller_cryptographic_verification_failed", "terminal_authority.controller_attestation")


def _create_private_directory(path: str, *, field: str) -> int:
    parent_descriptor = -1
    directory_descriptor = -1
    try:
        parent_descriptor = _open_secure_directory_chain(
            os.path.dirname(path),
            field=f"{field}.parent",
            root_only=False,
        )
        name = os.path.basename(path)
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        directory_descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        os.fchmod(directory_descriptor, 0o700)
        metadata = os.fstat(directory_descriptor)
    except OSError:
        _reject("output_create_failed", field)
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(directory_descriptor)
        _reject("output_create_failed", field)
    return directory_descriptor


def _write_pinned(
    name: str,
    raw: bytes,
    *,
    directory_descriptor: int,
    field: str,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(name, flags, 0o400, dir_fd=directory_descriptor)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _reject("pinned_write_failed", field)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
    except TerminalManifestError:
        raise
    except OSError:
        _reject("pinned_write_failed", field)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _pin_manifest_artifacts(manifest: LaunchManifest) -> dict[str, dict[str, str]]:
    root = manifest.outputs["pinned_artifact_directory"]
    directory_fd = _create_private_directory(
        root,
        field="outputs.pinned_artifact_directory",
    )
    pinned: dict[str, dict[str, str]] = {
        "receipts": {},
        "raw_observability": {},
        "terminal_authority": {},
    }
    groups: tuple[tuple[str, Mapping[str, StableArtifact | None]], ...] = (
        ("receipts", manifest.receipts),
        ("raw_observability", manifest.raw_observability),
        ("terminal_authority", manifest.terminal_authority),
    )
    try:
        for group, artifacts in groups:
            for name, artifact in artifacts.items():
                if artifact is None:
                    continue
                target_name = f"{group}--{name}.artifact"
                target = os.path.join(root, target_name)
                _write_pinned(
                    target_name,
                    artifact.raw,
                    directory_descriptor=directory_fd,
                    field=f"{group}.{name}",
                )
                pinned[group][name] = target
        for bundle_name in RAW_OBSERVABILITY_COMPANION_BUNDLES:
            for name, artifact in sorted(
                manifest.raw_observability_companions[bundle_name].items()
            ):
                _write_pinned(
                    name,
                    artifact.raw,
                    directory_descriptor=directory_fd,
                    field=(
                        f"raw_observability.{bundle_name}.companions.{name}"
                    ),
                )
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return pinned


def _bundle_artifact_set_sha256(files: Mapping[str, str]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            dict(files),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _open_digest_pinned_file(
    path: str,
    *,
    expected_sha256: str,
    field: str,
) -> int:
    parent_descriptor = _open_secure_directory_chain(
        os.path.dirname(path),
        field=f"{field}.parent",
        root_only=True,
    )
    descriptor = -1
    try:
        before_path = os.stat(os.path.basename(path), dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(
            os.path.basename(path),
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            _file_identity(before) != _file_identity(before_path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_JSON_ARTIFACT_BYTES
        ):
            _reject("installed_artifact_unsafe", field)
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        after_path = os.stat(
            os.path.basename(path),
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        actual_sha256 = "sha256:" + digest.hexdigest()
        if (
            remaining != 0
            or _file_identity(after) != _file_identity(before)
            or _file_identity(after_path) != _file_identity(before)
            or actual_sha256 != expected_sha256
        ):
            _reject("installed_artifact_digest_mismatch", field)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except TerminalManifestError:
        raise
    except OSError:
        _reject("installed_artifact_unavailable", field)
    finally:
        os.close(parent_descriptor)
        if sys.exc_info()[0] is not None and descriptor >= 0:
            os.close(descriptor)


def _installed_tree_inventory(
    root: Path,
    *,
    expected_uid: int,
) -> tuple[frozenset[str], frozenset[str]]:
    """Securely enumerate one immutable installed runtime tree.

    The bundle manifest authenticates expected file bytes.  This companion
    inventory closes the inverse condition: no unlisted module, bytecode, or
    configuration file may coexist under an importable installed path.
    """

    field = "installed_runtime.tree"
    if type(expected_uid) is not int or expected_uid < 0:
        _reject("installed_tree_unsafe", field)
    root_descriptor = _open_secure_directory_chain(
        str(root),
        field=field,
        root_only=expected_uid == 0,
    )
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    entry_count = 0

    def visit(directory_descriptor: int, prefix: PurePosixPath) -> None:
        nonlocal entry_count
        try:
            names = sorted(os.listdir(directory_descriptor))
        except OSError:
            _reject("installed_tree_unavailable", field)
        entry_count += len(names)
        if entry_count > MAX_INSTALLED_TREE_ENTRIES:
            _reject("installed_tree_too_large", field)
        for name in names:
            if (
                not name
                or name in {".", ".."}
                or "/" in name
                or len(name.encode("utf-8", errors="surrogatepass")) > 255
                or any(ord(character) < 32 for character in name)
            ):
                _reject("installed_tree_unsafe", field)
            relative = prefix / name if prefix.parts else PurePosixPath(name)
            try:
                before_path = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                _reject("installed_tree_unavailable", field)
            mode = stat.S_IMODE(before_path.st_mode)
            if before_path.st_uid != expected_uid or mode & 0o022:
                _reject("installed_tree_unsafe", field)
            if stat.S_ISREG(before_path.st_mode):
                if before_path.st_nlink != 1:
                    _reject("installed_tree_unsafe", field)
                observed_files.add(relative.as_posix())
                continue
            if not stat.S_ISDIR(before_path.st_mode):
                _reject("installed_tree_unsafe", field)
            observed_directories.add(relative.as_posix())
            child_descriptor = -1
            try:
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                before = os.fstat(child_descriptor)
                if _file_identity(before) != _file_identity(before_path):
                    _reject("installed_tree_changed", field)
                visit(child_descriptor, relative)
                after = os.fstat(child_descriptor)
                after_path = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _file_identity(after) != _file_identity(before)
                    or _file_identity(after_path) != _file_identity(before)
                ):
                    _reject("installed_tree_changed", field)
            except TerminalManifestError:
                raise
            except OSError:
                _reject("installed_tree_unavailable", field)
            finally:
                if child_descriptor >= 0:
                    os.close(child_descriptor)

    try:
        root_metadata = os.fstat(root_descriptor)
        if (
            root_metadata.st_uid != expected_uid
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            _reject("installed_tree_unsafe", field)
        visit(root_descriptor, PurePosixPath())
        root_after = os.fstat(root_descriptor)
        root_after_path = os.stat(root, follow_symlinks=False)
        if (
            _file_identity(root_after) != _file_identity(root_metadata)
            or _file_identity(root_after_path) != _file_identity(root_metadata)
        ):
            _reject("installed_tree_changed", field)
    finally:
        os.close(root_descriptor)
    return frozenset(observed_files), frozenset(observed_directories)


def _verify_installed_tree_inventory(
    root: Path,
    *,
    expected_relative_files: set[str] | frozenset[str],
    expected_uid: int,
) -> None:
    expected_files = frozenset(expected_relative_files)
    expected_directories = frozenset(
        parent.as_posix()
        for relative in expected_files
        for parent in PurePosixPath(relative).parents
        if parent.parts
    )
    observed_files, observed_directories = _installed_tree_inventory(
        root,
        expected_uid=expected_uid,
    )
    if (
        observed_files != expected_files
        or observed_directories != expected_directories
    ):
        _reject("installed_bundle_unexpected_files", "installed_runtime.tree")


def _verify_installed_runtime(manifest: LaunchManifest) -> int:
    if (
        os.geteuid() != 0
        or SOURCE_PATH != INSTALLED_ENTRYPOINT
        or Path(sys.executable) != INSTALLED_PYTHON_PATH
        or sys.flags.isolated != 1
    ):
        _reject("installed_entrypoint_required", "installed_runtime")
    runtime_artifacts = manifest.invocation_contract["runtime_artifacts"]
    if not isinstance(runtime_artifacts, Mapping):
        _reject("invocation_contract_mismatch", "invocation_contract.runtime_artifacts")
    bundle_descriptor = runtime_artifacts.get("bundle_manifest")
    if not isinstance(bundle_descriptor, Mapping):
        _reject("invocation_contract_mismatch", "invocation_contract.runtime_artifacts.bundle_manifest")
    bundle_path, bundle_raw = _stable_file(
        bundle_descriptor.get("path"),
        field="installed_runtime.bundle_manifest",
        maximum=MAX_JSON_ARTIFACT_BYTES,
        private=False,
    )
    if bundle_path != str(INSTALLED_BUNDLE_MANIFEST_PATH):
        _reject("installed_bundle_path_mismatch", "installed_runtime.bundle_manifest")
    bundle_parent = _open_secure_directory_chain(
        os.path.dirname(bundle_path),
        field="installed_runtime.bundle_manifest.parent",
        root_only=True,
    )
    os.close(bundle_parent)
    bundle_sha256 = "sha256:" + hashlib.sha256(bundle_raw).hexdigest()
    if bundle_sha256 != bundle_descriptor.get("sha256"):
        _reject("installed_artifact_digest_mismatch", "installed_runtime.bundle_manifest")
    bundle = _closed(
        _strict_json(bundle_raw, field="installed_runtime.bundle_manifest"),
        {
            "schema",
            "version",
            "install_root",
            "python",
            "files",
            "artifact_set_sha256",
        },
        field="installed_runtime.bundle_manifest",
    )
    if (
        bundle.get("schema") != INSTALLED_BUNDLE_SCHEMA
        or type(bundle.get("version")) is not int
        or bundle.get("version") != 1
        or bundle.get("install_root") != str(INSTALLED_ENTRYPOINT.parent)
    ):
        _reject("installed_bundle_schema_invalid", "installed_runtime.bundle_manifest")
    python_descriptor = _closed(
        bundle.get("python"),
        {"path", "sha256"},
        field="installed_runtime.bundle_manifest.python",
    )
    expected_python = runtime_artifacts.get("python")
    if python_descriptor != expected_python:
        _reject("installed_python_mismatch", "installed_runtime.bundle_manifest.python")
    python_path, python_raw = _stable_file(
        python_descriptor.get("path"),
        field="installed_runtime.python",
        maximum=512 * 1024 * 1024,
    )
    if (
        python_path != str(INSTALLED_PYTHON_PATH)
        or "sha256:" + hashlib.sha256(python_raw).hexdigest()
        != python_descriptor.get("sha256")
    ):
        _reject("installed_python_mismatch", "installed_runtime.python")
    python_parent = _open_secure_directory_chain(
        os.path.dirname(python_path),
        field="installed_runtime.python.parent",
        root_only=True,
    )
    os.close(python_parent)

    files = bundle.get("files")
    if type(files) is not dict or not files:
        _reject("installed_bundle_files_invalid", "installed_runtime.bundle_manifest.files")
    normalized_files: dict[str, str] = {}
    total_bytes = 0
    for relative, expected_sha256 in files.items():
        if type(relative) is not str or type(expected_sha256) is not str:
            _reject("installed_bundle_files_invalid", "installed_runtime.bundle_manifest.files")
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or str(relative_path) != relative
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            _reject("installed_bundle_files_invalid", "installed_runtime.bundle_manifest.files")
        expected_sha256 = _sha256(
            expected_sha256,
            field=f"installed_runtime.bundle_manifest.files.{relative}",
        )
        installed_path = INSTALLED_ENTRYPOINT.parent / relative_path
        _parent_fd = _open_secure_directory_chain(
            str(installed_path.parent),
            field=f"installed_runtime.files.{relative}.parent",
            root_only=True,
        )
        os.close(_parent_fd)
        actual_path, raw = _stable_file(
            str(installed_path),
            field=f"installed_runtime.files.{relative}",
            maximum=MAX_RAW_ARTIFACT_BYTES,
        )
        if (
            actual_path != str(installed_path)
            or "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            _reject("installed_artifact_digest_mismatch", f"installed_runtime.files.{relative}")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            _reject("installed_bundle_too_large", "installed_runtime.bundle_manifest.files")
        normalized_files[relative] = expected_sha256
    if (
        not MANDATORY_BUNDLE_RELATIVE_PATHS.issubset(normalized_files)
        or bundle.get("artifact_set_sha256")
        != _bundle_artifact_set_sha256(normalized_files)
    ):
        _reject("installed_bundle_artifact_set_mismatch", "installed_runtime.bundle_manifest")
    try:
        bundle_manifest_relative = INSTALLED_BUNDLE_MANIFEST_PATH.relative_to(
            INSTALLED_ENTRYPOINT.parent
        ).as_posix()
    except ValueError:
        _reject("installed_bundle_path_mismatch", "installed_runtime.bundle_manifest")
    if bundle_manifest_relative in normalized_files:
        _reject("installed_bundle_artifact_set_mismatch", "installed_runtime.bundle_manifest")
    _verify_installed_tree_inventory(
        INSTALLED_ENTRYPOINT.parent,
        expected_relative_files={*normalized_files, bundle_manifest_relative},
        expected_uid=0,
    )

    artifact_relative_paths = {
        "entrypoint": "propertyquarry-global-launch-terminal",
        "gold": "runtime/scripts/propertyquarry_gold_status.py",
        "evidence_contract": "runtime/scripts/propertyquarry_evidence_contract.py",
        "preflight_policy": "runtime/scripts/propertyquarry_release_preflight_policy.py",
        "flagship_operations_policy": (
            "runtime/config/monitoring/propertyquarry_flagship_operations.v1.json"
        ),
    }
    for name, relative in artifact_relative_paths.items():
        descriptor = runtime_artifacts.get(name)
        if (
            not isinstance(descriptor, Mapping)
            or descriptor.get("path") != INSTALLED_RUNTIME_ARTIFACT_PATHS[name]
            or descriptor.get("sha256") != normalized_files.get(relative)
        ):
            _reject("installed_artifact_contract_mismatch", f"installed_runtime.{name}")
    return _open_digest_pinned_file(
        INSTALLED_RUNTIME_ARTIFACT_PATHS["gold"],
        expected_sha256=str(runtime_artifacts["gold"]["sha256"]),
        field="installed_runtime.gold",
    )


def build_gold_argv(
    manifest: LaunchManifest,
    pinned: Mapping[str, Mapping[str, str]],
    *,
    gold_path: str | None = None,
) -> list[str]:
    gold_target = gold_path or str(ROOT / "scripts/propertyquarry_gold_status.py")
    argv = (
        [
            str(INSTALLED_PYTHON_PATH),
            *INSTALLED_PYTHON_FLAGS,
            "-c",
            GOLD_FD_BOOTSTRAP,
            gold_target,
        ]
        if gold_path is not None
        else [sys.executable, gold_target]
    )
    argv.extend(
        [
        "--profile",
        "launch",
        "--claim-scope",
        "core",
        "--required-browser-engines",
        "chromium,firefox,webkit",
        ]
    )
    for name, flag in CORE_RECEIPT_FLAGS.items():
        argv.extend((flag, pinned["receipts"][name]))
    for name, flag in RAW_OBSERVABILITY_FLAGS.items():
        argv.extend((flag, pinned["raw_observability"][name]))
    product = manifest.product_data
    # The signed controller policy is the expectation.  The performance
    # receipt remains an untrusted observation for Gold to compare against it.
    performance_browser_policy = _validate_performance_browser_policy(
        manifest.invocation_contract.get("performance_browser_policy"),
        field="invocation_contract.performance_browser_policy",
    )
    chromium_path = performance_browser_policy["executable_path"]
    chromium_sha256 = performance_browser_policy[
        "executable_sha256"
    ].removeprefix("sha256:")
    release_manifest_sha256 = str(
        manifest.invocation_contract["release_manifest_sha256"]
    ).removeprefix("sha256:")
    runtime_deployment_id = (
        RUNTIME_DEPLOYMENT_ID_PREFIX + manifest.release_identity["commit_sha"][:12]
    )
    argv.extend(
        (
            "--expected-release-sha",
            manifest.release_identity["commit_sha"],
            "--expected-image-digest",
            manifest.release_identity["image_digest"],
            "--expected-release-deployment-id",
            runtime_deployment_id,
            "--expected-release-manifest-sha256",
            release_manifest_sha256,
            "--expected-performance-chromium-executable-path",
            chromium_path,
            "--expected-performance-chromium-executable-sha256",
            chromium_sha256,
            "--expected-public-origin",
            product["public_origin"],
            "--expected-teable-origin",
            product["teable_origin"],
            "--expected-teable-base-id-sha256",
            product["teable_base_id_sha256"],
            "--expected-rybbit-origin",
            product["rybbit_origin"],
            "--expected-rybbit-site-id-sha256",
            product["rybbit_site_id_sha256"],
            "--expected-evidence-overlay-phase",
            product["evidence_overlay_phase"],
            "--launch-evidence-dir",
            manifest.outputs["launch_evidence_directory"],
            "--write",
            manifest.outputs["gold_status_receipt"],
            "--require-launch-evidence",
            "--fail-on-blocked",
        )
    )
    return argv


def _blocked(
    *,
    phase: str,
    blockers: Sequence[tuple[str, str]],
    release_identity: Mapping[str, str] | None = None,
    gold_invoked: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "status": "blocked",
        "phase": phase,
        "gold_invoked": gold_invoked,
        "blockers": [
            {"code": code, "field": field}
            for code, field in blockers
        ],
    }
    if release_identity is not None:
        result["release_identity"] = dict(release_identity)
    return result


def _canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_gold_result(
    payload: Mapping[str, object],
    *,
    returncode: int,
    release_identity: Mapping[str, str],
) -> None:
    try:
        identity = _release_identity(
            payload.get("release_identity"),
            field="gold_result.release_identity",
        )
    except TerminalManifestError:
        _reject("gold_result_identity_invalid", "gold_result.release_identity")
    status = payload.get("status")
    blockers = payload.get("blockers")
    if (
        payload.get("schema") != GOLD_STATUS_SCHEMA
        or identity != dict(release_identity)
        or payload.get("readiness_profile") != "launch"
        or payload.get("evidence_tier") != "launch"
        or payload.get("claim_scope") != "core"
        or status not in {"pass", "blocked"}
        or type(blockers) is not list
    ):
        _reject("gold_result_contract_mismatch", "gold_result")
    if status == "pass":
        if (
            returncode != 0
            or payload.get("core_gold_status") != "pass"
            or payload.get("ready_for_notification") is not True
            or blockers != []
        ):
            _reject("gold_result_exit_status_mismatch", "gold_result")
    elif (
        returncode != 2
        or payload.get("core_gold_status") != "blocked"
        or payload.get("ready_for_notification") is not False
        or not blockers
    ):
        _reject("gold_result_exit_status_mismatch", "gold_result")


def _terminal_envelope(
    manifest: LaunchManifest,
    *,
    gold_result: Mapping[str, object],
    gold_result_raw: bytes,
) -> dict[str, object]:
    controller = manifest.terminal_authority.get("controller_attestation")
    if controller is None:
        _reject("controller_attestation_missing", "terminal_authority.controller_attestation")
    status = str(gold_result.get("status"))
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "phase": "gold",
        "gold_invoked": True,
        "release_identity": dict(manifest.release_identity),
        "controller_attestation_sha256": controller.sha256,
        "attested_artifact_map_sha256": _canonical_sha256(
            manifest.attested_artifact_digests()
        ),
        "invocation_contract_sha256": _canonical_sha256(
            manifest.invocation_contract
        ),
        "gold_result_sha256": "sha256:" + hashlib.sha256(gold_result_raw).hexdigest(),
        "blockers": list(gold_result.get("blockers") or []) if status == "blocked" else [],
        "gold_result": dict(gold_result),
    }


def _write_terminal_status_receipt(
    path: str,
    payload: Mapping[str, object],
) -> None:
    raw = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > MAX_GOLD_RESULT_BYTES:
        _reject("terminal_result_too_large", "outputs.terminal_status_receipt")
    parent_fd = _open_secure_directory_chain(
        os.path.dirname(path),
        field="outputs.terminal_status_receipt.parent",
        root_only=False,
    )
    try:
        _write_pinned(
            os.path.basename(path),
            raw,
            directory_descriptor=parent_fd,
            field="outputs.terminal_status_receipt",
        )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


Runner = Callable[..., subprocess.CompletedProcess[object]]
AuthorityVerifier = Callable[[LaunchManifest], None]
InstalledVerifier = Callable[[LaunchManifest], int | None]


def execute_terminal(
    manifest: LaunchManifest,
    *,
    runner: Runner = subprocess.run,
    authority_verifier: AuthorityVerifier = _verify_controller_attestation,
    installed_verifier: InstalledVerifier = _verify_installed_runtime,
    effective_uid: int | None = None,
) -> tuple[int, dict[str, object]]:
    if manifest.missing_authority:
        blockers = [
            (f"{name}_evidence_missing", f"terminal_authority.{name}")
            for name in manifest.missing_authority
        ]
        return 2, _blocked(
            phase="terminal_authority",
            blockers=blockers,
            release_identity=manifest.release_identity,
        )
    try:
        authority_verifier(manifest)
    except TerminalManifestError as exc:
        return 2, _blocked(
            phase="controller_verification",
            blockers=[(exc.code, exc.field)],
            release_identity=manifest.release_identity,
        )
    except Exception:
        return 2, _blocked(
            phase="controller_verification",
            blockers=[("controller_verification_unavailable", "terminal_authority.controller_attestation")],
            release_identity=manifest.release_identity,
        )
    if (os.geteuid() if effective_uid is None else effective_uid) != 0:
        return 2, _blocked(
            phase="installed_authority",
            blockers=[("root_installed_authority_required", "terminal_invocation")],
            release_identity=manifest.release_identity,
        )
    gold_descriptor: int | None = None
    try:
        gold_descriptor = installed_verifier(manifest)
    except TerminalManifestError as exc:
        return 2, _blocked(
            phase="installed_authority",
            blockers=[(exc.code, exc.field)],
            release_identity=manifest.release_identity,
        )
    except Exception:
        return 2, _blocked(
            phase="installed_authority",
            blockers=[("installed_runtime_verification_failed", "installed_runtime")],
            release_identity=manifest.release_identity,
        )
    try:
        pinned = _pin_manifest_artifacts(manifest)
        gold_path = (
            f"/proc/self/fd/{gold_descriptor}"
            if gold_descriptor is not None
            else None
        )
        if gold_descriptor is not None and not Path("/proc/self/fd").is_dir():
            _reject("fd_pinned_execution_unavailable", "installed_runtime.gold")
        argv = build_gold_argv(manifest, pinned, gold_path=gold_path)
        if gold_descriptor is not None:
            argv[0] = str(INSTALLED_PYTHON_PATH)
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = runner(
            argv,
            cwd=str(ROOT),
            env=environment,
            shell=False,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GOLD_TIMEOUT_SECONDS,
            pass_fds=((gold_descriptor,) if gold_descriptor is not None else ()),
        )
    except subprocess.TimeoutExpired:
        return 2, _blocked(
            phase="gold_execution",
            blockers=[("gold_execution_timeout", "terminal_invocation")],
            release_identity=manifest.release_identity,
            gold_invoked=True,
        )
    except TerminalManifestError as exc:
        return 2, _blocked(
            phase="artifact_pinning",
            blockers=[(exc.code, exc.field)],
            release_identity=manifest.release_identity,
            gold_invoked=False,
        )
    except Exception:
        return 2, _blocked(
            phase="gold_execution",
            blockers=[("gold_execution_failed", "terminal_invocation")],
            release_identity=manifest.release_identity,
            gold_invoked=True,
        )
    finally:
        if gold_descriptor is not None:
            os.close(gold_descriptor)
    try:
        _gold_path, gold_result_raw = _stable_file(
            manifest.outputs["gold_status_receipt"],
            field="outputs.gold_status_receipt",
            maximum=MAX_GOLD_RESULT_BYTES,
            private=True,
        )
        gold_result = _strict_json(gold_result_raw, field="gold_result")
        _validate_gold_result(
            gold_result,
            returncode=completed.returncode,
            release_identity=manifest.release_identity,
        )
    except TerminalManifestError as exc:
        return 2, _blocked(
            phase="gold_verification",
            blockers=[(exc.code, exc.field)],
            release_identity=manifest.release_identity,
            gold_invoked=True,
        )
    if gold_result.get("status") == "pass":
        try:
            authority_verifier(manifest)
        except TerminalManifestError as exc:
            return 2, _blocked(
                phase="controller_revalidation",
                blockers=[(exc.code, exc.field)],
                release_identity=manifest.release_identity,
                gold_invoked=True,
            )
        except Exception:
            return 2, _blocked(
                phase="controller_revalidation",
                blockers=[("controller_revalidation_unavailable", "terminal_authority.controller_attestation")],
                release_identity=manifest.release_identity,
                gold_invoked=True,
            )
    envelope = _terminal_envelope(
        manifest,
        gold_result=gold_result,
        gold_result_raw=gold_result_raw,
    )
    try:
        _write_terminal_status_receipt(
            manifest.outputs["terminal_status_receipt"],
            envelope,
        )
    except TerminalManifestError as exc:
        return 2, _blocked(
            phase="terminal_receipt",
            blockers=[(exc.code, exc.field)],
            release_identity=manifest.release_identity,
            gold_invoked=True,
        )
    return (0 if gold_result.get("status") == "pass" else 2), envelope


def _print_result(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = subprocess.run,
    authority_verifier: AuthorityVerifier = _verify_controller_attestation,
    installed_verifier: InstalledVerifier = _verify_installed_runtime,
    effective_uid: int | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != "--manifest":
        _print_result(
            _blocked(
                phase="manifest_validation",
                blockers=[("terminal_arguments_invalid", "terminal_invocation")],
            )
        )
        return 2
    if SOURCE_PATH == INSTALLED_ENTRYPOINT and arguments[1] != GLOBAL_LAUNCH_MANIFEST_PATH:
        _print_result(
            _blocked(
                phase="manifest_validation",
                blockers=[("fixed_manifest_path_required", "terminal_invocation")],
            )
        )
        return 2
    try:
        manifest_path = Path(_path_text(arguments[1], field="manifest"))
        manifest = load_manifest(manifest_path)
    except TerminalManifestError as exc:
        _print_result(
            _blocked(
                phase="manifest_validation",
                blockers=[(exc.code, exc.field)],
            )
        )
        return 2
    except Exception:
        _print_result(
            _blocked(
                phase="manifest_validation",
                blockers=[("manifest_processing_failed", "manifest")],
            )
        )
        return 2
    code, result = execute_terminal(
        manifest,
        runner=runner,
        authority_verifier=authority_verifier,
        installed_verifier=installed_verifier,
        effective_uid=effective_uid,
    )
    _print_result(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
