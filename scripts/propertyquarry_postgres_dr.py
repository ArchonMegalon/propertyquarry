#!/usr/bin/env python3
"""Create and release-bind PropertyQuarry Postgres backups and disposable restore drills."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import urllib.parse


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EA_PACKAGE_ROOT = _REPO_ROOT / "ea"
AWS_CLI_RELEASE_PIN_REPO_PATH = "config/propertyquarry/aws_cli_release_pin.json"
AWS_CLI_RELEASE_PIN_PATH = _REPO_ROOT / AWS_CLI_RELEASE_PIN_REPO_PATH
if str(_EA_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EA_PACKAGE_ROOT))


RECEIPT_SCHEMA = "propertyquarry.postgres_dr_receipt.v3"
OFF_HOST_RETRIEVAL_SCHEMA = "propertyquarry.off_host_retrieval.v2"
AWS_CLI_ATTESTATION_CONTRACT_NAME = "propertyquarry.aws_cli_attestation"
AWS_CLI_ATTESTATION_CONTRACT_VERSION = 1
AWS_CLI_RELEASE_PIN_SCHEMA = "propertyquarry.aws_cli_release_pin.v1"
AWS_CLI_RELEASE_PIN_CONFIGURED = "CONFIGURED"
AWS_CLI_RELEASE_PIN_UNCONFIGURED = "UNCONFIGURED"
AWS_CLI_MINIMUM_VERSION = (2, 0, 0)
AWS_CLI_MINIMAL_PATH = "/usr/bin:/bin"
SNAPSHOT_CONTRACT_NAME = "propertyquarry.postgres_exported_snapshot"
SNAPSHOT_CONTRACT_VERSION = 2
CRITICAL_DATA_CONTRACT_NAME = "propertyquarry.postgres_critical_data"
CRITICAL_DATA_CONTRACT_VERSION = 4
CRITICAL_DATA_EVIDENCE_VERSION = 4
CRITICAL_DATA_FINGERPRINT_ALGORITHM = "postgresql_sha256_bounded_chunk_merkle_v2"
CRITICAL_DATA_SCHEMA = "public"
CRITICAL_DATA_CHUNK_SIZE = 1_024
CRITICAL_DATA_MAX_ROW_BYTES = 128 * 1_024 * 1_024
CRITICAL_DATA_MAX_CHUNKS = 65_536
CRITICAL_DATA_MAX_SUPPORTED_ROWS = CRITICAL_DATA_CHUNK_SIZE * CRITICAL_DATA_MAX_CHUNKS
CRITICAL_DATA_PRIVACY_SCHEMA_VERSION = 15
CRITICAL_DATA_ADMISSION_CAPACITY_SCHEMA_VERSION = 17
CRITICAL_DATA_PRE_PRIVACY_TABLE_SET_VERSION = 1
CRITICAL_DATA_PRIVACY_TABLE_SET_VERSION = 2
CRITICAL_DATA_ADMISSION_CAPACITY_TABLE_SET_VERSION = 3
CRITICAL_DATA_ADMISSION_QUOTA_MAX_ROWS = 1_000_000
CRITICAL_DATA_ADMISSION_LEASE_MAX_ROWS = 100_000
CRITICAL_DATA_ADMISSION_CAPACITY_LIMITS = {
    "lease": CRITICAL_DATA_ADMISSION_LEASE_MAX_ROWS,
    "quota": CRITICAL_DATA_ADMISSION_QUOTA_MAX_ROWS,
}
CRITICAL_DATA_PRE_PRIVACY_TABLES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("property_search_runs", ("principal_id", "run_id"), True),
    ("property_search_work_jobs", ("job_id",), False),
    ("delivery_outbox", ("delivery_id",), False),
    ("property_content_jobs", ("packet_id",), False),
    ("property_content_job_events", ("event_sequence",), False),
    ("property_content_webhook_events", ("provider", "provider_event_id"), False),
)
CRITICAL_DATA_PRIVACY_TABLES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    *CRITICAL_DATA_PRE_PRIVACY_TABLES,
    (
        "property_account_privacy_requests",
        ("principal_key", "request_id"),
        False,
    ),
)
CRITICAL_DATA_TABLES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    *CRITICAL_DATA_PRIVACY_TABLES,
    ("propertyquarry_admission_quota_buckets", ("bucket_key",), False),
    (
        "propertyquarry_admission_leases",
        ("lease_id", "dimension_key"),
        False,
    ),
    ("propertyquarry_admission_capacity_state", ("capacity_key",), True),
)
_CRITICAL_DATA_NON_TEXT_IDENTITIES = {
    ("property_content_job_events", "event_sequence"),
    ("propertyquarry_admission_leases", "lease_id"),
}
_CRITICAL_DATA_TEXT_IDENTITIES = {
    (table, column)
    for table, columns, _data_required in CRITICAL_DATA_TABLES
    for column in columns
    if (table, column) not in _CRITICAL_DATA_NON_TEXT_IDENTITIES
}
DISPOSABLE_CONFIRMATION = "YES_DESTROY_DISPOSABLE_TARGET"
DEFAULT_DISPOSABLE_PREFIX = "propertyquarry_restore_drill_"
DEFAULT_ARTIFACT_MAX_AGE_SECONDS = 86_400.0
DEFAULT_RESTORE_MAX_DURATION_SECONDS = 1_800.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 3_600.0
DEFAULT_RELEASE_EVIDENCE_MAX_AGE_SECONDS = 86_400.0
DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS = 300.0
DEFAULT_OFF_HOST_MINIMUM_RETENTION_SECONDS = 7 * 86_400.0
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
RESTORE_RTO_SCOPE = (
    "off_host_retrieval",
    "decryption",
    "archive_validation",
    "database_restore",
    "migration",
    "data_integrity",
    "readiness",
)
_POSTGRES_URL_RE = re.compile(r"postgres(?:ql)?://[^\s'\"]+", flags=re.IGNORECASE)
_SAFE_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_S3_ETAG_RE = re.compile(r"^[0-9a-f]{32}(?:-[1-9][0-9]*)?$", flags=re.IGNORECASE)
_AWS_REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[1-9][0-9]*$")
_AWS_CLI_VERSION_RE = re.compile(r"\baws-cli/([0-9]+)\.([0-9]+)\.([0-9]+)\b")
_IPV4_ADDRESS_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$")
_AWS_REQUEST_ID_RE = re.compile(
    r"x-amz-request-id[^:]*:\s*(?:b)?['\"]?([A-Za-z0-9+/=_-]{8,})",
    flags=re.IGNORECASE,
)
_EXPORTED_SNAPSHOT_RE = re.compile(r"^[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HOOK_TLS_ENV_KEYS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
_AWS_PROVIDER_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_CA_BUNDLE",
)
_HOOK_FORBIDDEN_ENV_KEYS = {
    "BASH_ENV",
    "ENV",
    "GCONV_PATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONHOME",
    "PYTHONPATH",
    "SHELLOPTS",
}
REMOTE_PROVIDER_CONTRACTS: dict[str, dict[str, object]] = {
    "s3": {
        "backend": "aws_s3api",
        "verification_method": "aws_s3api_head_and_get_object_version_sha256_v1",
        "retrieval_method": "aws_s3api_head_and_get_object_version_sha256_v1",
    },
}

Runner = Callable[..., Any]
Clock = Callable[[], float]
Which = Callable[[str], str | None]
SnapshotConnector = Callable[..., Any]


class DisasterRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "dr_error")
        self.details = dict(details or {})


class _DuplicateReceiptKey(ValueError):
    pass


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: object) -> float:
    raw = str(value or "").strip()
    if not raw:
        raise DisasterRecoveryError("backup_timestamp_missing", "Backup receipt has no completion timestamp.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone_required")
        return parsed.timestamp()
    except Exception as exc:
        raise DisasterRecoveryError("backup_timestamp_invalid", "Backup completion timestamp is invalid.") from exc


def _float_env(environ: Mapping[str, str], name: str, default: float, *, minimum: float = 1.0) -> float:
    raw = str(environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception as exc:
        raise DisasterRecoveryError("environment_invalid", f"{name} must be numeric.") from exc
    if not math.isfinite(value) or value < minimum:
        raise DisasterRecoveryError("environment_invalid", f"{name} must be at least {minimum}.")
    return value


def _truthy(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _redact_text(value: object) -> str:
    return _POSTGRES_URL_RE.sub("<redacted-database-url>", str(value or ""))


def _redacted_command(command: Sequence[str]) -> list[str]:
    return [_redact_text(item) for item in command]


def _database_identity(database_url: str, *, label: str) -> dict[str, object]:
    raw = str(database_url or "").strip()
    if not raw:
        raise DisasterRecoveryError("database_url_missing", f"{label} database URL is required.")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception as exc:
        raise DisasterRecoveryError("database_url_invalid", f"{label} database URL is invalid.") from exc
    if parsed.scheme.lower() not in {"postgres", "postgresql"}:
        raise DisasterRecoveryError("database_url_invalid", f"{label} database URL must use postgres or postgresql.")
    host = str(parsed.hostname or "").strip().lower()
    database = urllib.parse.unquote(str(parsed.path or "").lstrip("/")).strip()
    if not host or not database:
        raise DisasterRecoveryError("database_url_invalid", f"{label} database URL must include host and database.")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise DisasterRecoveryError("database_url_invalid", f"{label} database URL port is invalid.") from exc
    return {"scheme": "postgresql", "host": host, "port": port, "database": database}


def _identity_key(identity: Mapping[str, object]) -> tuple[str, int, str]:
    return (
        str(identity.get("host") or "").strip().lower(),
        int(identity.get("port") or 5432),
        str(identity.get("database") or "").strip().lower(),
    )


def _binary(environ: Mapping[str, str], env_name: str, default: str, which: Which) -> str:
    requested = str(environ.get(env_name) or default).strip() or default
    resolved = which(requested)
    if not resolved:
        raise DisasterRecoveryError("binary_missing", f"Required executable is unavailable: {requested}")
    return resolved


def _command_timeout(environ: Mapping[str, str]) -> float:
    return _float_env(
        environ,
        "PROPERTYQUARRY_DR_COMMAND_TIMEOUT_SECONDS",
        DEFAULT_COMMAND_TIMEOUT_SECONDS,
        minimum=10.0,
    )


def _run_checked(
    *,
    step: str,
    command: Sequence[str],
    environ: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    extra_env: Mapping[str, str] | None = None,
    recorded_command: Sequence[str] | None = None,
    include_failure_stderr: bool = True,
    process_environment: Mapping[str, str] | None = None,
    executable: str | None = None,
    pass_fds: Sequence[int] = (),
) -> Any:
    command_list = [str(item) for item in command]
    command_for_receipt = [str(item) for item in (recorded_command or command_list)]
    commands.append({"step": step, "command": _redacted_command(command_for_receipt)})
    process_env = dict(environ if process_environment is None else process_environment)
    process_env.update({str(key): str(value) for key, value in dict(extra_env or {}).items()})
    runner_kwargs: dict[str, object] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": _command_timeout(environ),
        "env": process_env,
    }
    if executable is not None:
        runner_kwargs["executable"] = executable
    if pass_fds:
        runner_kwargs["pass_fds"] = tuple(int(descriptor) for descriptor in pass_fds)
    try:
        result = runner(command_list, **runner_kwargs)
    except subprocess.TimeoutExpired as exc:
        raise DisasterRecoveryError("command_timeout", f"{step} timed out.") from exc
    except Exception as exc:
        raise DisasterRecoveryError("command_failed", f"{step} could not start: {_redact_text(exc)}") from exc
    if int(getattr(result, "returncode", 1) or 0) != 0:
        stderr = _redact_text(getattr(result, "stderr", ""))[-800:] if include_failure_stderr else ""
        details: dict[str, object] = {"failed_step": step}
        if stderr:
            details["stderr_tail"] = stderr
        raise DisasterRecoveryError(
            "command_failed",
            f"{step} failed.",
            details=details,
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_file(path: Path) -> None:
    path.chmod(0o600)


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _normalize_s3_etag(value: object) -> str:
    raw = str(value or "").strip()
    if raw.startswith('"') or raw.endswith('"'):
        if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
            raise DisasterRecoveryError(
                "off_host_etag_invalid",
                "S3 ETag quoting must be balanced.",
            )
        raw = raw[1:-1]
    if '"' in raw or not _S3_ETAG_RE.fullmatch(raw):
        raise DisasterRecoveryError(
            "off_host_etag_invalid",
            "S3 ETag does not satisfy the provider-native identity contract.",
        )
    return raw.lower()


def _parse_aws_cli_release_pin(raw: bytes) -> dict[str, str]:
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_invalid",
            "Canonical AWS CLI release pin is not valid UTF-8 JSON.",
        ) from exc
    expected_keys = {"path", "schema", "sha256", "status", "version"}
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_invalid",
            "Canonical AWS CLI release pin has an unexpected schema.",
        )
    schema = str(payload.get("schema") or "").strip()
    status = str(payload.get("status") or "").strip()
    path = str(payload.get("path") or "").strip()
    version = str(payload.get("version") or "").strip()
    sha256_value = str(payload.get("sha256") or "").strip()
    if schema != AWS_CLI_RELEASE_PIN_SCHEMA:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_invalid",
            "Canonical AWS CLI release pin schema is not release-controlled.",
        )
    if status == AWS_CLI_RELEASE_PIN_UNCONFIGURED:
        if (path, version, sha256_value) != (
            AWS_CLI_RELEASE_PIN_UNCONFIGURED,
            AWS_CLI_RELEASE_PIN_UNCONFIGURED,
            AWS_CLI_RELEASE_PIN_UNCONFIGURED,
        ):
            raise DisasterRecoveryError(
                "aws_cli_release_pin_invalid",
                "UNCONFIGURED AWS CLI release pin contains partial approval data.",
            )
        raise DisasterRecoveryError(
            "aws_cli_release_pin_unconfigured",
            "Canonical AWS CLI release pin is UNCONFIGURED; commit a reviewed pin before DR proof.",
        )
    if status != AWS_CLI_RELEASE_PIN_CONFIGURED:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_invalid",
            "Canonical AWS CLI release pin status is invalid.",
        )
    sha256 = sha256_value.lower()
    version_match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    cli_path = Path(path)
    if (
        not cli_path.is_absolute()
        or os.path.normpath(path) != path
        or not version_match
        or tuple(int(item) for item in version_match.groups()) < AWS_CLI_MINIMUM_VERSION
        or not _SHA256_RE.fullmatch(sha256)
    ):
        raise DisasterRecoveryError(
            "aws_cli_release_pin_invalid",
            "Canonical AWS CLI release pin contains an invalid path, version, or SHA-256.",
        )
    return {
        "schema": schema,
        "status": status,
        "path": path,
        "version": version,
        "sha256": sha256,
        "manifest_repo_path": AWS_CLI_RELEASE_PIN_REPO_PATH,
        "manifest_sha256": manifest_sha256,
    }


def _load_aws_cli_release_pin() -> dict[str, str]:
    path = AWS_CLI_RELEASE_PIN_PATH
    try:
        before = os.lstat(path)
        resolved = path.resolve(strict=True)
    except Exception as exc:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_missing",
            "Canonical AWS CLI release pin file is unavailable.",
        ) from exc
    if (
        resolved != path
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid not in {0, os.geteuid()}
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or before.st_nlink != 1
    ):
        raise DisasterRecoveryError(
            "aws_cli_release_pin_untrusted",
            "Canonical AWS CLI release pin must be a trusted immutable regular file.",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _aws_cli_file_identity(opened) != _aws_cli_file_identity(before):
            raise DisasterRecoveryError(
                "aws_cli_release_pin_race",
                "Canonical AWS CLI release pin changed while it was opened.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            total += len(chunk)
            if total > 16_384:
                raise DisasterRecoveryError(
                    "aws_cli_release_pin_invalid",
                    "Canonical AWS CLI release pin exceeds its bounded size.",
                )
            chunks.append(chunk)
        after_opened = os.fstat(descriptor)
        after_path = os.lstat(path)
        if (
            _aws_cli_file_identity(after_opened) != _aws_cli_file_identity(opened)
            or _aws_cli_file_identity(after_path) != _aws_cli_file_identity(opened)
        ):
            raise DisasterRecoveryError(
                "aws_cli_release_pin_race",
                "Canonical AWS CLI release pin changed while it was read.",
            )
        return _parse_aws_cli_release_pin(b"".join(chunks))
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_aws_cli_release_pin_overrides(environ: Mapping[str, str]) -> None:
    forbidden = (
        "PROPERTYQUARRY_AWS_CLI_PATH",
        "PROPERTYQUARRY_AWS_CLI_APPROVED_VERSION",
        "PROPERTYQUARRY_AWS_CLI_APPROVED_SHA256",
    )
    selected = [name for name in forbidden if str(environ.get(name) or "").strip()]
    if selected:
        raise DisasterRecoveryError(
            "aws_cli_release_pin_override_forbidden",
            "AWS CLI approval is release-controlled; environment overrides are forbidden.",
            details={"forbidden_overrides": selected},
        )


def _aws_cli_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
    )


def _validate_aws_cli_file(path: Path, value: os.stat_result) -> None:
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or value.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or not value.st_mode & stat.S_IXUSR
        or value.st_nlink != 1
    ):
        raise DisasterRecoveryError(
            "aws_cli_path_untrusted",
            f"AWS CLI must be trusted-owner, executable, singly linked, and immutable to peers: {path}",
        )


@contextmanager
def _open_attested_aws_cli(
    *,
    path: Path,
    expected_sha256: str,
    expected_attestation: Mapping[str, object] | None = None,
) -> Iterator[tuple[int, os.stat_result, str]]:
    try:
        before = os.lstat(path)
        resolved = path.resolve(strict=True)
    except Exception as exc:
        raise DisasterRecoveryError(
            "aws_cli_path_invalid",
            "Pinned AWS CLI path is unavailable.",
        ) from exc
    if resolved != path:
        raise DisasterRecoveryError(
            "aws_cli_path_untrusted",
            "AWS CLI path must be canonical and contain no symlink resolution.",
        )
    _validate_aws_cli_file(path, before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    opened: os.stat_result | None = None
    identity: tuple[int, int, int, int, int, int, int, int] | None = None
    yielded = False
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _validate_aws_cli_file(path, opened)
        identity = _aws_cli_file_identity(opened)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise DisasterRecoveryError(
                "aws_cli_binary_race",
                "AWS CLI identity changed while it was being opened.",
            )
        actual_sha256 = _sha256_descriptor(descriptor)
        if actual_sha256 != expected_sha256:
            raise DisasterRecoveryError(
                "aws_cli_sha256_mismatch",
                "AWS CLI SHA-256 does not match the release-approved digest.",
            )
        if expected_attestation is not None and (
            opened.st_dev != int(expected_attestation.get("device") or -1)
            or opened.st_ino != int(expected_attestation.get("inode") or -1)
            or opened.st_mtime_ns != int(expected_attestation.get("mtime_ns") or -1)
        ):
            raise DisasterRecoveryError(
                "aws_cli_binary_replaced",
                "AWS CLI path no longer identifies the attested binary inode.",
            )
        path_after = os.lstat(path)
        if _aws_cli_file_identity(path_after) != identity:
            raise DisasterRecoveryError(
                "aws_cli_binary_race",
                "AWS CLI identity changed during attestation.",
            )
        yielded = True
        yield descriptor, opened, actual_sha256
    finally:
        post_error: DisasterRecoveryError | None = None
        if yielded and descriptor >= 0 and opened is not None and identity is not None:
            try:
                final_opened = os.fstat(descriptor)
                final_path = os.lstat(path)
                final_sha256 = _sha256_descriptor(descriptor)
                if (
                    _aws_cli_file_identity(final_opened) != identity
                    or _aws_cli_file_identity(final_path) != identity
                    or final_sha256 != expected_sha256
                ):
                    post_error = DisasterRecoveryError(
                        "aws_cli_binary_race",
                        "AWS CLI identity changed while the provider command was running.",
                    )
            except Exception as exc:
                post_error = DisasterRecoveryError(
                    "aws_cli_binary_race",
                    "AWS CLI identity could not be revalidated after provider execution.",
                )
                post_error.__cause__ = exc
        if descriptor >= 0:
            os.close(descriptor)
        if post_error is not None:
            raise post_error


def _aws_cli_fd_executable(descriptor: int) -> str:
    proc_fd_root = Path("/proc/self/fd")
    if not proc_fd_root.is_dir():
        raise DisasterRecoveryError(
            "aws_cli_fd_execution_unavailable",
            "Race-safe AWS CLI descriptor execution requires /proc/self/fd.",
        )
    return f"/proc/self/fd/{descriptor}"


def _attest_aws_cli(
    *,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
) -> dict[str, object]:
    _reject_aws_cli_release_pin_overrides(env)
    approved = _load_aws_cli_release_pin()
    path = Path(approved["path"])
    with _open_attested_aws_cli(
        path=path,
        expected_sha256=approved["sha256"],
    ) as (descriptor, opened, actual_sha256):
        version_result = _run_checked(
            step="attest_aws_cli_version",
            command=[str(path), "--version"],
            environ=env,
            runner=runner,
            commands=commands,
            include_failure_stderr=False,
            process_environment={"PATH": AWS_CLI_MINIMAL_PATH, "LANG": "C", "LC_ALL": "C"},
            executable=_aws_cli_fd_executable(descriptor),
            pass_fds=(descriptor,),
        )
        version_output = " ".join(
            (
                str(getattr(version_result, "stdout", "") or ""),
                str(getattr(version_result, "stderr", "") or ""),
            )
        )
        match = _AWS_CLI_VERSION_RE.search(version_output)
        actual_version = ".".join(match.groups()) if match else ""
        if not match or actual_version != approved["version"]:
            raise DisasterRecoveryError(
                "aws_cli_version_mismatch",
                "AWS CLI version does not match the release-approved version.",
            )
        return {
            "contract_name": AWS_CLI_ATTESTATION_CONTRACT_NAME,
            "contract_version": AWS_CLI_ATTESTATION_CONTRACT_VERSION,
            "path": str(path),
            "version": actual_version,
            "sha256": actual_sha256,
            "size_bytes": opened.st_size,
            "owner_uid": opened.st_uid,
            "mode": format(stat.S_IMODE(opened.st_mode), "04o"),
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "mtime_ns": opened.st_mtime_ns,
            "regular_file": True,
            "symlink": False,
            "group_world_writable": False,
            "single_link": True,
            "minimal_path": AWS_CLI_MINIMAL_PATH,
            "release_pin": dict(approved),
        }


def _validated_aws_cli_attestation(
    payload: object,
    *,
    label: str,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "aws_cli_attestation_missing",
            f"{label} AWS CLI attestation is missing.",
        )
    path = str(payload.get("path") or "").strip()
    version = str(payload.get("version") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    approved = _load_aws_cli_release_pin()
    raw_release_pin = payload.get("release_pin")
    release_pin = (
        {str(key): str(value) for key, value in raw_release_pin.items()}
        if isinstance(raw_release_pin, Mapping)
        else {}
    )
    if (
        release_pin != approved
        or path != approved["path"]
        or version != approved["version"]
        or sha256 != approved["sha256"]
    ):
        raise DisasterRecoveryError(
            "aws_cli_release_pin_mismatch",
            f"{label} AWS CLI attestation is not bound to the canonical release pin.",
        )
    mode_text = str(payload.get("mode") or "")
    try:
        size_bytes = int(payload.get("size_bytes"))
        owner_uid = int(payload.get("owner_uid"))
        device = int(payload.get("device"))
        inode = int(payload.get("inode"))
        mtime_ns = int(payload.get("mtime_ns"))
        mode = int(mode_text, 8)
    except Exception as exc:
        raise DisasterRecoveryError(
            "aws_cli_attestation_invalid",
            f"{label} AWS CLI attestation has invalid numeric fields.",
        ) from exc
    version_match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if (
        payload.get("contract_name") != AWS_CLI_ATTESTATION_CONTRACT_NAME
        or payload.get("contract_version") != AWS_CLI_ATTESTATION_CONTRACT_VERSION
        or not Path(path).is_absolute()
        or os.path.normpath(path) != path
        or not version_match
        or tuple(int(item) for item in version_match.groups()) < AWS_CLI_MINIMUM_VERSION
        or not _SHA256_RE.fullmatch(sha256)
        or size_bytes <= 0
        or owner_uid not in {0, os.geteuid()}
        or device < 0
        or inode <= 0
        or mtime_ns < 0
        or not re.fullmatch(r"0[0-7]{3}", mode_text)
        or mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or not mode & stat.S_IXUSR
        or payload.get("regular_file") is not True
        or payload.get("symlink") is not False
        or payload.get("group_world_writable") is not False
        or payload.get("single_link") is not True
        or payload.get("minimal_path") != AWS_CLI_MINIMAL_PATH
    ):
        raise DisasterRecoveryError(
            "aws_cli_attestation_invalid",
            f"{label} AWS CLI attestation does not satisfy the release contract.",
        )
    return {
        "contract_name": AWS_CLI_ATTESTATION_CONTRACT_NAME,
        "contract_version": AWS_CLI_ATTESTATION_CONTRACT_VERSION,
        "path": path,
        "version": version,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "owner_uid": owner_uid,
        "mode": mode_text,
        "device": device,
        "inode": inode,
        "mtime_ns": mtime_ns,
        "regular_file": True,
        "symlink": False,
        "group_world_writable": False,
        "single_link": True,
        "minimal_path": AWS_CLI_MINIMAL_PATH,
        "release_pin": dict(approved),
    }


def _run_attested_aws_cli(
    *,
    step: str,
    arguments: Sequence[str],
    attestation: Mapping[str, object],
    env: Mapping[str, str],
    provider_env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    recorded_command: Sequence[str],
    extra_pass_fds: Sequence[int] = (),
) -> Any:
    validated = _validated_aws_cli_attestation(attestation, label="Runtime")
    path = Path(str(validated["path"]))
    with _open_attested_aws_cli(
        path=path,
        expected_sha256=str(validated["sha256"]),
        expected_attestation=validated,
    ) as (descriptor, _opened, _sha256_value):
        pass_fds = (descriptor, *(int(item) for item in extra_pass_fds))
        return _run_checked(
            step=step,
            command=[str(path), *(str(item) for item in arguments)],
            environ=env,
            runner=runner,
            commands=commands,
            recorded_command=recorded_command,
            include_failure_stderr=False,
            process_environment=provider_env,
            executable=_aws_cli_fd_executable(descriptor),
            pass_fds=pass_fds,
        )


def _default_snapshot_connector(database_url: str, **kwargs: object) -> object:
    try:
        import psycopg
    except Exception as exc:
        raise DisasterRecoveryError(
            "snapshot_driver_unavailable",
            "psycopg is required to export the backup snapshot.",
        ) from exc
    return psycopg.connect(database_url, **kwargs)


@contextmanager
def _exported_repeatable_read_snapshot(
    *,
    database_url: str,
    connector: SnapshotConnector | None = None,
) -> Iterator[dict[str, object]]:
    connect = connector or _default_snapshot_connector
    connection: object | None = None
    cursor: object | None = None
    transaction_open = False
    try:
        connection = connect(database_url, autocommit=False, connect_timeout=5)
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")  # type: ignore[attr-defined]
        transaction_open = True
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT pg_export_snapshot()::text, txid_current_snapshot()::text;"
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes, bytearray))
            or len(row) != 2
        ):
            raise DisasterRecoveryError(
                "snapshot_export_invalid",
                "Postgres did not return an exported snapshot identity.",
            )
        snapshot_id = str(row[0] or "").strip()
        transaction_snapshot = str(row[1] or "").strip()
        if not _EXPORTED_SNAPSHOT_RE.fullmatch(snapshot_id) or not transaction_snapshot:
            raise DisasterRecoveryError(
                "snapshot_export_invalid",
                "Postgres returned an invalid exported snapshot identity.",
            )
        yield {
            "snapshot_id": snapshot_id,
            "snapshot_id_sha256": hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest(),
            "transaction_snapshot_sha256": hashlib.sha256(
                transaction_snapshot.encode("utf-8")
            ).hexdigest(),
        }
    except DisasterRecoveryError:
        raise
    except Exception as exc:
        raise DisasterRecoveryError(
            "snapshot_export_failed",
            "The repeatable-read backup snapshot could not be exported.",
        ) from exc
    finally:
        if transaction_open and connection is not None:
            try:
                connection.rollback()  # type: ignore[attr-defined]
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()  # type: ignore[attr-defined]
            except Exception:
                pass


def _snapshot_evidence(
    exported: Mapping[str, object],
    *,
    pg_dump_plaintext_sha256: str,
    governing_schema_version: int | None = None,
) -> dict[str, object]:
    critical_data_contract = _critical_data_contract(governing_schema_version)
    payload: dict[str, object] = {
        "contract_name": SNAPSHOT_CONTRACT_NAME,
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "isolation_level": "repeatable_read",
        "read_only": True,
        "snapshot_id_sha256": str(exported.get("snapshot_id_sha256") or "").lower(),
        "transaction_snapshot_sha256": str(
            exported.get("transaction_snapshot_sha256") or ""
        ).lower(),
        "pg_dump_snapshot_bound": True,
        "schema_ledger_snapshot_bound": True,
        "critical_data_snapshot_bound": True,
        "critical_data_query_count": len(critical_data_contract["tables"]),
        "critical_data_governing_schema_version": critical_data_contract[
            "governing_schema_version"
        ],
        "critical_data_table_set_version": critical_data_contract[
            "table_set_version"
        ],
        "critical_data_contract_fingerprint_sha256": critical_data_contract[
            "contract_fingerprint_sha256"
        ],
        "snapshot_exporter_held_open": True,
        "pg_dump_plaintext_sha256": pg_dump_plaintext_sha256,
    }
    payload["snapshot_dump_binding_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def _validated_snapshot_evidence(
    payload: object,
    *,
    label: str,
    pg_dump_plaintext_sha256: str | None = None,
    governing_schema_version: int | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "snapshot_evidence_missing",
            f"{label} exported-snapshot evidence is missing.",
        )
    critical_data_contract = _critical_data_contract(governing_schema_version)
    normalized = {
        "contract_name": str(payload.get("contract_name") or ""),
        "contract_version": payload.get("contract_version"),
        "isolation_level": str(payload.get("isolation_level") or ""),
        "read_only": payload.get("read_only"),
        "snapshot_id_sha256": str(payload.get("snapshot_id_sha256") or "").lower(),
        "transaction_snapshot_sha256": str(
            payload.get("transaction_snapshot_sha256") or ""
        ).lower(),
        "pg_dump_snapshot_bound": payload.get("pg_dump_snapshot_bound"),
        "schema_ledger_snapshot_bound": payload.get("schema_ledger_snapshot_bound"),
        "critical_data_snapshot_bound": payload.get("critical_data_snapshot_bound"),
        "critical_data_query_count": payload.get("critical_data_query_count"),
        "critical_data_governing_schema_version": payload.get(
            "critical_data_governing_schema_version"
        ),
        "critical_data_table_set_version": payload.get(
            "critical_data_table_set_version"
        ),
        "critical_data_contract_fingerprint_sha256": str(
            payload.get("critical_data_contract_fingerprint_sha256") or ""
        ).lower(),
        "snapshot_exporter_held_open": payload.get("snapshot_exporter_held_open"),
        "pg_dump_plaintext_sha256": str(
            payload.get("pg_dump_plaintext_sha256") or ""
        ).lower(),
        "snapshot_dump_binding_sha256": str(
            payload.get("snapshot_dump_binding_sha256") or ""
        ).lower(),
    }
    binding_payload = dict(normalized)
    declared_binding = str(binding_payload.pop("snapshot_dump_binding_sha256") or "")
    expected_binding = hashlib.sha256(
        json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        normalized["contract_name"] != SNAPSHOT_CONTRACT_NAME
        or normalized["contract_version"] != SNAPSHOT_CONTRACT_VERSION
        or normalized["isolation_level"] != "repeatable_read"
        or normalized["read_only"] is not True
        or normalized["pg_dump_snapshot_bound"] is not True
        or normalized["schema_ledger_snapshot_bound"] is not True
        or normalized["critical_data_snapshot_bound"] is not True
        or normalized["critical_data_query_count"]
        != len(critical_data_contract["tables"])
        or normalized["critical_data_governing_schema_version"]
        != critical_data_contract["governing_schema_version"]
        or normalized["critical_data_table_set_version"]
        != critical_data_contract["table_set_version"]
        or normalized["critical_data_contract_fingerprint_sha256"]
        != critical_data_contract["contract_fingerprint_sha256"]
        or normalized["snapshot_exporter_held_open"] is not True
        or not _SHA256_RE.fullmatch(str(normalized["snapshot_id_sha256"]))
        or not _SHA256_RE.fullmatch(str(normalized["transaction_snapshot_sha256"]))
        or not _SHA256_RE.fullmatch(str(normalized["pg_dump_plaintext_sha256"]))
        or declared_binding != expected_binding
        or (
            pg_dump_plaintext_sha256 is not None
            and normalized["pg_dump_plaintext_sha256"] != pg_dump_plaintext_sha256
        )
    ):
        raise DisasterRecoveryError(
            "snapshot_evidence_invalid",
            f"{label} evidence is not bound to one exported repeatable-read snapshot.",
        )
    return normalized


def _snapshot_bound_sql(sql: str, snapshot_id: str | None) -> str:
    if snapshot_id is None:
        return sql
    if not _EXPORTED_SNAPSHOT_RE.fullmatch(snapshot_id):
        raise DisasterRecoveryError(
            "snapshot_identity_invalid",
            "The imported Postgres snapshot identity is invalid.",
        )
    return (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        f"SET TRANSACTION SNAPSHOT '{snapshot_id}'; "
        f"{sql}"
    )


def _receipt_file_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
    )


def _receipt_directory_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


_ReceiptDirectoryHandle = tuple[
    int,
    int | None,
    str | None,
    tuple[int, ...],
]


def _canonical_receipt_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path.expanduser()))))


def _close_receipt_directory_handles(
    handles: list[_ReceiptDirectoryHandle],
) -> None:
    for descriptor, _, _, _ in reversed(handles):
        with suppress(OSError):
            os.close(descriptor)


def _validate_receipt_directory_trust(
    value: os.stat_result,
    *,
    final: bool,
    code: str,
    label: str,
) -> None:
    peer_writable = bool(value.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky = bool(value.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or (peer_writable and (final or not sticky))
    ):
        raise DisasterRecoveryError(
            code,
            f"{label} must use a trusted-owner directory chain; "
            "only non-final sticky ancestors may be writable by peers.",
        )


def _revalidate_receipt_directory_chain(
    handles: list[_ReceiptDirectoryHandle],
    *,
    code: str = "receipt_destination_invalid",
    label: str = "Receipt destination",
) -> None:
    for index, (
        descriptor,
        parent_descriptor,
        component,
        identity,
    ) in enumerate(handles):
        opened = os.fstat(descriptor)
        if parent_descriptor is None:
            named = os.lstat("/")
        else:
            named = os.stat(
                str(component),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        _validate_receipt_directory_trust(
            opened,
            final=index == len(handles) - 1,
            code=code,
            label=label,
        )
        if (
            _receipt_directory_identity(opened) != identity
            or _receipt_directory_identity(named) != identity
        ):
            raise DisasterRecoveryError(
                code,
                f"{label} directory chain changed while it was used.",
            )


def _open_receipt_destination_directory(
    parent: Path,
) -> list[_ReceiptDirectoryHandle]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    handles: list[_ReceiptDirectoryHandle] = []
    try:
        root_descriptor = os.open("/", directory_flags)
        handles.append((root_descriptor, None, None, ()))
        root_metadata = os.fstat(root_descriptor)
        root_identity = _receipt_directory_identity(root_metadata)
        handles[-1] = (root_descriptor, None, None, root_identity)

        components = parent.parts[1:]
        _validate_receipt_directory_trust(
            root_metadata,
            final=not components,
            code="receipt_destination_invalid",
            label="Receipt destination",
        )
        for component_index, component in enumerate(components):
            parent_descriptor = handles[-1][0]
            created = False
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(
                        component,
                        mode=0o700,
                        dir_fd=parent_descriptor,
                    )
                    created = True
                except FileExistsError:
                    pass
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
            handles.append(
                (child_descriptor, parent_descriptor, component, ())
            )
            child_metadata = os.fstat(child_descriptor)
            child_identity = _receipt_directory_identity(child_metadata)
            handles[-1] = (
                child_descriptor,
                parent_descriptor,
                component,
                child_identity,
            )
            _validate_receipt_directory_trust(
                child_metadata,
                final=component_index == len(components) - 1,
                code="receipt_destination_invalid",
                label="Receipt destination",
            )
            named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if _receipt_directory_identity(named) != child_identity:
                raise DisasterRecoveryError(
                    "receipt_destination_invalid",
                    "Receipt destination directory changed while it was opened.",
                )
            if created:
                os.fsync(parent_descriptor)

        _revalidate_receipt_directory_chain(handles)
        return handles
    except BaseException:
        _close_receipt_directory_handles(handles)
        raise


def _validate_receipt_file(
    value: os.stat_result,
    *,
    code: str,
    label: str,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid not in {0, os.geteuid()}
        or value.st_mode
        & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or value.st_nlink != 1
    ):
        raise DisasterRecoveryError(
            code,
            f"{label} must be a trusted-owner, singly linked regular file "
            "that is immutable to peers.",
        )


def _unique_receipt_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateReceiptKey(key)
        result[key] = value
    return result


def _reject_receipt_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _read_stable_receipt_json(
    path: Path,
    *,
    code: str,
    label: str,
) -> tuple[object, Path]:
    candidate = _canonical_receipt_path(path)
    if candidate == Path("/"):
        raise DisasterRecoveryError(code, f"{label} path is invalid.")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_handles: list[
        tuple[int, int | None, str | None, tuple[int, ...]]
    ] = []
    descriptor = -1
    try:
        try:
            root_descriptor = os.open("/", directory_flags)
            directory_handles.append(
                (root_descriptor, None, None, ())
            )
            root_identity = _receipt_directory_identity(
                os.fstat(root_descriptor)
            )
            directory_handles[-1] = (
                root_descriptor,
                None,
                None,
                root_identity,
            )
            for component in candidate.parts[1:-1]:
                parent_descriptor = directory_handles[-1][0]
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
                directory_handles.append(
                    (
                        child_descriptor,
                        parent_descriptor,
                        component,
                        (),
                    )
                )
                child_identity = _receipt_directory_identity(
                    os.fstat(child_descriptor)
                )
                directory_handles[-1] = (
                    child_descriptor,
                    parent_descriptor,
                    component,
                    child_identity,
                )
                named = os.stat(
                    component,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(named.st_mode)
                    or _receipt_directory_identity(named) != child_identity
                ):
                    raise DisasterRecoveryError(
                        code,
                        f"{label} path changed while it was opened.",
                    )

            _revalidate_receipt_directory_chain(
                directory_handles,
                code=code,
                label=f"{label} path",
            )
            filename = candidate.name
            parent_descriptor = directory_handles[-1][0]
            descriptor = os.open(
                filename,
                file_flags,
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            _validate_receipt_file(opened, code=code, label=label)
            opened_identity = _receipt_file_identity(opened)
            named = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if _receipt_file_identity(named) != opened_identity:
                raise DisasterRecoveryError(
                    code,
                    f"{label} changed while it was opened.",
                )

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RECEIPT_BYTES:
                    raise DisasterRecoveryError(
                        code,
                        f"{label} exceeds the bounded receipt size.",
                    )
                chunks.append(chunk)

            after_opened = os.fstat(descriptor)
            after_named = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _receipt_file_identity(after_opened) != opened_identity
                or _receipt_file_identity(after_named) != opened_identity
            ):
                raise DisasterRecoveryError(
                    code,
                    f"{label} changed while it was read.",
                )
            _revalidate_receipt_directory_chain(
                directory_handles,
                code=code,
                label=f"{label} path",
            )
        except DisasterRecoveryError:
            raise
        except OSError as exc:
            raise DisasterRecoveryError(
                code,
                f"{label} cannot be opened as a stable regular file: "
                f"{candidate}",
            ) from exc

        try:
            loaded = json.loads(
                b"".join(chunks).decode("utf-8"),
                object_pairs_hook=_unique_receipt_object,
                parse_constant=_reject_receipt_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise DisasterRecoveryError(
                code,
                f"{label} is not unambiguous UTF-8 JSON.",
            ) from exc
        return loaded, candidate
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        _close_receipt_directory_handles(directory_handles)


def _atomic_receipt(path: Path, payload: Mapping[str, object]) -> None:
    destination = _canonical_receipt_path(path)
    parent = destination.parent
    if destination == Path("/") or destination.name in {"", ".", ".."}:
        raise DisasterRecoveryError(
            "receipt_destination_invalid",
            "Receipt destination path is invalid.",
        )
    try:
        encoded = (
            json.dumps(
                dict(payload),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DisasterRecoveryError(
            "receipt_destination_invalid",
            "Receipt payload is not finite JSON.",
        ) from exc

    directory_handles: list[_ReceiptDirectoryHandle] = []
    descriptor = -1
    temporary_name: str | None = None
    try:
        directory_handles = _open_receipt_destination_directory(parent)
        parent_descriptor = directory_handles[-1][0]
        staging_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        for _ in range(64):
            candidate_name = (
                f".{destination.name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                descriptor = os.open(
                    candidate_name,
                    staging_flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate_name
            break
        if descriptor < 0 or temporary_name is None:
            raise DisasterRecoveryError(
                "receipt_destination_invalid",
                "A unique receipt staging file could not be allocated.",
            )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        opened = os.fstat(descriptor)
        _validate_receipt_file(
            opened,
            code="receipt_destination_invalid",
            label="Receipt staging file",
        )
        staged_identity = _receipt_file_identity(opened)
        staged_named = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _receipt_file_identity(staged_named) != staged_identity:
            raise DisasterRecoveryError(
                "receipt_destination_invalid",
                "Receipt staging file changed before publication.",
            )
        _revalidate_receipt_directory_chain(directory_handles)
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        published_identity = _receipt_file_identity(os.fstat(descriptor))
        published_named = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _receipt_file_identity(published_named) != published_identity:
            raise DisasterRecoveryError(
                "receipt_destination_invalid",
                "Receipt destination changed during publication.",
            )
        _revalidate_receipt_directory_chain(directory_handles)
        os.fsync(parent_descriptor)
    except DisasterRecoveryError:
        raise
    except OSError as exc:
        raise DisasterRecoveryError(
            "receipt_destination_invalid",
            "Receipt destination cannot be published safely.",
        ) from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_name is not None and directory_handles:
            with suppress(OSError):
                os.unlink(
                    temporary_name,
                    dir_fd=directory_handles[-1][0],
                )
        _close_receipt_directory_handles(directory_handles)


def _load_receipt(path: Path) -> dict[str, object]:
    loaded, _ = _read_stable_receipt_json(
        path,
        code="backup_receipt_invalid",
        label="Backup receipt",
    )
    if (
        not isinstance(loaded, dict)
        or loaded.get("schema") != RECEIPT_SCHEMA
        or loaded.get("status") != "pass"
        or loaded.get("operation") != "backup"
    ):
        raise DisasterRecoveryError("backup_receipt_invalid", "Backup receipt is not a passing backup receipt.")
    return dict(loaded)


def _load_passing_operation_receipt(path: Path, *, operation: str) -> dict[str, object]:
    loaded, _ = _read_stable_receipt_json(
        path,
        code="dr_receipt_invalid",
        label=f"{operation.replace('_', ' ').title()} receipt",
    )
    if (
        not isinstance(loaded, dict)
        or loaded.get("schema") != RECEIPT_SCHEMA
        or loaded.get("status") != "pass"
        or loaded.get("operation") != operation
    ):
        raise DisasterRecoveryError(
            "dr_receipt_invalid",
            f"DR receipt is not a passing {operation} receipt.",
        )
    return dict(loaded)


def _temp_file(directory: Path, *, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".propertyquarry-dr-", suffix=suffix, dir=str(directory))
    os.close(descriptor)
    path = Path(raw_path)
    _private_file(path)
    return path


def _base_receipt(*, operation: str, started_epoch: float, runtime_mode: str) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "operation": operation,
        "status": "running",
        "runtime_mode": runtime_mode,
        "started_at": _utc_iso(started_epoch),
    }


def _release_identity(
    environ: Mapping[str, str],
    *,
    required: bool,
) -> dict[str, str]:
    commit_sha = str(environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip().lower()
    image_digest = str(environ.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST") or "").strip().lower()
    if required and (not commit_sha or not image_digest):
        raise DisasterRecoveryError(
            "release_identity_required",
            "A full PropertyQuarry release commit SHA and sha256 image digest are required.",
        )
    if commit_sha and not _GIT_COMMIT_RE.fullmatch(commit_sha):
        raise DisasterRecoveryError(
            "release_commit_invalid",
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA must be a full 40-character Git SHA.",
        )
    if image_digest and not _IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise DisasterRecoveryError(
            "release_image_digest_invalid",
            "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST must be an immutable sha256 image digest.",
        )
    return {"git_commit_sha": commit_sha, "image_digest": image_digest}


def _validated_receipt_release(payload: object, *, label: str) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "release_identity_missing",
            f"{label} release identity is missing.",
        )
    return _release_identity(
        {
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA": str(payload.get("git_commit_sha") or ""),
            "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": str(payload.get("image_digest") or ""),
        },
        required=True,
    )


def _expected_schema_ledger() -> dict[str, object]:
    try:
        from app.product.property_search_schema import (
            PROPERTY_SEARCH_MIGRATIONS,
            SCHEMA_COMPONENT,
            SCHEMA_LEDGER_TABLE,
        )
    except Exception as exc:
        raise DisasterRecoveryError(
            "schema_contract_unavailable",
            "The PropertyQuarry source migration contract could not be loaded.",
        ) from exc
    migrations = [
        {
            "version": int(migration.version),
            "migration_name": str(migration.name),
            "checksum_sha256": str(migration.checksum).lower(),
        }
        for migration in PROPERTY_SEARCH_MIGRATIONS
    ]
    payload: dict[str, object] = {
        "component": str(SCHEMA_COMPONENT),
        "ledger_table": str(SCHEMA_LEDGER_TABLE),
        "ledger_present": True,
        "current_version": migrations[-1]["version"] if migrations else 0,
        "migrations": migrations,
    }
    payload["fingerprint_sha256"] = _schema_ledger_fingerprint(payload)
    return payload


def _schema_ledger_fingerprint(payload: Mapping[str, object]) -> str:
    canonical = {
        "component": str(payload.get("component") or ""),
        "ledger_table": str(payload.get("ledger_table") or ""),
        "ledger_present": payload.get("ledger_present") is True,
        "current_version": int(payload.get("current_version") or 0),
        "migrations": list(payload.get("migrations") or []),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validated_schema_ledger(
    payload: object,
    *,
    label: str,
    require_current: bool,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "schema_ledger_missing",
            f"{label} migration ledger evidence is missing.",
        )
    migrations_raw = payload.get("migrations")
    if not isinstance(migrations_raw, list):
        raise DisasterRecoveryError(
            "schema_ledger_invalid",
            f"{label} migration ledger rows are invalid.",
        )
    migrations: list[dict[str, object]] = []
    for row in migrations_raw:
        if not isinstance(row, Mapping):
            raise DisasterRecoveryError(
                "schema_ledger_invalid",
                f"{label} migration ledger contains an invalid row.",
            )
        try:
            version = int(row.get("version") or 0)
        except Exception as exc:
            raise DisasterRecoveryError(
                "schema_ledger_invalid",
                f"{label} migration ledger version is invalid.",
            ) from exc
        migration_name = str(row.get("migration_name") or "").strip()
        checksum = str(row.get("checksum_sha256") or "").strip().lower()
        if version <= 0 or not migration_name or not _SHA256_RE.fullmatch(checksum):
            raise DisasterRecoveryError(
                "schema_ledger_invalid",
                f"{label} migration ledger row is incomplete.",
            )
        migrations.append(
            {
                "version": version,
                "migration_name": migration_name,
                "checksum_sha256": checksum,
            }
        )
    ledger_present = payload.get("ledger_present")
    if not isinstance(ledger_present, bool):
        raise DisasterRecoveryError(
            "schema_ledger_presence_missing",
            f"{label} migration ledger presence evidence is missing.",
        )
    try:
        current_version = int(payload.get("current_version") or 0)
    except Exception as exc:
        raise DisasterRecoveryError(
            "schema_ledger_version_invalid",
            f"{label} current migration version is invalid.",
        ) from exc
    observed_version = int(migrations[-1]["version"]) if migrations else 0
    if current_version != observed_version or (not ledger_present and migrations):
        raise DisasterRecoveryError(
            "schema_ledger_version_mismatch",
            f"{label} migration ledger version does not match its ordered rows.",
        )
    normalized: dict[str, object] = {
        "component": str(payload.get("component") or "").strip(),
        "ledger_table": str(payload.get("ledger_table") or "").strip(),
        "ledger_present": ledger_present,
        "current_version": current_version,
        "migrations": migrations,
    }
    normalized["fingerprint_sha256"] = _schema_ledger_fingerprint(normalized)
    declared_fingerprint = str(payload.get("fingerprint_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(declared_fingerprint):
        raise DisasterRecoveryError(
            "schema_ledger_fingerprint_missing",
            f"{label} migration ledger fingerprint is missing or invalid.",
        )
    if declared_fingerprint != normalized["fingerprint_sha256"]:
        raise DisasterRecoveryError(
            "schema_ledger_fingerprint_mismatch",
            f"{label} migration ledger fingerprint is invalid.",
        )
    expected = _expected_schema_ledger()
    expected_migrations = list(expected["migrations"])
    expected_prefix = expected_migrations[: len(migrations)]
    source_contract_valid = (
        normalized["component"] == expected["component"]
        and normalized["ledger_table"] == expected["ledger_table"]
        and migrations == expected_prefix
        and len(migrations) <= len(expected_migrations)
    )
    if not source_contract_valid or (require_current and normalized != expected):
        raise DisasterRecoveryError(
            "schema_ledger_release_mismatch",
            (
                f"{label} migration ledger does not match the exact release schema."
                if require_current
                else f"{label} migration ledger is not a valid ordered prefix of the release schema."
            ),
            details={
                "expected_schema_fingerprint": expected["fingerprint_sha256"],
                "observed_schema_fingerprint": normalized["fingerprint_sha256"],
            },
        )
    return normalized


def _critical_data_tables_for_schema_version(
    governing_schema_version: int | None = None,
) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
    latest_version = int(_expected_schema_ledger()["current_version"])
    resolved_version = (
        latest_version
        if governing_schema_version is None
        else governing_schema_version
    )
    if (
        isinstance(resolved_version, bool)
        or not isinstance(resolved_version, int)
        or resolved_version < 0
        or resolved_version > latest_version
    ):
        raise DisasterRecoveryError(
            "critical_data_schema_version_invalid",
            "Critical-data evidence must be governed by a valid release schema version.",
        )
    if resolved_version < CRITICAL_DATA_PRIVACY_SCHEMA_VERSION:
        return CRITICAL_DATA_PRE_PRIVACY_TABLES
    if resolved_version < CRITICAL_DATA_ADMISSION_CAPACITY_SCHEMA_VERSION:
        return CRITICAL_DATA_PRIVACY_TABLES
    return CRITICAL_DATA_TABLES


def _critical_data_contract(
    governing_schema_version: int | None = None,
) -> dict[str, object]:
    tables_for_schema = _critical_data_tables_for_schema_version(
        governing_schema_version
    )
    resolved_version = (
        int(_expected_schema_ledger()["current_version"])
        if governing_schema_version is None
        else governing_schema_version
    )
    privacy_lifecycle_required = (
        resolved_version >= CRITICAL_DATA_PRIVACY_SCHEMA_VERSION
    )
    admission_capacity_required = (
        resolved_version >= CRITICAL_DATA_ADMISSION_CAPACITY_SCHEMA_VERSION
    )
    tables = [
        {
            "schema": CRITICAL_DATA_SCHEMA,
            "table": table,
            "identity_columns": list(identity_columns),
            "data_required": data_required,
        }
        for table, identity_columns, data_required in tables_for_schema
    ]
    identity = {
        "contract_name": CRITICAL_DATA_CONTRACT_NAME,
        "contract_version": CRITICAL_DATA_CONTRACT_VERSION,
        "evidence_version": CRITICAL_DATA_EVIDENCE_VERSION,
        "fingerprint_algorithm": CRITICAL_DATA_FINGERPRINT_ALGORITHM,
        "chunk_size": CRITICAL_DATA_CHUNK_SIZE,
        "max_row_bytes": CRITICAL_DATA_MAX_ROW_BYTES,
        "max_chunks": CRITICAL_DATA_MAX_CHUNKS,
        "max_supported_rows": CRITICAL_DATA_MAX_SUPPORTED_ROWS,
        "governing_schema_version": resolved_version,
        "table_set_version": (
            CRITICAL_DATA_ADMISSION_CAPACITY_TABLE_SET_VERSION
            if admission_capacity_required
            else (
                CRITICAL_DATA_PRIVACY_TABLE_SET_VERSION
                if privacy_lifecycle_required
                else CRITICAL_DATA_PRE_PRIVACY_TABLE_SET_VERSION
            )
        ),
        "privacy_lifecycle_required": privacy_lifecycle_required,
        "admission_capacity_required": admission_capacity_required,
        "admission_capacity_limits": (
            dict(CRITICAL_DATA_ADMISSION_CAPACITY_LIMITS)
            if admission_capacity_required
            else {}
        ),
        "tables": tables,
    }
    identity["contract_fingerprint_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return identity


def _critical_chunk_leaf_hash(table: str, chunk: Mapping[str, object]) -> bytes:
    payload = {
        "domain": "propertyquarry.critical_data.chunk_leaf.v2",
        "table": table,
        "chunk_index": chunk["chunk_index"],
        "row_count": chunk["row_count"],
        "max_row_bytes_observed": chunk["max_row_bytes_observed"],
        "chunk_sha256": chunk["chunk_sha256"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _critical_merkle_root(table: str, chunks: Sequence[Mapping[str, object]]) -> str:
    if not chunks:
        return hashlib.sha256(
            f"propertyquarry.critical_data.empty.v2\0{table}".encode("utf-8")
        ).hexdigest()
    level = [_critical_chunk_leaf_hash(table, chunk) for chunk in chunks]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(
                b"propertyquarry.critical_data.merkle_node.v2\0"
                + level[index]
                + level[index + 1]
            ).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def _strict_nonnegative_int(value: object, *, code: str, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DisasterRecoveryError(code, message)
    return value


def _validated_admission_capacity_state(
    payload: object,
    *,
    label: str,
) -> list[dict[str, int | str]]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise DisasterRecoveryError(
            "critical_data_admission_capacity_state_invalid",
            f"{label} admission capacity state must contain exactly two canonical rows.",
        )
    normalized: list[dict[str, int | str]] = []
    for expected_key, row in zip(
        sorted(CRITICAL_DATA_ADMISSION_CAPACITY_LIMITS),
        payload,
        strict=True,
    ):
        if not isinstance(row, Mapping) or set(row) != {
            "capacity_key",
            "row_count",
            "row_limit",
        }:
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_state_invalid",
                f"{label} admission capacity state row is invalid.",
            )
        capacity_key = str(row.get("capacity_key") or "")
        row_count = _strict_nonnegative_int(
            row.get("row_count"),
            code="critical_data_admission_capacity_state_invalid",
            message=f"{label} admission capacity count is invalid for {expected_key}.",
        )
        row_limit = _strict_nonnegative_int(
            row.get("row_limit"),
            code="critical_data_admission_capacity_state_invalid",
            message=f"{label} admission capacity limit is invalid for {expected_key}.",
        )
        expected_limit = CRITICAL_DATA_ADMISSION_CAPACITY_LIMITS[expected_key]
        if (
            capacity_key != expected_key
            or row_limit != expected_limit
            or row_count > row_limit
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_state_invalid",
                f"{label} admission capacity state does not match the v17 hard limits.",
            )
        normalized.append(
            {
                "capacity_key": capacity_key,
                "row_count": row_count,
                "row_limit": row_limit,
            }
        )
    return normalized


def _validated_critical_data_evidence(
    payload: object,
    *,
    label: str,
    governing_schema_version: int | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "critical_data_evidence_missing",
            f"{label} critical-data evidence is missing.",
        )
    expected = _critical_data_contract(governing_schema_version)
    for key in (
        "contract_name",
        "contract_version",
        "evidence_version",
        "fingerprint_algorithm",
        "contract_fingerprint_sha256",
        "chunk_size",
        "max_row_bytes",
        "max_chunks",
        "max_supported_rows",
        "governing_schema_version",
        "table_set_version",
        "privacy_lifecycle_required",
        "admission_capacity_required",
        "admission_capacity_limits",
    ):
        if payload.get(key) != expected[key]:
            raise DisasterRecoveryError(
                "critical_data_contract_mismatch",
                f"{label} critical-data evidence does not match the release-controlled contract.",
            )
    rows = payload.get("tables")
    expected_tables = _critical_data_tables_for_schema_version(
        int(expected["governing_schema_version"])
    )
    if not isinstance(rows, list) or len(rows) != len(expected_tables):
        raise DisasterRecoveryError(
            "critical_data_evidence_invalid",
            f"{label} critical-data table evidence is incomplete.",
        )
    normalized_rows: list[dict[str, object]] = []
    for row, (table, identity_columns, data_required) in zip(
        rows,
        expected_tables,
        strict=True,
    ):
        if not isinstance(row, Mapping):
            raise DisasterRecoveryError(
                "critical_data_evidence_invalid",
                f"{label} critical-data evidence contains an invalid table row.",
            )
        if (
            str(row.get("schema") or "") != CRITICAL_DATA_SCHEMA
            or str(row.get("table") or "") != table
            or list(row.get("identity_columns") or []) != list(identity_columns)
            or row.get("data_required") is not data_required
        ):
            raise DisasterRecoveryError(
                "critical_data_contract_mismatch",
                f"{label} critical-data table contract is not release-controlled.",
            )
        row_count = _strict_nonnegative_int(
            row.get("row_count"),
            code="critical_data_count_invalid",
            message=f"{label} row count is invalid for {table}.",
        )
        if (
            table == "propertyquarry_admission_quota_buckets"
            and row_count > CRITICAL_DATA_ADMISSION_QUOTA_MAX_ROWS
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_exceeded",
                f"{label} quota admission state exceeds the schema-v17 hard limit.",
            )
        if (
            table == "propertyquarry_admission_leases"
            and row_count > CRITICAL_DATA_ADMISSION_LEASE_MAX_ROWS
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_exceeded",
                f"{label} lease admission state exceeds the schema-v17 hard limit.",
            )
        if (
            table == "propertyquarry_admission_capacity_state"
            and row_count != 2
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_state_invalid",
                f"{label} admission capacity state must contain exactly two rows.",
            )
        capacity_state: list[dict[str, int | str]] | None = None
        if table == "propertyquarry_admission_capacity_state":
            capacity_state = _validated_admission_capacity_state(
                row.get("capacity_state"),
                label=label,
            )
        elif "capacity_state" in row:
            raise DisasterRecoveryError(
                "critical_data_contract_mismatch",
                f"{label} capacity-state evidence is attached to the wrong table.",
            )
        chunk_count = _strict_nonnegative_int(
            row.get("chunk_count"),
            code="critical_data_chunk_count_invalid",
            message=f"{label} chunk count is invalid for {table}.",
        )
        if (
            row.get("evidence_version") != CRITICAL_DATA_EVIDENCE_VERSION
            or row.get("chunk_size") != CRITICAL_DATA_CHUNK_SIZE
            or row.get("max_row_bytes") != CRITICAL_DATA_MAX_ROW_BYTES
            or row.get("max_chunks") != CRITICAL_DATA_MAX_CHUNKS
            or row.get("max_supported_rows") != CRITICAL_DATA_MAX_SUPPORTED_ROWS
        ):
            raise DisasterRecoveryError(
                "critical_data_contract_mismatch",
                f"{label} chunk bounds are not release-controlled for {table}.",
            )
        if row_count > CRITICAL_DATA_MAX_SUPPORTED_ROWS or chunk_count > CRITICAL_DATA_MAX_CHUNKS:
            raise DisasterRecoveryError(
                "critical_data_scale_bound_exceeded",
                f"{label} critical-data evidence exceeds the bounded Merkle contract for {table}.",
            )
        chunks = row.get("chunks")
        if not isinstance(chunks, list) or len(chunks) != chunk_count:
            raise DisasterRecoveryError(
                "critical_data_chunks_invalid",
                f"{label} chunk evidence is incomplete for {table}.",
            )
        normalized_chunks: list[dict[str, object]] = []
        total_chunk_rows = 0
        for expected_index, chunk in enumerate(chunks):
            if not isinstance(chunk, Mapping):
                raise DisasterRecoveryError(
                    "critical_data_chunks_invalid",
                    f"{label} contains an invalid chunk for {table}.",
                )
            chunk_index = _strict_nonnegative_int(
                chunk.get("chunk_index"),
                code="critical_data_chunks_invalid",
                message=f"{label} chunk index is invalid for {table}.",
            )
            chunk_rows = _strict_nonnegative_int(
                chunk.get("row_count"),
                code="critical_data_chunks_invalid",
                message=f"{label} chunk row count is invalid for {table}.",
            )
            max_row_bytes_observed = _strict_nonnegative_int(
                chunk.get("max_row_bytes_observed"),
                code="critical_data_chunks_invalid",
                message=f"{label} chunk row-size evidence is invalid for {table}.",
            )
            chunk_sha256 = str(chunk.get("chunk_sha256") or "").strip().lower()
            expected_rows = CRITICAL_DATA_CHUNK_SIZE
            if expected_index == chunk_count - 1:
                expected_rows = row_count - (CRITICAL_DATA_CHUNK_SIZE * expected_index)
            if (
                chunk_index != expected_index
                or chunk_rows != expected_rows
                or chunk_rows <= 0
                or chunk_rows > CRITICAL_DATA_CHUNK_SIZE
                or max_row_bytes_observed <= 0
                or max_row_bytes_observed > CRITICAL_DATA_MAX_ROW_BYTES
                or not _SHA256_RE.fullmatch(chunk_sha256)
            ):
                raise DisasterRecoveryError(
                    "critical_data_chunks_invalid",
                    f"{label} bounded chunk evidence is invalid for {table}.",
                )
            total_chunk_rows += chunk_rows
            normalized_chunks.append(
                {
                    "chunk_index": chunk_index,
                    "row_count": chunk_rows,
                    "max_row_bytes_observed": max_row_bytes_observed,
                    "chunk_sha256": chunk_sha256,
                }
            )
        if total_chunk_rows != row_count or chunk_count != (
            (row_count + CRITICAL_DATA_CHUNK_SIZE - 1) // CRITICAL_DATA_CHUNK_SIZE
        ):
            raise DisasterRecoveryError(
                "critical_data_chunks_invalid",
                f"{label} chunk totals do not match the row count for {table}.",
            )
        merkle_root = str(row.get("merkle_root_sha256") or "").strip().lower()
        fingerprint = str(row.get("fingerprint_sha256") or "").strip().lower()
        expected_merkle_root = _critical_merkle_root(table, normalized_chunks)
        if (
            not _SHA256_RE.fullmatch(merkle_root)
            or fingerprint != merkle_root
            or merkle_root != expected_merkle_root
        ):
            raise DisasterRecoveryError(
                "critical_data_fingerprint_invalid",
                f"{label} Merkle-root evidence is invalid for {table}.",
            )
        if data_required and row_count <= 0:
            raise DisasterRecoveryError(
                "critical_data_required_table_empty",
                f"{label} release-critical data table is empty: {table}.",
            )
        normalized_row: dict[str, object] = {
            "schema": CRITICAL_DATA_SCHEMA,
            "table": table,
            "identity_columns": list(identity_columns),
            "data_required": data_required,
            "evidence_version": CRITICAL_DATA_EVIDENCE_VERSION,
            "chunk_size": CRITICAL_DATA_CHUNK_SIZE,
            "max_row_bytes": CRITICAL_DATA_MAX_ROW_BYTES,
            "max_chunks": CRITICAL_DATA_MAX_CHUNKS,
            "max_supported_rows": CRITICAL_DATA_MAX_SUPPORTED_ROWS,
            "row_count": row_count,
            "chunk_count": chunk_count,
            "chunks": normalized_chunks,
            "merkle_root_sha256": merkle_root,
            "fingerprint_sha256": fingerprint,
        }
        if capacity_state is not None:
            normalized_row["capacity_state"] = capacity_state
        normalized_rows.append(normalized_row)
    if expected["admission_capacity_required"] is True:
        normalized_by_table = {
            str(row["table"]): row for row in normalized_rows
        }
        capacity_rows = normalized_by_table[
            "propertyquarry_admission_capacity_state"
        ]["capacity_state"]
        if not isinstance(capacity_rows, list):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_state_invalid",
                f"{label} admission capacity state is missing after validation.",
            )
        capacity_counts = {
            str(row["capacity_key"]): int(row["row_count"])
            for row in capacity_rows
        }
        actual_counts = {
            "lease": int(
                normalized_by_table[
                    "propertyquarry_admission_leases"
                ]["row_count"]
            ),
            "quota": int(
                normalized_by_table[
                    "propertyquarry_admission_quota_buckets"
                ]["row_count"]
            ),
        }
        if capacity_counts != actual_counts:
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_counter_drift",
                f"{label} admission capacity counters do not match the snapshot-bound table counts.",
            )
    return {
        "contract_name": expected["contract_name"],
        "contract_version": expected["contract_version"],
        "evidence_version": expected["evidence_version"],
        "fingerprint_algorithm": expected["fingerprint_algorithm"],
        "contract_fingerprint_sha256": expected["contract_fingerprint_sha256"],
        "chunk_size": expected["chunk_size"],
        "max_row_bytes": expected["max_row_bytes"],
        "max_chunks": expected["max_chunks"],
        "max_supported_rows": expected["max_supported_rows"],
        "governing_schema_version": expected["governing_schema_version"],
        "table_set_version": expected["table_set_version"],
        "privacy_lifecycle_required": expected["privacy_lifecycle_required"],
        "admission_capacity_required": expected["admission_capacity_required"],
        "admission_capacity_limits": expected["admission_capacity_limits"],
        "tables": normalized_rows,
    }


def _critical_data_evidence_from_database(
    *,
    label: str,
    step: str,
    psql: str,
    database_url: str,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    snapshot_id: str | None = None,
    governing_schema_version: int | None = None,
) -> dict[str, object]:
    contract = _critical_data_contract(governing_schema_version)
    tables_for_schema = _critical_data_tables_for_schema_version(
        int(contract["governing_schema_version"])
    )
    evidence_rows: list[dict[str, object]] = []
    for table, identity_columns, data_required in tables_for_schema:
        if not _SAFE_TABLE_RE.fullmatch(table) or any(
            not _SAFE_TABLE_RE.fullmatch(column) for column in identity_columns
        ):
            raise DisasterRecoveryError(
                "critical_data_contract_invalid",
                "Release-controlled critical-data contract contains an unsafe identifier.",
            )
        preflight_sql = (
            "SET TIME ZONE 'UTC'; "
            f"/* propertyquarry_critical_data:{table}:row_bound_preflight */ "
            "SELECT COUNT(*)::bigint FROM ("
            f'SELECT 1 FROM "{CRITICAL_DATA_SCHEMA}"."{table}" AS source_row '
            f"LIMIT {CRITICAL_DATA_MAX_SUPPORTED_ROWS + 1}"
            ") AS bounded_preflight;"
        )
        preflight_raw = _psql_scalar(
            step=f"{step}_{table}_row_bound_preflight",
            psql=psql,
            database_url=database_url,
            sql=preflight_sql,
            env=env,
            runner=runner,
            commands=commands,
            snapshot_id=snapshot_id,
        )
        if re.fullmatch(r"(?:0|[1-9][0-9]*)", preflight_raw) is None:
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} critical-data row-bound preflight is invalid for {table}.",
            )
        preflight_row_count = int(preflight_raw)
        if preflight_row_count > CRITICAL_DATA_MAX_SUPPORTED_ROWS:
            raise DisasterRecoveryError(
                "critical_data_scale_bound_exceeded",
                f"{label} critical-data table exceeds the bounded Merkle contract for {table}.",
            )
        if (
            table == "propertyquarry_admission_quota_buckets"
            and preflight_row_count > CRITICAL_DATA_ADMISSION_QUOTA_MAX_ROWS
        ) or (
            table == "propertyquarry_admission_leases"
            and preflight_row_count > CRITICAL_DATA_ADMISSION_LEASE_MAX_ROWS
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_exceeded",
                f"{label} admission state exceeds the schema-v17 hard limit for {table}.",
            )
        if (
            table == "propertyquarry_admission_capacity_state"
            and preflight_row_count != 2
        ):
            raise DisasterRecoveryError(
                "critical_data_admission_capacity_state_invalid",
                f"{label} admission capacity state must contain exactly two rows.",
            )
        identity_projection_sql = ", ".join(
            f'source_row."{column}"' for column in identity_columns
        )
        identity_sql = ", ".join(
            (
                f'digested_row."{column}" COLLATE "C"'
                if (table, column) in _CRITICAL_DATA_TEXT_IDENTITIES
                else f'digested_row."{column}"'
            )
            for column in identity_columns
        )
        capacity_state_projection = ""
        if table == "propertyquarry_admission_capacity_state":
            capacity_state_projection = (
                ", 'capacity_state', COALESCE((SELECT json_agg(json_build_object("
                "'capacity_key', capacity_key, 'row_count', row_count, "
                "'row_limit', row_limit) ORDER BY capacity_key) "
                f'FROM "{CRITICAL_DATA_SCHEMA}"."{table}"), \'[]\'::json)'
            )
        sql = (
            "SET TIME ZONE 'UTC'; "
            "SET work_mem TO '16MB'; "
            f"/* propertyquarry_critical_data:{table} */ "
            "WITH digested_rows AS MATERIALIZED ("
            f"SELECT {identity_projection_sql}, "
            "octet_length(serialized_row.row_bytes)::bigint AS row_size_bytes, "
            "encode(sha256(serialized_row.row_bytes), 'hex') AS row_sha256 "
            f'FROM "{CRITICAL_DATA_SCHEMA}"."{table}" AS source_row '
            "CROSS JOIN LATERAL (SELECT "
            "convert_to(to_jsonb(source_row)::text, 'UTF8') AS row_bytes "
            "OFFSET 0) AS serialized_row"
            "), canonical_rows AS MATERIALIZED ("
            f"SELECT row_number() OVER (ORDER BY {identity_sql}, "
            "digested_row.row_sha256 COLLATE \"C\")::bigint AS ordinal, "
            "digested_row.row_size_bytes, digested_row.row_sha256 "
            "FROM digested_rows AS digested_row"
            "), bounded_rows AS MATERIALIZED ("
            "SELECT ordinal, row_size_bytes, row_sha256, "
            f"((ordinal - 1) / {CRITICAL_DATA_CHUNK_SIZE})::bigint AS chunk_index "
            f"FROM canonical_rows WHERE row_size_bytes <= {CRITICAL_DATA_MAX_ROW_BYTES}"
            "), bounded_chunks AS MATERIALIZED ("
            "SELECT chunk_index, COUNT(*)::bigint AS row_count, "
            "MAX(row_size_bytes)::bigint AS max_row_bytes_observed, "
            "encode(sha256(convert_to(string_agg(row_sha256, '' ORDER BY ordinal), "
            "'UTF8')), 'hex') AS chunk_sha256 FROM bounded_rows GROUP BY chunk_index"
            ") SELECT json_build_object("
            f"'evidence_version', {CRITICAL_DATA_EVIDENCE_VERSION}, "
            "'row_count', (SELECT COUNT(*)::bigint FROM canonical_rows), "
            "'oversized_row_count', (SELECT COUNT(*)::bigint FROM canonical_rows "
            f"WHERE row_size_bytes > {CRITICAL_DATA_MAX_ROW_BYTES}), "
            "'max_row_bytes_observed', (SELECT COALESCE(MAX(row_size_bytes), 0)::bigint "
            "FROM canonical_rows), "
            "'chunk_count', (SELECT CASE WHEN COUNT(*) = 0 THEN 0 "
            f"ELSE ((COUNT(*) - 1) / {CRITICAL_DATA_CHUNK_SIZE}) + 1 END FROM canonical_rows), "
            "'chunks', COALESCE((SELECT json_agg(json_build_object("
            "'chunk_index', chunk_index, 'row_count', row_count, "
            "'max_row_bytes_observed', max_row_bytes_observed, "
            "'chunk_sha256', chunk_sha256) ORDER BY chunk_index) FROM bounded_chunks), "
            f"'[]'::json){capacity_state_projection})::text;"
        )
        raw = _psql_scalar(
            step=f"{step}_{table}",
            psql=psql,
            database_url=database_url,
            sql=sql,
            env=env,
            runner=runner,
            commands=commands,
            snapshot_id=snapshot_id,
        )
        try:
            observed = json.loads(raw)
        except Exception as exc:
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} critical-data query returned invalid JSON for {table}.",
            ) from exc
        if not isinstance(observed, Mapping):
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} critical-data query returned an invalid object for {table}.",
            )
        observed_row_count = _strict_nonnegative_int(
            observed.get("row_count"),
            code="critical_data_query_invalid",
            message=f"{label} row count is invalid for {table}.",
        )
        if observed_row_count != preflight_row_count:
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} critical-data row count does not match its bounded preflight for {table}.",
            )
        oversized_row_count = _strict_nonnegative_int(
            observed.get("oversized_row_count"),
            code="critical_data_query_invalid",
            message=f"{label} oversized-row count is invalid for {table}.",
        )
        max_row_bytes_observed = _strict_nonnegative_int(
            observed.get("max_row_bytes_observed"),
            code="critical_data_query_invalid",
            message=f"{label} maximum row size is invalid for {table}.",
        )
        if oversized_row_count:
            raise DisasterRecoveryError(
                "critical_data_row_too_large",
                f"{label} contains a canonical row larger than the release bound in {table}.",
                details={
                    "table": table,
                    "max_row_bytes": CRITICAL_DATA_MAX_ROW_BYTES,
                    "max_row_bytes_observed": max_row_bytes_observed,
                    "oversized_row_count": oversized_row_count,
                },
            )
        chunks = observed.get("chunks")
        if not isinstance(chunks, list):
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} chunk query returned invalid evidence for {table}.",
            )
        normalized_chunks = [dict(chunk) if isinstance(chunk, Mapping) else chunk for chunk in chunks]
        try:
            merkle_root = _critical_merkle_root(
                table,
                [chunk for chunk in normalized_chunks if isinstance(chunk, Mapping)],
            )
        except Exception as exc:
            raise DisasterRecoveryError(
                "critical_data_query_invalid",
                f"{label} chunk query returned incomplete evidence for {table}.",
            ) from exc
        evidence_row: dict[str, object] = {
            "schema": CRITICAL_DATA_SCHEMA,
            "table": table,
            "identity_columns": list(identity_columns),
            "data_required": data_required,
            "evidence_version": observed.get("evidence_version"),
            "chunk_size": CRITICAL_DATA_CHUNK_SIZE,
            "max_row_bytes": CRITICAL_DATA_MAX_ROW_BYTES,
            "max_chunks": CRITICAL_DATA_MAX_CHUNKS,
            "max_supported_rows": CRITICAL_DATA_MAX_SUPPORTED_ROWS,
            "row_count": observed_row_count,
            "chunk_count": observed.get("chunk_count"),
            "chunks": normalized_chunks,
            "merkle_root_sha256": merkle_root,
            "fingerprint_sha256": merkle_root,
        }
        if table == "propertyquarry_admission_capacity_state":
            evidence_row["capacity_state"] = observed.get("capacity_state")
        evidence_rows.append(evidence_row)
    return _validated_critical_data_evidence(
        {**contract, "tables": evidence_rows},
        label=label,
        governing_schema_version=int(contract["governing_schema_version"]),
    )


def _validated_off_host_object(
    payload: object,
    *,
    artifact: Mapping[str, object],
    now_epoch: float,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "off_host_verification_required",
            "Verified off-host backup object evidence is required.",
        )
    provider = str(payload.get("provider") or "").strip().lower()
    backend = str(payload.get("backend") or "").strip().lower()
    region = str(payload.get("region") or "").strip().lower()
    bucket = str(payload.get("bucket") or payload.get("container") or "").strip()
    object_key = str(payload.get("object_key") or "").strip()
    version_id = str(payload.get("version_id") or "").strip()
    etag = _normalize_s3_etag(payload.get("etag"))
    verification_method = str(payload.get("verification_method") or "").strip()
    provider_request_id = str(payload.get("provider_request_id") or "").strip()
    object_lock_mode = str(payload.get("object_lock_mode") or "").strip().upper()
    object_lock_retain_until = str(
        payload.get("object_lock_retain_until") or ""
    ).strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    try:
        size_bytes = int(payload.get("size_bytes") or 0)
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_identity_invalid",
            "Off-host object size is invalid.",
        ) from exc
    provider_contract = REMOTE_PROVIDER_CONTRACTS.get(provider)
    if not provider_contract:
        raise DisasterRecoveryError(
            "off_host_identity_invalid",
            "Off-host provider must use a release-approved remote provider contract.",
        )
    if backend != provider_contract["backend"]:
        raise DisasterRecoveryError(
            "off_host_backend_invalid",
            "Off-host backend does not match the release-approved provider backend.",
        )
    if verification_method != provider_contract["verification_method"]:
        raise DisasterRecoveryError(
            "off_host_verification_method_invalid",
            "Off-host verification method is not provider-native and release-approved.",
        )
    if provider == "s3" and (
        not _S3_BUCKET_RE.fullmatch(bucket)
        or ".." in bucket
        or bucket.lower() in {"file", "local", "localhost"}
        or _IPV4_ADDRESS_RE.fullmatch(bucket)
        or not _AWS_REGION_RE.fullmatch(region)
    ):
        raise DisasterRecoveryError(
            "off_host_identity_invalid",
            "S3 bucket, region, or ETag does not satisfy the provider-native identity contract.",
        )
    object_key_segments = object_key.split("/")
    if (
        not object_key
        or object_key.startswith(("/", "~"))
        or "\\" in object_key
        or "://" in object_key
        or ":" in object_key
        or any(segment in {"", ".", ".."} for segment in object_key_segments)
    ):
        raise DisasterRecoveryError(
            "off_host_identity_invalid",
            "Off-host object key must be a provider key, not a local or traversal path.",
        )
    if not etag or not provider_request_id:
        raise DisasterRecoveryError(
            "off_host_identity_invalid",
            "Off-host ETag and provider request identity are required.",
        )
    if not version_id or version_id.lower() in {"latest", "null", "none", "unversioned"}:
        raise DisasterRecoveryError(
            "off_host_version_invalid",
            "Off-host object identity requires an immutable provider version ID.",
        )
    if payload.get("off_host") is not True or payload.get("object_exists") is not True:
        raise DisasterRecoveryError(
            "off_host_object_unverified",
            "The remote verifier did not prove that the off-host object exists.",
        )
    if payload.get("checksum_verified") is not True:
        raise DisasterRecoveryError(
            "off_host_checksum_unverified",
            "The remote verifier did not verify the off-host object checksum.",
        )
    try:
        retain_until_epoch = _parse_utc_iso(object_lock_retain_until)
    except DisasterRecoveryError as exc:
        raise DisasterRecoveryError(
            "off_host_immutability_invalid",
            "Off-host Object Lock retention is invalid.",
        ) from exc
    if (
        payload.get("immutability_verified") is not True
        or object_lock_mode != "COMPLIANCE"
        or retain_until_epoch
        < now_epoch + DEFAULT_OFF_HOST_MINIMUM_RETENTION_SECONDS
    ):
        raise DisasterRecoveryError(
            "off_host_immutability_unverified",
            "Off-host evidence must prove active S3 Object Lock COMPLIANCE retention.",
        )
    if payload.get("encrypted") is not True:
        raise DisasterRecoveryError(
            "off_host_encryption_unverified",
            "The off-host object is not proven encrypted.",
        )
    if artifact.get("encrypted") is not True:
        raise DisasterRecoveryError(
            "off_host_encryption_mismatch",
            "Off-host evidence cannot bind an unencrypted backup artifact.",
        )
    expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
    try:
        expected_size = int(artifact.get("size_bytes") or 0)
    except Exception:
        expected_size = 0
    if not _SHA256_RE.fullmatch(sha256) or sha256 != expected_sha256:
        raise DisasterRecoveryError(
            "off_host_checksum_mismatch",
            "Off-host object checksum does not match the encrypted backup artifact.",
        )
    if size_bytes <= 0 or size_bytes != expected_size:
        raise DisasterRecoveryError(
            "off_host_size_mismatch",
            "Off-host object size does not match the encrypted backup artifact.",
        )
    verified_at = str(payload.get("verified_at") or "").strip()
    verified_epoch = _parse_utc_iso(verified_at)
    if verified_epoch > now_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS:
        raise DisasterRecoveryError(
            "off_host_timestamp_future",
            "Off-host verification timestamp is in the future.",
        )
    return {
        "provider": provider,
        "backend": backend,
        "region": region,
        "bucket": bucket,
        "object_key": object_key,
        "version_id": version_id,
        "etag": etag,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "encrypted": True,
        "off_host": True,
        "object_exists": True,
        "checksum_verified": True,
        "immutability_verified": True,
        "object_lock_mode": "COMPLIANCE",
        "object_lock_retain_until": _utc_iso(retain_until_epoch),
        "verified_at": _utc_iso(verified_epoch),
        "verification_method": verification_method,
        "provider_request_id": provider_request_id,
    }


def execute_backup(
    *,
    artifact_path: Path,
    overwrite: bool,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    clock: Clock = time.time,
    which: Which = shutil.which,
    snapshot_connector: SnapshotConnector | None = None,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    started = clock()
    runtime_mode = str(env.get("EA_RUNTIME_MODE") or "dev").strip().lower() or "dev"
    source_url = str(env.get("PROPERTYQUARRY_BACKUP_DATABASE_URL") or env.get("DATABASE_URL") or "").strip()
    source_identity = _database_identity(source_url, label="Source")
    release_identity = _release_identity(env, required=runtime_mode == "prod")
    encryption_recipient = str(env.get("PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT") or "").strip()
    encryption_required = runtime_mode == "prod" or _truthy(env, "PROPERTYQUARRY_BACKUP_ENCRYPTION_REQUIRED")
    if encryption_required and not encryption_recipient:
        raise DisasterRecoveryError(
            "encryption_required",
            "Production backups require PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT.",
        )
    if runtime_mode == "prod" and overwrite:
        raise DisasterRecoveryError("overwrite_forbidden", "Production backup artifacts cannot be overwritten.")
    artifact = artifact_path.expanduser().resolve()
    if artifact.exists() and not overwrite:
        raise DisasterRecoveryError("artifact_exists", f"Backup artifact already exists: {artifact}")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    pg_dump = _binary(env, "PROPERTYQUARRY_PG_DUMP_BIN", "pg_dump", which)
    pg_restore = _binary(env, "PROPERTYQUARRY_PG_RESTORE_BIN", "pg_restore", which)
    psql = _binary(env, "PROPERTYQUARRY_PSQL_BIN", "psql", which)
    gpg = _binary(env, "PROPERTYQUARRY_GPG_BIN", "gpg", which) if encryption_recipient else ""
    commands: list[dict[str, object]] = []
    plain_dump = _temp_file(artifact.parent, suffix=".dump")
    encrypted_temp: Path | None = None
    try:
        with _exported_repeatable_read_snapshot(
            database_url=source_url,
            connector=snapshot_connector,
        ) as exported_snapshot:
            snapshot_id = str(exported_snapshot["snapshot_id"])
            _run_checked(
                step="pg_dump",
                command=[
                    pg_dump,
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-acl",
                    f"--snapshot={snapshot_id}",
                    "--file",
                    str(plain_dump),
                    "--dbname",
                    source_url,
                ],
                environ=env,
                runner=runner,
                commands=commands,
            )
            if not plain_dump.is_file() or plain_dump.stat().st_size <= 0:
                raise DisasterRecoveryError(
                    "backup_artifact_empty",
                    "pg_dump did not produce a non-empty artifact.",
                )
            plaintext_sha256 = _sha256(plain_dump)
            source_schema = _migration_ledger_from_database(
                label="Source",
                step="source_migration_ledger",
                psql=psql,
                database_url=source_url,
                env=env,
                runner=runner,
                commands=commands,
                require_current=False,
                snapshot_id=snapshot_id,
            )
            source_schema_version = int(source_schema["current_version"])
            source_critical_data = _critical_data_evidence_from_database(
                label="Source",
                step="source_critical_data",
                psql=psql,
                database_url=source_url,
                env=env,
                runner=runner,
                commands=commands,
                snapshot_id=snapshot_id,
                governing_schema_version=source_schema_version,
            )
            source_snapshot = _snapshot_evidence(
                exported_snapshot,
                pg_dump_plaintext_sha256=plaintext_sha256,
                governing_schema_version=source_schema_version,
            )
        _run_checked(
            step="pg_restore_list",
            command=[pg_restore, "--list", str(plain_dump)],
            environ=env,
            runner=runner,
            commands=commands,
        )
        if encryption_recipient:
            encrypted_temp = _temp_file(artifact.parent, suffix=".gpg")
            _run_checked(
                step="encrypt_backup",
                command=[
                    gpg,
                    "--batch",
                    "--yes",
                    "--trust-model",
                    "always",
                    "--output",
                    str(encrypted_temp),
                    "--encrypt",
                    "--recipient",
                    encryption_recipient,
                    str(plain_dump),
                ],
                environ=env,
                runner=runner,
                commands=commands,
            )
            if encrypted_temp.stat().st_size <= 0:
                raise DisasterRecoveryError("encrypted_artifact_empty", "GPG did not produce a non-empty artifact.")
            encrypted_temp.replace(artifact)
            encrypted_temp = None
        else:
            plain_dump.replace(artifact)
        _private_file(artifact)
        evidence_epoch = clock()
        artifact_receipt: dict[str, object] = {
            "path": str(artifact),
            "size_bytes": artifact.stat().st_size,
            "sha256": _sha256(artifact),
            "plaintext_sha256": plaintext_sha256,
            "encrypted": bool(encryption_recipient),
            "encryption": "gpg-recipient" if encryption_recipient else "none",
        }
        off_host_object = _verified_off_host_object_from_hook(
            env=env,
            runner=runner,
            commands=commands,
            artifact=artifact_receipt,
            artifact_path=artifact,
            now_epoch=evidence_epoch,
            required=runtime_mode == "prod",
        )
        completed = clock()
        receipt = _base_receipt(operation="backup", started_epoch=started, runtime_mode=runtime_mode)
        receipt.update(
            {
                "status": "pass",
                "completed_at": _utc_iso(completed),
                "duration_seconds": round(max(0.0, completed - started), 3),
                "release": release_identity,
                "source": source_identity,
                "source_snapshot": source_snapshot,
                "source_schema": source_schema,
                "source_critical_data": source_critical_data,
                "artifact": artifact_receipt,
                "off_host_object": off_host_object,
                "verification": {
                    "custom_format_list_valid": True,
                    "source_schema_contract_valid": True,
                    "source_snapshot_contract_valid": True,
                    "source_critical_data_contract_valid": True,
                    "source_critical_data_table_set_bound": True,
                    "off_host_object_verified": bool(off_host_object),
                },
                "commands": commands,
            }
        )
        return receipt
    finally:
        plain_dump.unlink(missing_ok=True)
        if encrypted_temp is not None:
            encrypted_temp.unlink(missing_ok=True)


def _validate_disposable_target(
    *,
    env: Mapping[str, str],
    target_identity: Mapping[str, object],
    source_identity: Mapping[str, object],
) -> None:
    confirmation = str(env.get("PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM") or "").strip()
    if confirmation != DISPOSABLE_CONFIRMATION:
        raise DisasterRecoveryError(
            "disposable_confirmation_required",
            f"Set PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM={DISPOSABLE_CONFIRMATION} for a disposable target.",
        )
    prefix = str(env.get("PROPERTYQUARRY_RESTORE_DISPOSABLE_PREFIX") or DEFAULT_DISPOSABLE_PREFIX).strip().lower()
    database = str(target_identity.get("database") or "").strip().lower()
    if not prefix or not database.startswith(prefix):
        raise DisasterRecoveryError(
            "target_not_disposable",
            f"Restore target database must start with disposable prefix {prefix!r}.",
        )
    if _identity_key(target_identity) == _identity_key(source_identity):
        raise DisasterRecoveryError("target_matches_source", "Restore target must not be the backup source database.")
    host = str(target_identity.get("host") or "").strip().lower()
    local_hosts = {"localhost", "127.0.0.1", "::1", "propertyquarry-db"}
    if host not in local_hosts and not _truthy(env, "PROPERTYQUARRY_RESTORE_ALLOW_REMOTE_TARGET"):
        raise DisasterRecoveryError(
            "remote_target_forbidden",
            "Remote restore targets require PROPERTYQUARRY_RESTORE_ALLOW_REMOTE_TARGET=1.",
        )


def _psql_scalar(
    *,
    step: str,
    psql: str,
    database_url: str,
    sql: str,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    snapshot_id: str | None = None,
) -> str:
    bound_sql = _snapshot_bound_sql(sql, snapshot_id)
    result = _run_checked(
        step=step,
        command=[
            psql,
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--dbname",
            database_url,
            "--command",
            bound_sql,
        ],
        environ=env,
        runner=runner,
        commands=commands,
    )
    lines = [line.strip() for line in str(getattr(result, "stdout", "") or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _migration_ledger_from_database(
    *,
    label: str,
    step: str,
    psql: str,
    database_url: str,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    require_current: bool,
    snapshot_id: str | None = None,
) -> dict[str, object]:
    expected = _expected_schema_ledger()
    ledger_table = str(expected["ledger_table"])
    component = str(expected["component"])
    if not _SAFE_TABLE_RE.fullmatch(ledger_table):
        raise DisasterRecoveryError(
            "schema_contract_invalid",
            "The release-controlled migration ledger table is not a safe identifier.",
        )
    component_literal = component.replace("'", "''")
    ledger_present_raw = _psql_scalar(
        step=f"{step}_presence",
        psql=psql,
        database_url=database_url,
        sql=f'''SELECT to_regclass('"public"."{ledger_table}"') IS NOT NULL;''',
        env=env,
        runner=runner,
        commands=commands,
        snapshot_id=snapshot_id,
    ).lower()
    if ledger_present_raw not in {"t", "true", "1", "f", "false", "0"}:
        raise DisasterRecoveryError(
            "schema_ledger_presence_invalid",
            f"{label} migration ledger presence query returned an invalid value.",
        )
    ledger_present = ledger_present_raw in {"t", "true", "1"}
    rows: object = []
    if ledger_present:
        sql = (
            "SELECT COALESCE(json_agg(json_build_object("
            "'version', ledger_row.\"version\", "
            "'migration_name', ledger_row.\"migration_name\", "
            "'checksum_sha256', ledger_row.\"checksum_sha256\") "
            "ORDER BY ledger_row.\"version\")::text, '[]') "
            f'FROM "public"."{ledger_table}" AS ledger_row '
            f"WHERE ledger_row.\"component\" = '{component_literal}';"
        )
        raw = _psql_scalar(
            step=step,
            psql=psql,
            database_url=database_url,
            sql=sql,
            env=env,
            runner=runner,
            commands=commands,
            snapshot_id=snapshot_id,
        )
        try:
            rows = json.loads(raw)
        except Exception as exc:
            raise DisasterRecoveryError(
                "schema_ledger_query_invalid",
                f"{label} migration ledger query did not return valid JSON.",
            ) from exc
    if not isinstance(rows, list):
        raise DisasterRecoveryError(
            "schema_ledger_query_invalid",
            f"{label} migration ledger query did not return a JSON array.",
        )
    payload: dict[str, object] = {
        "component": component,
        "ledger_table": ledger_table,
        "ledger_present": ledger_present,
        "current_version": int(rows[-1].get("version") or 0) if rows and isinstance(rows[-1], Mapping) else 0,
        "migrations": rows,
    }
    payload["fingerprint_sha256"] = _schema_ledger_fingerprint(payload)
    return _validated_schema_ledger(payload, label=label, require_current=require_current)


def _verified_off_host_object_from_hook(
    *,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    artifact: Mapping[str, object],
    artifact_path: Path,
    now_epoch: float,
    required: bool,
) -> dict[str, object]:
    command = _hook_command(env, "PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_COMMAND")
    if not command:
        if required:
            raise DisasterRecoveryError(
                "off_host_verification_required",
                "Production backups require PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_COMMAND.",
            )
        return {}
    result = _run_checked(
        step="verify_off_host_object",
        command=command,
        environ=env,
        runner=runner,
        commands=commands,
        extra_env={
            "PROPERTYQUARRY_BACKUP_ARTIFACT_PATH": str(artifact_path),
            "PROPERTYQUARRY_BACKUP_ARTIFACT_SHA256": str(artifact.get("sha256") or ""),
            "PROPERTYQUARRY_BACKUP_ARTIFACT_SIZE_BYTES": str(artifact.get("size_bytes") or ""),
            "PROPERTYQUARRY_BACKUP_ARTIFACT_ENCRYPTED": "1" if artifact.get("encrypted") is True else "0",
        },
        recorded_command=_recorded_hook_command(command),
        include_failure_stderr=False,
        process_environment=_minimal_hook_environment(
            env,
            declared_keys_env="PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_ENV_KEYS",
            provider_keys=True,
        ),
    )
    lines = [line.strip() for line in str(getattr(result, "stdout", "") or "").splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_verification_invalid",
            "Off-host verifier did not return a JSON object on its final output line.",
        ) from exc
    return _validated_off_host_object(
        payload,
        artifact=artifact,
        now_epoch=now_epoch,
    )


def _hook_command(env: Mapping[str, str], name: str) -> list[str]:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return []
    try:
        command = shlex.split(raw)
    except ValueError as exc:
        raise DisasterRecoveryError("hook_invalid", f"{name} is not a valid command.") from exc
    if not command:
        raise DisasterRecoveryError("hook_invalid", f"{name} is empty.")
    if len(command) != 1:
        raise DisasterRecoveryError(
            "hook_arguments_forbidden",
            f"{name} must be an argument-free executable; hook argv is never heuristically classified.",
        )
    executable = Path(command[0])
    if not executable.is_absolute() or os.path.normpath(str(executable)) != str(executable):
        raise DisasterRecoveryError(
            "hook_path_invalid",
            f"{name} must use a canonical absolute executable path.",
        )
    try:
        resolved = executable.resolve(strict=True)
        executable_stat = os.lstat(executable)
    except Exception as exc:
        raise DisasterRecoveryError(
            "hook_path_invalid",
            f"{name} executable is unavailable.",
        ) from exc
    if (
        resolved != executable
        or stat.S_ISLNK(executable_stat.st_mode)
        or not stat.S_ISREG(executable_stat.st_mode)
        or executable_stat.st_uid not in {0, os.geteuid()}
        or executable_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or not executable_stat.st_mode & stat.S_IXUSR
    ):
        raise DisasterRecoveryError(
            "hook_path_untrusted",
            f"{name} executable is not a trusted regular file.",
        )
    return [str(executable)]


def _minimal_hook_environment(
    env: Mapping[str, str],
    *,
    declared_keys_env: str | None,
    provider_keys: bool = False,
) -> dict[str, str]:
    process_env = {
        "PATH": AWS_CLI_MINIMAL_PATH,
        "LANG": "C",
        "LC_ALL": "C",
    }
    allowed = set(_HOOK_TLS_ENV_KEYS)
    if provider_keys:
        allowed.update(_AWS_PROVIDER_ENV_KEYS)
    if declared_keys_env:
        raw_keys = [item.strip() for item in str(env.get(declared_keys_env) or "").split(",") if item.strip()]
        for key in raw_keys:
            if not _ENV_KEY_RE.fullmatch(key):
                raise DisasterRecoveryError(
                    "hook_env_key_invalid",
                    f"{declared_keys_env} contains an invalid environment key.",
                )
            if key in {"DATABASE_URL", "PROPERTYQUARRY_BACKUP_DATABASE_URL", "PROPERTYQUARRY_RESTORE_DATABASE_URL"}:
                raise DisasterRecoveryError(
                    "hook_env_key_forbidden",
                    f"{declared_keys_env} cannot import database credentials from the operator environment.",
                )
            if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"} | _HOOK_FORBIDDEN_ENV_KEYS:
                raise DisasterRecoveryError(
                    "hook_env_key_forbidden",
                    f"{declared_keys_env} cannot override the fixed hook process environment.",
                )
            allowed.add(key)
    process_env.update({key: str(env[key]) for key in allowed if key in env})
    return process_env


def _recorded_hook_command(command: Sequence[str]) -> list[str]:
    return [str(command[0])]


def _retrieval_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


def _validate_open_retrieval_file(*, destination: Path, descriptor: int) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(destination)
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_retrieval_destination_race",
            "Retrieval destination identity could not be revalidated.",
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or _retrieval_file_identity(opened) != _retrieval_file_identity(named)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1
    ):
        raise DisasterRecoveryError(
            "off_host_retrieval_destination_race",
            "Retrieval destination was replaced or no longer satisfies its private-file contract.",
        )
    return opened


@contextmanager
def _exclusive_retrieval_destination(destination: Path) -> Iterator[int]:
    if not destination.is_absolute() or os.path.normpath(str(destination)) != str(destination):
        raise DisasterRecoveryError(
            "off_host_retrieval_destination_invalid",
            "Retrieval destination must be an absolute canonical path.",
        )
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    try:
        parent_stat = os.lstat(parent)
        parent_resolved = parent.resolve(strict=True)
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_retrieval_directory_invalid",
            "Private retrieval directory is unavailable.",
        ) from exc
    if (
        parent_resolved != parent
        or stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise DisasterRecoveryError(
            "off_host_retrieval_directory_untrusted",
            "Retrieval directory must be canonical, process-owned, and mode 0700.",
        )
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise DisasterRecoveryError(
            "off_host_retrieval_destination_exists",
            "Off-host retrieval destination must not already exist.",
        )
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    identity: tuple[int, int, int, int, int, int] | None = None
    completed = False
    try:
        descriptor = os.open(destination, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        opened = _validate_open_retrieval_file(destination=destination, descriptor=descriptor)
        identity = _retrieval_file_identity(opened)
        yield descriptor
        _validate_open_retrieval_file(destination=destination, descriptor=descriptor)
        completed = True
    finally:
        named_matches = False
        if descriptor >= 0 and identity is not None:
            try:
                named_matches = _retrieval_file_identity(os.lstat(destination)) == identity
            except Exception:
                named_matches = False
        if descriptor >= 0:
            os.close(descriptor)
        if not completed and named_matches:
            destination.unlink(missing_ok=True)


def _validated_off_host_retrieval(
    payload: object,
    *,
    off_host_object: Mapping[str, object],
    artifact: Mapping[str, object],
    now_epoch: float,
    retrieved_path: Path | None,
    retrieved_descriptor: int | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise DisasterRecoveryError(
            "off_host_retrieval_invalid",
            "Off-host retrieval command did not return provider retrieval evidence.",
        )
    if str(payload.get("schema") or "").strip() != OFF_HOST_RETRIEVAL_SCHEMA:
        raise DisasterRecoveryError(
            "off_host_retrieval_schema_invalid",
            "Off-host retrieval evidence has the wrong schema.",
        )
    identity_fields = (
        "provider",
        "backend",
        "region",
        "bucket",
        "object_key",
        "version_id",
        "etag",
        "object_lock_mode",
        "object_lock_retain_until",
    )
    normalized_identity = {
        key: (
            _normalize_s3_etag(payload.get(key))
            if key == "etag"
            else str(payload.get(key) or "").strip().lower()
            if key in {"provider", "backend", "region"}
            else str(payload.get(key) or "").strip()
        )
        for key in identity_fields
    }
    expected_identity = {
        key: (
            _normalize_s3_etag(off_host_object.get(key))
            if key == "etag"
            else str(off_host_object.get(key) or "").strip().lower()
            if key in {"provider", "backend", "region"}
            else str(off_host_object.get(key) or "").strip()
        )
        for key in identity_fields
    }
    if normalized_identity != expected_identity:
        raise DisasterRecoveryError(
            "off_host_retrieval_identity_mismatch",
            "Retrieved object identity does not match the exact provider object version in the backup receipt.",
        )
    if any(
        payload.get(key) is not True
        for key in ("object_exists", "provider_verified", "version_verified", "checksum_verified")
    ):
        raise DisasterRecoveryError(
            "off_host_retrieval_unverified",
            "Provider retrieval must verify object existence, immutable version identity, and checksum.",
        )
    provider_request_id = str(payload.get("provider_request_id") or "").strip()
    retrieval_method = str(payload.get("retrieval_method") or "").strip()
    provider_contract = REMOTE_PROVIDER_CONTRACTS.get(normalized_identity["provider"])
    if (
        not provider_request_id
        or not provider_contract
        or retrieval_method != provider_contract["retrieval_method"]
    ):
        raise DisasterRecoveryError(
            "off_host_retrieval_provenance_missing",
            "Provider request identity and the release-approved retrieval method are required.",
        )
    expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
    expected_size = int(artifact.get("size_bytes") or 0)
    declared_sha256 = str(payload.get("sha256") or "").strip().lower()
    try:
        declared_size = int(payload.get("size_bytes") or 0)
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_retrieval_size_invalid",
            "Provider retrieval size is invalid.",
        ) from exc
    if (
        not _SHA256_RE.fullmatch(declared_sha256)
        or declared_sha256 != expected_sha256
        or declared_size <= 0
        or declared_size != expected_size
    ):
        raise DisasterRecoveryError(
            "off_host_retrieval_artifact_mismatch",
            "Provider retrieval evidence does not match the backup artifact hash and size.",
        )
    if retrieved_path is not None:
        if retrieved_descriptor is None:
            raise DisasterRecoveryError(
                "off_host_retrieval_descriptor_missing",
                "Provider retrieval validation requires the exclusively opened destination descriptor.",
            )
        opened = _validate_open_retrieval_file(
            destination=retrieved_path,
            descriptor=retrieved_descriptor,
        )
        actual_size = opened.st_size
        actual_sha256 = _sha256_descriptor(retrieved_descriptor)
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            raise DisasterRecoveryError(
                "off_host_retrieval_bytes_mismatch",
                "Retrieved bytes do not match the immutable off-host artifact identity.",
                details={"expected_sha256": expected_sha256, "actual_sha256": actual_sha256},
            )
    aws_cli = _validated_aws_cli_attestation(
        payload.get("aws_cli"),
        label="Off-host retrieval",
    )
    retrieved_at = str(payload.get("retrieved_at") or "").strip()
    retrieved_epoch = _parse_utc_iso(retrieved_at)
    if retrieved_epoch > now_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS:
        raise DisasterRecoveryError(
            "off_host_retrieval_timestamp_future",
            "Off-host retrieval timestamp is in the future.",
        )
    return {
        "schema": OFF_HOST_RETRIEVAL_SCHEMA,
        **normalized_identity,
        "sha256": declared_sha256,
        "size_bytes": declared_size,
        "object_exists": True,
        "provider_verified": True,
        "version_verified": True,
        "checksum_verified": True,
        "provider_request_id": provider_request_id,
        "retrieval_method": retrieval_method,
        "retrieved_at": _utc_iso(retrieved_epoch),
        "aws_cli": aws_cli,
    }


@contextmanager
def _retrieve_off_host_object(
    *,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    off_host_object: Mapping[str, object],
    artifact: Mapping[str, object],
    destination: Path,
    clock: Clock,
) -> Iterator[tuple[dict[str, object], int]]:
    if str(env.get("PROPERTYQUARRY_RESTORE_OFF_HOST_RETRIEVE_COMMAND") or "").strip():
        raise DisasterRecoveryError(
            "off_host_retrieval_hook_forbidden",
            "Arbitrary restore retrieval hooks are forbidden; use the release-approved provider backend.",
        )
    if str(env.get("PROPERTYQUARRY_AWS_BIN") or "").strip():
        raise DisasterRecoveryError(
            "off_host_provider_binary_override_forbidden",
            "PROPERTYQUARRY_AWS_BIN overrides are forbidden for recovery proof.",
        )
    provider = str(off_host_object.get("provider") or "").strip().lower()
    backend = str(off_host_object.get("backend") or "").strip().lower()
    if provider != "s3" or backend != "aws_s3api":
        raise DisasterRecoveryError(
            "off_host_backend_invalid",
            "Restore retrieval requires the release-approved S3 aws-s3api backend.",
        )
    attestation = _attest_aws_cli(env=env, runner=runner, commands=commands)
    bucket = str(off_host_object.get("bucket") or "")
    region = str(off_host_object.get("region") or "")
    object_key = str(off_host_object.get("object_key") or "")
    version_id = str(off_host_object.get("version_id") or "")
    etag = _normalize_s3_etag(off_host_object.get("etag"))
    object_lock_mode = str(
        off_host_object.get("object_lock_mode") or ""
    ).strip().upper()
    object_lock_retain_until = str(
        off_host_object.get("object_lock_retain_until") or ""
    ).strip()
    object_lock_retain_epoch = _parse_utc_iso(object_lock_retain_until)
    provider_env = _minimal_hook_environment(env, declared_keys_env=None, provider_keys=True)
    provider_env.update(
        {
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_CLI_AUTO_PROMPT": "off",
            "AWS_CLI_HISTORY_FILE": "/dev/null",
            "AWS_PAGER": "",
            "AWS_DEFAULT_OUTPUT": "json",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    endpoint_suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    provider_endpoint = f"https://s3.{region}.{endpoint_suffix}"
    cli_path = str(attestation["path"])
    head_result = _run_attested_aws_cli(
        step="head_off_host_object_version",
        arguments=[
            "--debug",
            "--region",
            region,
            "--endpoint-url",
            provider_endpoint,
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            object_key,
            "--version-id",
            version_id,
            "--checksum-mode",
            "ENABLED",
        ],
        attestation=attestation,
        env=env,
        provider_env=provider_env,
        runner=runner,
        commands=commands,
        recorded_command=[
            cli_path,
            "s3api",
            "head-object",
            "<immutable-identity-redacted>",
        ],
    )
    try:
        head = json.loads(str(getattr(head_result, "stdout", "") or ""))
    except Exception as exc:
        raise DisasterRecoveryError(
            "off_host_retrieval_invalid",
            "Provider head-object response is invalid.",
        ) from exc
    if not isinstance(head, Mapping):
        raise DisasterRecoveryError(
            "off_host_retrieval_invalid",
            "Provider head-object response must be a JSON object.",
        )

    with _exclusive_retrieval_destination(destination) as destination_descriptor:
        get_result = _run_attested_aws_cli(
            step="retrieve_off_host_object_version",
            arguments=[
                "--debug",
                "--region",
                region,
                "--endpoint-url",
                provider_endpoint,
                "s3api",
                "get-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--version-id",
                version_id,
                "--if-match",
                etag,
                "--checksum-mode",
                "ENABLED",
                _aws_cli_fd_executable(destination_descriptor),
            ],
            attestation=attestation,
            env=env,
            provider_env=provider_env,
            runner=runner,
            commands=commands,
            recorded_command=[
                cli_path,
                "s3api",
                "get-object",
                "<immutable-identity-redacted>",
            ],
            extra_pass_fds=(destination_descriptor,),
        )
        try:
            retrieved = json.loads(str(getattr(get_result, "stdout", "") or ""))
        except Exception as exc:
            raise DisasterRecoveryError(
                "off_host_retrieval_invalid",
                "Provider get-object response is invalid.",
            ) from exc
        if not isinstance(retrieved, Mapping):
            raise DisasterRecoveryError(
                "off_host_retrieval_invalid",
                "Provider get-object response must be a JSON object.",
            )
        os.fsync(destination_descriptor)

        expected_size = int(artifact.get("size_bytes") or 0)
        for response, label in ((head, "head-object"), (retrieved, "get-object")):
            response_version = str(response.get("VersionId") or "")
            response_etag = _normalize_s3_etag(response.get("ETag"))
            try:
                response_size = int(response.get("ContentLength") or 0)
            except Exception as exc:
                raise DisasterRecoveryError(
                    "off_host_retrieval_identity_mismatch",
                    f"Provider {label} size is invalid.",
                ) from exc
            if response_version != version_id or response_etag != etag or response_size != expected_size:
                raise DisasterRecoveryError(
                    "off_host_retrieval_identity_mismatch",
                    f"Provider {label} response does not match the immutable receipt identity.",
                )
            response_lock_mode = str(
                response.get("ObjectLockMode") or ""
            ).strip().upper()
            response_encryption = str(
                response.get("ServerSideEncryption") or ""
            ).strip()
            try:
                response_retain_epoch = _parse_utc_iso(
                    response.get("ObjectLockRetainUntilDate")
                )
            except DisasterRecoveryError as exc:
                raise DisasterRecoveryError(
                    "off_host_immutability_unverified",
                    f"Provider {label} Object Lock retention is invalid.",
                ) from exc
            if response_encryption != "AES256":
                raise DisasterRecoveryError(
                    "off_host_encryption_unverified",
                    f"Provider {label} response does not prove the required AES256 server-side encryption.",
                )
            if (
                response_lock_mode != object_lock_mode
                or response_retain_epoch < object_lock_retain_epoch
            ):
                raise DisasterRecoveryError(
                    "off_host_immutability_unverified",
                    f"Provider {label} response does not preserve the receipt-bound Object Lock retention.",
                )

        def provider_request_id(response: Mapping[str, object], result: object) -> str:
            metadata = response.get("ResponseMetadata")
            metadata_request_id = metadata.get("RequestId") if isinstance(metadata, Mapping) else ""
            modeled_request_id = str(response.get("RequestId") or metadata_request_id or "").strip()
            if modeled_request_id:
                return modeled_request_id
            match = _AWS_REQUEST_ID_RE.search(str(getattr(result, "stderr", "") or ""))
            return str(match.group(1) if match else "").strip()

        head_request_id = provider_request_id(head, head_result)
        get_request_id = provider_request_id(retrieved, get_result)
        if (
            not head_request_id
            or not get_request_id
            or head_request_id == get_request_id
        ):
            raise DisasterRecoveryError(
                "off_host_retrieval_provenance_missing",
                "Distinct provider head/get request identities are required.",
            )
        opened = _validate_open_retrieval_file(
            destination=destination,
            descriptor=destination_descriptor,
        )
        payload = {
            "schema": OFF_HOST_RETRIEVAL_SCHEMA,
            "provider": provider,
            "backend": backend,
            "region": region,
            "bucket": bucket,
            "object_key": object_key,
            "version_id": version_id,
            "etag": etag,
            "object_lock_mode": object_lock_mode,
            "object_lock_retain_until": object_lock_retain_until,
            "sha256": _sha256_descriptor(destination_descriptor),
            "size_bytes": opened.st_size,
            "object_exists": True,
            "provider_verified": True,
            "version_verified": True,
            "checksum_verified": True,
            "provider_request_id": f"{head_request_id}:{get_request_id}",
            "retrieval_method": REMOTE_PROVIDER_CONTRACTS[provider]["retrieval_method"],
            "retrieved_at": _utc_iso(clock()),
            "aws_cli": attestation,
        }
        retrieval = _validated_off_host_retrieval(
            payload,
            off_host_object=off_host_object,
            artifact=artifact,
            now_epoch=clock(),
            retrieved_path=destination,
            retrieved_descriptor=destination_descriptor,
        )
        yield retrieval, destination_descriptor


def _copy_descriptor_to_private_file(*, source_descriptor: int, destination: Path) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    destination_descriptor = -1
    try:
        destination_descriptor = os.open(destination, flags, 0o600)
        os.fchmod(destination_descriptor, 0o600)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("private restore archive write made no progress")
                offset += written
        os.fsync(destination_descriptor)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
    except Exception as exc:
        if destination_descriptor >= 0:
            destination.unlink(missing_ok=True)
        raise DisasterRecoveryError(
            "restore_archive_copy_failed",
            "Verified retrieval bytes could not be copied to the private restore archive.",
        ) from exc
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


@contextmanager
def _prepared_restore_archive(
    *,
    env: Mapping[str, str],
    runner: Runner,
    commands: list[dict[str, object]],
    off_host_object: Mapping[str, object],
    artifact: Mapping[str, object],
    destination: Path,
    encrypted: bool,
    gpg: str,
    pg_restore: str,
    clock: Clock,
) -> Iterator[tuple[dict[str, object], str, Path, bool]]:
    with tempfile.TemporaryDirectory(prefix="propertyquarry-restore-drill-") as temp_dir:
        dump_path = Path(temp_dir) / "backup.dump"
        with _retrieve_off_host_object(
            env=env,
            runner=runner,
            commands=commands,
            off_host_object=off_host_object,
            artifact=artifact,
            destination=destination,
            clock=clock,
        ) as (retrieval, retrieval_descriptor):
            actual_sha256 = _sha256_descriptor(retrieval_descriptor)
            if actual_sha256 != str(artifact.get("sha256") or "").strip().lower():
                raise DisasterRecoveryError(
                    "off_host_retrieval_bytes_mismatch",
                    "Verified retrieval bytes changed before archive consumption.",
                )
            if encrypted:
                _run_checked(
                    step="decrypt_backup",
                    command=[
                        gpg,
                        "--batch",
                        "--yes",
                        "--output",
                        str(dump_path),
                        "--decrypt",
                        _aws_cli_fd_executable(retrieval_descriptor),
                    ],
                    environ=env,
                    runner=runner,
                    commands=commands,
                    recorded_command=[
                        gpg,
                        "--batch",
                        "--yes",
                        "--output",
                        str(dump_path),
                        "--decrypt",
                        "<verified-retrieval-fd>",
                    ],
                    pass_fds=(retrieval_descriptor,),
                )
            else:
                _copy_descriptor_to_private_file(
                    source_descriptor=retrieval_descriptor,
                    destination=dump_path,
                )
            _validate_open_retrieval_file(
                destination=destination,
                descriptor=retrieval_descriptor,
            )
            if _sha256_descriptor(retrieval_descriptor) != actual_sha256:
                raise DisasterRecoveryError(
                    "off_host_retrieval_bytes_mismatch",
                    "Verified retrieval bytes changed during archive consumption.",
                )
            _private_file(dump_path)
            if not dump_path.is_file() or dump_path.stat().st_size <= 0:
                raise DisasterRecoveryError(
                    "decrypted_artifact_empty",
                    "Restore input is empty after decryption.",
                )
            expected_plaintext_sha256 = str(
                artifact.get("plaintext_sha256") or ""
            ).strip().lower()
            if expected_plaintext_sha256 and _sha256(dump_path) != expected_plaintext_sha256:
                raise DisasterRecoveryError(
                    "plaintext_checksum_mismatch",
                    "Decrypted backup checksum is invalid.",
                )
            _run_checked(
                step="pg_restore_list",
                command=[pg_restore, "--list", str(dump_path)],
                environ=env,
                runner=runner,
                commands=commands,
            )
            _validate_open_retrieval_file(
                destination=destination,
                descriptor=retrieval_descriptor,
            )
            if _sha256_descriptor(retrieval_descriptor) != actual_sha256:
                raise DisasterRecoveryError(
                    "off_host_retrieval_bytes_mismatch",
                    "Verified retrieval bytes changed before archive validation completed.",
                )
        yield retrieval, actual_sha256, dump_path, bool(expected_plaintext_sha256)


def _table_contract(env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    def names(name: str) -> list[str]:
        values = [item.strip().lower() for item in str(env.get(name) or "").split(",") if item.strip()]
        if len(values) != len(set(values)):
            raise DisasterRecoveryError("required_table_duplicate", f"{name} contains duplicate table names.")
        for table in values:
            if not _SAFE_TABLE_RE.fullmatch(table):
                raise DisasterRecoveryError("required_table_invalid", f"Unsafe required table name: {table}")
        return values

    required_tables = names("PROPERTYQUARRY_RESTORE_REQUIRED_TABLES")
    non_empty_tables = names("PROPERTYQUARRY_RESTORE_REQUIRED_NON_EMPTY_TABLES")
    if any(table not in required_tables for table in non_empty_tables):
        raise DisasterRecoveryError(
            "non_empty_table_not_required",
            "Every required non-empty table must also be listed in PROPERTYQUARRY_RESTORE_REQUIRED_TABLES.",
        )
    return required_tables, non_empty_tables


def execute_restore_drill(
    *,
    artifact_path: Path,
    backup_receipt_path: Path,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    clock: Clock = time.time,
    which: Which = shutil.which,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    started = clock()
    runtime_mode = str(env.get("EA_RUNTIME_MODE") or "dev").strip().lower() or "dev"
    backup_receipt = _load_receipt(backup_receipt_path)
    artifact_info = dict(backup_receipt.get("artifact") or {})
    source_identity = dict(backup_receipt.get("source") or {})
    backup_release = _validated_receipt_release(backup_receipt.get("release"), label="Backup")
    current_release = _release_identity(
        env,
        required=runtime_mode == "prod" or bool(
            str(env.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip()
            or str(env.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST") or "").strip()
        ),
    )
    if any(current_release.values()) and current_release != backup_release:
        raise DisasterRecoveryError(
            "restore_release_mismatch",
            "Restore drill release identity does not match the backup receipt.",
        )
    source_schema = _validated_schema_ledger(
        backup_receipt.get("source_schema"),
        label="Backup source",
        require_current=False,
    )
    source_schema_version = int(source_schema["current_version"])
    source_snapshot = _validated_snapshot_evidence(
        backup_receipt.get("source_snapshot"),
        label="Backup source",
        pg_dump_plaintext_sha256=str(artifact_info.get("plaintext_sha256") or "").lower(),
        governing_schema_version=source_schema_version,
    )
    source_critical_data = _validated_critical_data_evidence(
        backup_receipt.get("source_critical_data"),
        label="Backup source",
        governing_schema_version=source_schema_version,
    )
    artifact = artifact_path.expanduser().absolute()
    encrypted = bool(artifact_info.get("encrypted"))
    if runtime_mode == "prod" and not encrypted:
        raise DisasterRecoveryError("encryption_required", "Production restore drills reject unencrypted backup artifacts.")
    off_host_object = _validated_off_host_object(
        backup_receipt.get("off_host_object"),
        artifact=artifact_info,
        now_epoch=started,
    )
    target_url = str(env.get("PROPERTYQUARRY_RESTORE_DATABASE_URL") or "").strip()
    target_identity = _database_identity(target_url, label="Restore target")
    _validate_disposable_target(env=env, target_identity=target_identity, source_identity=source_identity)
    if artifact.exists() or artifact.is_symlink():
        raise DisasterRecoveryError(
            "off_host_retrieval_destination_exists",
            "Restore input must be a new retrieval destination; an existing local artifact is never trusted.",
        )
    completed_backup_epoch = _parse_utc_iso(backup_receipt.get("completed_at"))
    artifact_age_seconds = max(0.0, started - completed_backup_epoch)
    max_age_seconds = _float_env(
        env,
        "PROPERTYQUARRY_BACKUP_MAX_AGE_SECONDS",
        DEFAULT_ARTIFACT_MAX_AGE_SECONDS,
    )
    if artifact_age_seconds > max_age_seconds:
        raise DisasterRecoveryError(
            "rpo_exceeded",
            "Backup artifact is older than the configured RPO.",
            details={
                "objectives": {
                    "rpo_seconds": round(artifact_age_seconds, 3),
                    "max_rpo_seconds": max_age_seconds,
                    "rpo_met": False,
                }
            },
        )
    pg_restore = _binary(env, "PROPERTYQUARRY_PG_RESTORE_BIN", "pg_restore", which)
    psql = _binary(env, "PROPERTYQUARRY_PSQL_BIN", "psql", which)
    gpg = _binary(env, "PROPERTYQUARRY_GPG_BIN", "gpg", which) if encrypted else ""
    commands: list[dict[str, object]] = []
    max_restore_seconds = _float_env(
        env,
        "PROPERTYQUARRY_RESTORE_MAX_DURATION_SECONDS",
        DEFAULT_RESTORE_MAX_DURATION_SECONDS,
    )
    verification: dict[str, object] = {
        "target_disposable_guard": True,
        "release_identity_matches_backup": True,
        "off_host_object_verified": True,
    }
    restore_started = clock()
    with _prepared_restore_archive(
        env=env,
        runner=runner,
        commands=commands,
        off_host_object=off_host_object,
        artifact=artifact_info,
        destination=artifact,
        encrypted=encrypted,
        gpg=gpg,
        pg_restore=pg_restore,
        clock=clock,
    ) as (retrieval, actual_sha256, dump_path, plaintext_checksum_valid):
        verification["off_host_retrieval_verified"] = True
        verification["aws_cli_attested"] = True
        verification["artifact_checksum_valid"] = True
        verification["plaintext_checksum_valid"] = plaintext_checksum_valid
        verification["custom_format_list_valid"] = True
        current_database = _psql_scalar(
            step="target_identity",
            psql=psql,
            database_url=target_url,
            sql="SELECT current_database();",
            env=env,
            runner=runner,
            commands=commands,
        )
        if current_database.strip().lower() != str(target_identity["database"]).strip().lower():
            raise DisasterRecoveryError("target_identity_mismatch", "Connected restore target does not match the guarded database name.")
        verification["target_identity_valid"] = True
        _run_checked(
            step="pg_restore",
            command=[
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                target_url,
                str(dump_path),
            ],
            environ=env,
            runner=runner,
            commands=commands,
        )
        hook_env = {"DATABASE_URL": target_url, "PROPERTYQUARRY_RESTORE_DRILL": "1"}
        migration_hook = _hook_command(env, "PROPERTYQUARRY_RESTORE_MIGRATION_COMMAND")
        if migration_hook:
            _run_checked(
                step="migration_hook",
                command=migration_hook,
                environ=env,
                runner=runner,
                commands=commands,
                extra_env=hook_env,
                recorded_command=_recorded_hook_command(migration_hook),
                include_failure_stderr=False,
                process_environment=_minimal_hook_environment(
                    env,
                    declared_keys_env="PROPERTYQUARRY_RESTORE_MIGRATION_ENV_KEYS",
                ),
            )
        verification["migration_hook_passed"] = bool(migration_hook)
        schema_table_count_raw = _psql_scalar(
            step="schema_integrity",
            psql=psql,
            database_url=target_url,
            sql=(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE';"
            ),
            env=env,
            runner=runner,
            commands=commands,
        )
        try:
            schema_table_count = int(schema_table_count_raw)
        except ValueError as exc:
            raise DisasterRecoveryError("schema_verification_invalid", "Schema verification did not return a table count.") from exc
        if schema_table_count <= 0:
            raise DisasterRecoveryError("schema_empty", "Restored database has no public base tables.")
        verification["schema_table_count"] = schema_table_count
        restored_schema = _migration_ledger_from_database(
            label="Restored",
            step="restored_migration_ledger",
            psql=psql,
            database_url=target_url,
            env=env,
            runner=runner,
            commands=commands,
            require_current=True,
        )
        restored_critical_data = _critical_data_evidence_from_database(
            label="Restored",
            step="restored_critical_data",
            psql=psql,
            database_url=target_url,
            env=env,
            runner=runner,
            commands=commands,
            governing_schema_version=source_schema_version,
        )
        if restored_critical_data != source_critical_data:
            raise DisasterRecoveryError(
                "critical_data_restore_mismatch",
                "Restored critical-data row counts or fingerprints do not match the backup source.",
                details={
                    "critical_data_contract_fingerprint": source_critical_data[
                        "contract_fingerprint_sha256"
                    ]
                },
            )
        verification["source_schema_contract_valid"] = True
        verification["source_snapshot_contract_valid"] = True
        verification["restored_schema_contract_valid"] = True
        verification["schema_migration_forward_verified"] = (
            int(restored_schema["current_version"]) >= int(source_schema["current_version"])
        )
        verification["schema_ledger_matches_source"] = restored_schema == source_schema
        verification["critical_data_contract_valid"] = True
        verification["critical_data_table_set_exact"] = True
        verification["critical_data_exact_match"] = True
        required_tables, non_empty_tables = _table_contract(env)
        required_table_evidence: dict[str, dict[str, object]] = {}
        for table in required_tables:
            present = _psql_scalar(
                step=f"required_table_{table}",
                psql=psql,
                database_url=target_url,
                sql=f"SELECT to_regclass('public.{table}') IS NOT NULL;",
                env=env,
                runner=runner,
                commands=commands,
            ).lower()
            if present not in {"t", "true", "1"}:
                raise DisasterRecoveryError("required_table_missing", f"Required restored table is missing: {table}")
            required_table_evidence[table] = {
                "present": True,
                "requires_data": table in non_empty_tables,
            }
        for table in non_empty_tables:
            raw_count = _psql_scalar(
                step=f"required_non_empty_table_{table}",
                psql=psql,
                database_url=target_url,
                sql=f"SELECT COUNT(*) FROM {table};",
                env=env,
                runner=runner,
                commands=commands,
            )
            try:
                row_count = int(raw_count)
            except ValueError as exc:
                raise DisasterRecoveryError(
                    "required_table_count_invalid",
                    f"Required data table row count is invalid: {table}",
                ) from exc
            if row_count <= 0:
                raise DisasterRecoveryError(
                    "required_table_empty",
                    f"Required restored data table is empty: {table}",
                )
            required_table_evidence[table]["row_count"] = row_count
        verification["required_tables"] = required_tables
        verification["required_non_empty_tables"] = non_empty_tables
        verification["required_table_evidence"] = required_table_evidence
        integrity_sql_configured = bool(str(env.get("PROPERTYQUARRY_RESTORE_INTEGRITY_SQL") or "").strip())
        integrity_sql = str(env.get("PROPERTYQUARRY_RESTORE_INTEGRITY_SQL") or "SELECT 1;").strip()
        integrity_expected = str(env.get("PROPERTYQUARRY_RESTORE_INTEGRITY_EXPECTED_VALUE") or "1").strip()
        integrity_result = _psql_scalar(
            step="integrity_query",
            psql=psql,
            database_url=target_url,
            sql=integrity_sql,
            env=env,
            runner=runner,
            commands=commands,
        )
        if integrity_result != integrity_expected:
            raise DisasterRecoveryError(
                "integrity_query_failed",
                "Restore integrity query did not return its required value.",
            )
        verification["integrity_query_passed"] = True
        verification["integrity_query_contract_explicit"] = integrity_sql_configured
        verification["integrity_query_result_sha256"] = hashlib.sha256(
            integrity_result.encode("utf-8")
        ).hexdigest()
        verify_hook = _hook_command(env, "PROPERTYQUARRY_RESTORE_VERIFY_COMMAND")
        readiness_hook = _hook_command(env, "PROPERTYQUARRY_RESTORE_READINESS_COMMAND")
        if verify_hook:
            _run_checked(
                step="verification_hook",
                command=verify_hook,
                environ=env,
                runner=runner,
                commands=commands,
                extra_env=hook_env,
                recorded_command=_recorded_hook_command(verify_hook),
                include_failure_stderr=False,
                process_environment=_minimal_hook_environment(
                    env,
                    declared_keys_env="PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS",
                ),
            )
        if readiness_hook:
            _run_checked(
                step="readiness_hook",
                command=readiness_hook,
                environ=env,
                runner=runner,
                commands=commands,
                extra_env=hook_env,
                recorded_command=_recorded_hook_command(readiness_hook),
                include_failure_stderr=False,
                process_environment=_minimal_hook_environment(
                    env,
                    declared_keys_env="PROPERTYQUARRY_RESTORE_READINESS_ENV_KEYS",
                ),
            )
        verification["verification_hook_passed"] = bool(verify_hook)
        verification["readiness_hook_passed"] = bool(readiness_hook)
        restore_completed = clock()
        restore_duration_seconds = max(0.0, restore_completed - restore_started)
    completed = clock()
    objectives = {
        "rpo_seconds": round(artifact_age_seconds, 3),
        "max_rpo_seconds": max_age_seconds,
        "rpo_met": artifact_age_seconds <= max_age_seconds,
        "rto_seconds": round(restore_duration_seconds, 3),
        "max_rto_seconds": max_restore_seconds,
        "rto_met": restore_duration_seconds <= max_restore_seconds,
        "rto_scope": list(RESTORE_RTO_SCOPE),
    }
    if not objectives["rto_met"]:
        raise DisasterRecoveryError(
            "rto_exceeded",
            "Disposable restore exceeded the configured RTO.",
            details={"objectives": objectives, "verification": verification, "commands": commands},
        )
    receipt = _base_receipt(operation="restore_drill", started_epoch=started, runtime_mode=runtime_mode)
    receipt.update(
        {
            "status": "pass",
            "completed_at": _utc_iso(completed),
            "duration_seconds": round(max(0.0, completed - started), 3),
            "release": backup_release,
            "source": source_identity,
            "target": target_identity,
            "source_schema": source_schema,
            "source_snapshot": source_snapshot,
            "restored_schema": restored_schema,
            "source_critical_data": source_critical_data,
            "restored_critical_data": restored_critical_data,
            "artifact": {
                "path": str(artifact),
                "size_bytes": retrieval["size_bytes"],
                "sha256": actual_sha256,
                "encrypted": encrypted,
                "backup_completed_at": backup_receipt.get("completed_at"),
                "source": "provider_verified_off_host_retrieval",
            },
            "off_host_object": off_host_object,
            "off_host_retrieval": retrieval,
            "objectives": objectives,
            "verification": verification,
            "commands": commands,
        }
    )
    return receipt


def verify_release_dr_evidence(
    *,
    backup_receipt_path: Path,
    restore_receipt_path: Path,
    release_commit_sha: str,
    image_digest: str,
    max_age_seconds: float = DEFAULT_RELEASE_EVIDENCE_MAX_AGE_SECONDS,
    clock: Clock = time.time,
) -> dict[str, object]:
    started = clock()
    expected_release = _release_identity(
        {
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA": release_commit_sha,
            "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": image_digest,
        },
        required=True,
    )
    _reject_aws_cli_release_pin_overrides(os.environ)
    if not math.isfinite(max_age_seconds) or max_age_seconds < 1:
        raise DisasterRecoveryError(
            "release_evidence_max_age_invalid",
            "DR release evidence max age must be at least one second.",
        )
    backup_input_path = _canonical_receipt_path(backup_receipt_path)
    restore_input_path = _canonical_receipt_path(restore_receipt_path)
    backup = _load_passing_operation_receipt(
        backup_input_path,
        operation="backup",
    )
    restore = _load_passing_operation_receipt(
        restore_input_path,
        operation="restore_drill",
    )
    backup_release = _validated_receipt_release(backup.get("release"), label="Backup")
    restore_release = _validated_receipt_release(restore.get("release"), label="Restore drill")
    if backup_release != expected_release or restore_release != expected_release:
        raise DisasterRecoveryError(
            "release_evidence_mismatch",
            "DR receipts are not bound to the exact release commit and image digest.",
        )

    backup_completed_epoch = _parse_utc_iso(backup.get("completed_at"))
    restore_completed_epoch = _parse_utc_iso(restore.get("completed_at"))
    if (
        backup_completed_epoch > started + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS
        or restore_completed_epoch > started + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS
    ):
        raise DisasterRecoveryError(
            "release_evidence_timestamp_future",
            "DR receipt completion timestamp is in the future.",
        )
    if restore_completed_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS < backup_completed_epoch:
        raise DisasterRecoveryError(
            "release_evidence_order_invalid",
            "Restore drill receipt predates its backup receipt.",
        )
    backup_age = max(0.0, started - backup_completed_epoch)
    restore_age = max(0.0, started - restore_completed_epoch)
    if backup_age > max_age_seconds or restore_age > max_age_seconds:
        raise DisasterRecoveryError(
            "release_evidence_stale",
            "Backup and restore-drill receipts must both be recent for launch.",
            details={
                "backup_age_seconds": round(backup_age, 3),
                "restore_age_seconds": round(restore_age, 3),
                "max_age_seconds": max_age_seconds,
            },
        )

    backup_artifact = dict(backup.get("artifact") or {})
    restore_artifact = dict(restore.get("artifact") or {})
    if (
        backup_artifact.get("encrypted") is not True
        or restore_artifact.get("encrypted") is not True
        or str(backup_artifact.get("encryption") or "") != "gpg-recipient"
    ):
        raise DisasterRecoveryError(
            "release_backup_unencrypted",
            "Launch DR evidence requires an encrypted backup artifact.",
        )
    backup_sha256 = str(backup_artifact.get("sha256") or "").strip().lower()
    if (
        not _SHA256_RE.fullmatch(backup_sha256)
        or str(restore_artifact.get("sha256") or "").strip().lower() != backup_sha256
    ):
        raise DisasterRecoveryError(
            "release_artifact_mismatch",
            "Backup and restore receipts do not identify the same encrypted artifact.",
        )
    try:
        backup_size = int(backup_artifact.get("size_bytes") or 0)
        restore_size = int(restore_artifact.get("size_bytes") or 0)
    except Exception as exc:
        raise DisasterRecoveryError(
            "release_artifact_size_invalid",
            "Backup or restore artifact size is invalid.",
        ) from exc
    if backup_size <= 0 or restore_size != backup_size:
        raise DisasterRecoveryError(
            "release_artifact_size_mismatch",
            "Backup and restore receipts do not identify the same artifact size.",
        )
    if str(restore_artifact.get("backup_completed_at") or "").strip() != str(
        backup.get("completed_at") or ""
    ).strip():
        raise DisasterRecoveryError(
            "release_backup_reference_mismatch",
            "Restore receipt does not reference the exact backup completion identity.",
        )

    source_schema = _validated_schema_ledger(
        backup.get("source_schema"),
        label="Backup source",
        require_current=False,
    )
    restore_source_schema = _validated_schema_ledger(
        restore.get("source_schema"),
        label="Restore source",
        require_current=False,
    )
    restored_schema = _validated_schema_ledger(
        restore.get("restored_schema"),
        label="Restored",
        require_current=True,
    )
    if source_schema != restore_source_schema:
        raise DisasterRecoveryError(
            "release_schema_mismatch",
            "Backup and restore receipts do not identify the same source migration ledger.",
        )
    source_schema_version = int(source_schema["current_version"])
    source_snapshot = _validated_snapshot_evidence(
        backup.get("source_snapshot"),
        label="Backup source",
        pg_dump_plaintext_sha256=str(backup_artifact.get("plaintext_sha256") or "").lower(),
        governing_schema_version=source_schema_version,
    )
    restore_source_snapshot = _validated_snapshot_evidence(
        restore.get("source_snapshot"),
        label="Restore source",
        pg_dump_plaintext_sha256=str(backup_artifact.get("plaintext_sha256") or "").lower(),
        governing_schema_version=source_schema_version,
    )
    if source_snapshot != restore_source_snapshot:
        raise DisasterRecoveryError(
            "release_snapshot_mismatch",
            "Backup and restore receipts are not bound to the same exported source snapshot.",
        )
    source_critical_data = _validated_critical_data_evidence(
        backup.get("source_critical_data"),
        label="Backup source",
        governing_schema_version=source_schema_version,
    )
    restore_source_critical_data = _validated_critical_data_evidence(
        restore.get("source_critical_data"),
        label="Restore source",
        governing_schema_version=source_schema_version,
    )
    restored_critical_data = _validated_critical_data_evidence(
        restore.get("restored_critical_data"),
        label="Restored",
        governing_schema_version=source_schema_version,
    )
    if (
        source_critical_data != restore_source_critical_data
        or source_critical_data != restored_critical_data
    ):
        raise DisasterRecoveryError(
            "release_critical_data_mismatch",
            "Release DR receipts do not prove exact source-to-restore critical-data survival.",
        )

    off_host = _validated_off_host_object(
        backup.get("off_host_object"),
        artifact=backup_artifact,
        now_epoch=started,
    )
    restore_off_host = _validated_off_host_object(
        restore.get("off_host_object"),
        artifact=backup_artifact,
        now_epoch=started,
    )
    if off_host != restore_off_host:
        raise DisasterRecoveryError(
            "release_off_host_identity_mismatch",
            "Restore receipt is not bound to the exact verified off-host object version.",
        )
    retrieval = _validated_off_host_retrieval(
        restore.get("off_host_retrieval"),
        off_host_object=off_host,
        artifact=backup_artifact,
        now_epoch=started,
        retrieved_path=None,
    )
    off_host_verified_epoch = _parse_utc_iso(off_host.get("verified_at"))
    if off_host_verified_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS < backup_completed_epoch:
        raise DisasterRecoveryError(
            "release_off_host_verification_order_invalid",
            "Off-host verification predates the completed encrypted backup.",
        )
    if off_host_verified_epoch > restore_completed_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS:
        raise DisasterRecoveryError(
            "release_restore_precedes_off_host_verification",
            "Restore drill was not run from a previously verified off-host object.",
        )
    retrieval_epoch = _parse_utc_iso(retrieval.get("retrieved_at"))
    if retrieval_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS < backup_completed_epoch:
        raise DisasterRecoveryError(
            "release_retrieval_predates_backup",
            "Off-host retrieval predates the completed backup.",
        )
    if retrieval_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS < off_host_verified_epoch:
        raise DisasterRecoveryError(
            "release_retrieval_predates_verification",
            "Off-host retrieval predates provider verification of the immutable backup version.",
        )
    if retrieval_epoch > restore_completed_epoch + DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS:
        raise DisasterRecoveryError(
            "release_retrieval_after_restore",
            "Off-host retrieval timestamp is later than the restore drill completion.",
        )
    if max(0.0, started - off_host_verified_epoch) > max_age_seconds:
        raise DisasterRecoveryError(
            "release_off_host_verification_stale",
            "Off-host object verification is too old for launch.",
        )

    backup_source = dict(backup.get("source") or {})
    restore_source = dict(restore.get("source") or {})
    target = dict(restore.get("target") or {})
    try:
        source_key = _identity_key(backup_source)
        restore_source_key = _identity_key(restore_source)
        target_key = _identity_key(target)
    except Exception as exc:
        raise DisasterRecoveryError(
            "release_database_identity_invalid",
            "Backup or restore database identity is invalid.",
        ) from exc
    if not all(source_key) or source_key != restore_source_key:
        raise DisasterRecoveryError(
            "release_source_identity_mismatch",
            "Restore receipt source database identity does not match the backup source.",
        )
    if target_key == source_key:
        raise DisasterRecoveryError(
            "release_restore_target_matches_source",
            "Restore evidence target database matches the backup source.",
        )
    target_database = str(target.get("database") or "").strip().lower()
    if not target_database.startswith(DEFAULT_DISPOSABLE_PREFIX):
        raise DisasterRecoveryError(
            "release_restore_target_not_disposable",
            "Restore evidence target is not a PropertyQuarry disposable drill database.",
        )
    objectives = dict(restore.get("objectives") or {})
    verification = dict(restore.get("verification") or {})
    if objectives.get("rpo_met") is not True or objectives.get("rto_met") is not True:
        raise DisasterRecoveryError(
            "release_recovery_objectives_failed",
            "Restore drill did not meet both configured RPO and RTO.",
        )
    if list(objectives.get("rto_scope") or []) != list(RESTORE_RTO_SCOPE):
        raise DisasterRecoveryError(
            "release_recovery_objective_scope_invalid",
            "Restore RTO does not cover retrieval, decryption, archive validation, restore, and verification.",
        )
    required_verification = (
        "artifact_checksum_valid",
        "target_disposable_guard",
        "target_identity_valid",
        "plaintext_checksum_valid",
        "custom_format_list_valid",
        "source_schema_contract_valid",
        "source_snapshot_contract_valid",
        "restored_schema_contract_valid",
        "migration_hook_passed",
        "schema_migration_forward_verified",
        "verification_hook_passed",
        "readiness_hook_passed",
        "release_identity_matches_backup",
        "off_host_object_verified",
        "off_host_retrieval_verified",
        "aws_cli_attested",
        "critical_data_contract_valid",
        "critical_data_table_set_exact",
        "critical_data_exact_match",
    )
    missing_verification = [key for key in required_verification if verification.get(key) is not True]
    if missing_verification:
        raise DisasterRecoveryError(
            "release_restore_verification_incomplete",
            "Restore drill is missing required launch verification evidence.",
            details={"missing_verification": missing_verification},
        )
    try:
        schema_table_count = int(verification.get("schema_table_count") or 0)
    except Exception as exc:
        raise DisasterRecoveryError(
            "release_schema_table_count_invalid",
            "Restore drill schema table count is invalid.",
        ) from exc
    if schema_table_count <= 0:
        raise DisasterRecoveryError(
            "release_schema_empty",
            "Restore drill did not prove a non-empty restored schema.",
        )

    completed = clock()
    receipt = _base_receipt(
        operation="release_gate",
        started_epoch=started,
        runtime_mode="release",
    )
    receipt.update(
        {
            "status": "pass",
            "completed_at": _utc_iso(completed),
            "duration_seconds": round(max(0.0, completed - started), 3),
            "release": expected_release,
            "source_schema": source_schema,
            "source_snapshot": source_snapshot,
            "restored_schema": restored_schema,
            "source_critical_data": source_critical_data,
            "restored_critical_data": restored_critical_data,
            "off_host_object": off_host,
            "off_host_retrieval": retrieval,
            "aws_cli": retrieval["aws_cli"],
            "evidence": {
                "backup_receipt": str(backup_input_path),
                "restore_receipt": str(restore_input_path),
                "backup_completed_at": backup.get("completed_at"),
                "restore_completed_at": restore.get("completed_at"),
                "backup_age_seconds": round(backup_age, 3),
                "restore_age_seconds": round(restore_age, 3),
                "max_age_seconds": max_age_seconds,
                "target": target,
                "objectives": objectives,
                "source_snapshot": source_snapshot,
                "critical_data_contract": {
                    "contract_name": source_critical_data["contract_name"],
                    "contract_version": source_critical_data["contract_version"],
                    "evidence_version": source_critical_data["evidence_version"],
                    "contract_fingerprint_sha256": source_critical_data[
                        "contract_fingerprint_sha256"
                    ],
                    "fingerprint_algorithm": source_critical_data["fingerprint_algorithm"],
                    "chunk_size": source_critical_data["chunk_size"],
                    "max_row_bytes": source_critical_data["max_row_bytes"],
                    "max_chunks": source_critical_data["max_chunks"],
                    "max_supported_rows": source_critical_data["max_supported_rows"],
                    "governing_schema_version": source_critical_data[
                        "governing_schema_version"
                    ],
                    "table_set_version": source_critical_data["table_set_version"],
                    "privacy_lifecycle_required": source_critical_data[
                        "privacy_lifecycle_required"
                    ],
                    "governed_table_names": [
                        row["table"] for row in source_critical_data["tables"]
                    ],
                },
                "critical_data_tables": [
                    {
                        "schema": row["schema"],
                        "table": row["table"],
                        "row_count": row["row_count"],
                        "chunk_count": row["chunk_count"],
                        "merkle_root_sha256": row["merkle_root_sha256"],
                        "fingerprint_sha256": row["fingerprint_sha256"],
                    }
                    for row in source_critical_data["tables"]
                ],
            },
            "verification": {
                "release_identity_exact": True,
                "encrypted_backup": True,
                "off_host_object_exact": True,
                "off_host_retrieval_exact": True,
                "aws_cli_attestation_exact": True,
                "source_schema_prefix_valid": True,
                "source_snapshot_exact": True,
                "restored_schema_exact": True,
                "schema_migration_forward_verified": True,
                "disposable_restore": True,
                "rpo_met": True,
                "rto_met": True,
                "verification_hook_passed": True,
                "readiness_hook_passed": True,
                "critical_data_contract_valid": True,
                "critical_data_table_set_exact": True,
                "critical_data_exact_match": True,
                "evidence_fresh": True,
            },
        }
    )
    return receipt


def _failure_receipt(
    *,
    operation: str,
    started_epoch: float,
    runtime_mode: str,
    error: BaseException,
    clock: Clock,
) -> dict[str, object]:
    completed = clock()
    code = error.code if isinstance(error, DisasterRecoveryError) else "unexpected_error"
    details = error.details if isinstance(error, DisasterRecoveryError) else {}
    receipt = _base_receipt(operation=operation, started_epoch=started_epoch, runtime_mode=runtime_mode)
    receipt.update(
        {
            "status": "fail",
            "completed_at": _utc_iso(completed),
            "duration_seconds": round(max(0.0, completed - started_epoch), 3),
            "error": {"code": code, "message": _redact_text(error)},
        }
    )
    receipt.update(details)
    return receipt


def _validate_receipt_destination(receipt_path: Path, protected_paths: Sequence[Path]) -> None:
    receipt = receipt_path.expanduser().resolve()
    for protected_path in protected_paths:
        if receipt == protected_path.expanduser().resolve():
            raise DisasterRecoveryError(
                "receipt_path_conflict",
                "Receipt path must be different from every backup artifact and input receipt path.",
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    backup = subparsers.add_parser("backup", help="Create and validate a custom-format Postgres backup.")
    backup.add_argument("--artifact", required=True, type=Path)
    backup.add_argument("--receipt", required=True, type=Path)
    backup.add_argument("--overwrite", action="store_true")
    restore = subparsers.add_parser("restore-drill", help="Restore a verified backup into a disposable target.")
    restore.add_argument("--artifact", required=True, type=Path)
    restore.add_argument("--backup-receipt", required=True, type=Path)
    restore.add_argument("--receipt", required=True, type=Path)
    release = subparsers.add_parser(
        "release-gate",
        help="Fail closed unless current DR evidence is bound to the exact release and schema.",
    )
    release.add_argument("--backup-receipt", required=True, type=Path)
    release.add_argument("--restore-receipt", required=True, type=Path)
    release.add_argument(
        "--release-commit-sha",
        default=os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA", ""),
    )
    release.add_argument(
        "--image-digest",
        default=os.environ.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", ""),
    )
    release.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
    )
    release.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    protected_paths: list[Path] = []
    if args.operation in {"backup", "restore-drill"}:
        protected_paths.append(args.artifact)
    if args.operation == "restore-drill":
        protected_paths.append(args.backup_receipt)
    elif args.operation == "release-gate":
        protected_paths.extend((args.backup_receipt, args.restore_receipt))
    try:
        _validate_receipt_destination(args.receipt, protected_paths)
    except DisasterRecoveryError as exc:
        print(f"PropertyQuarry Postgres {args.operation} refused: {exc}", file=sys.stderr)
        return 2
    started = time.time()
    runtime_mode = str(os.environ.get("EA_RUNTIME_MODE") or "dev").strip().lower() or "dev"
    try:
        if args.operation == "backup":
            receipt = execute_backup(
                artifact_path=args.artifact,
                overwrite=bool(args.overwrite),
            )
        elif args.operation == "restore-drill":
            receipt = execute_restore_drill(
                artifact_path=args.artifact,
                backup_receipt_path=args.backup_receipt,
            )
        else:
            max_age_seconds = (
                float(args.max_age_seconds)
                if args.max_age_seconds is not None
                else _float_env(
                    os.environ,
                    "PROPERTYQUARRY_DR_RELEASE_MAX_AGE_SECONDS",
                    DEFAULT_RELEASE_EVIDENCE_MAX_AGE_SECONDS,
                )
            )
            receipt = verify_release_dr_evidence(
                backup_receipt_path=args.backup_receipt,
                restore_receipt_path=args.restore_receipt,
                release_commit_sha=args.release_commit_sha,
                image_digest=args.image_digest,
                max_age_seconds=max_age_seconds,
            )
    except BaseException as exc:
        receipt = _failure_receipt(
            operation=str(args.operation or "unknown").replace("-", "_"),
            started_epoch=started,
            runtime_mode=runtime_mode,
            error=exc,
            clock=time.time,
        )
        _atomic_receipt(args.receipt, receipt)
        print(f"PropertyQuarry Postgres {args.operation} failed: {_redact_text(exc)}", file=sys.stderr)
        return 1
    _atomic_receipt(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
