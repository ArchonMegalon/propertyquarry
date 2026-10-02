#!/usr/bin/env python3
"""Execute one explicitly approved scheduler activation exactly once."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_scheduler_activation_authorization as authorization
from scripts import propertyquarry_ooda_scheduler_activation_decision as decision
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness
from scripts.propertyquarry_secure_file_io import (
    OutputExistsError,
    SecureFileIOError,
    atomic_write_bytes,
)
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


CLAIM_SCHEMA = "propertyquarry.ooda_scheduler_activation_execution_claim.v1"
OBSERVATION_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_deployment_observation.v1"
)
RESULT_SCHEMA = "propertyquarry.ooda_scheduler_activation_execution_result.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_scheduler_activation_execution_verification.v1"
HISTORY_SCHEMA = "propertyquarry.ooda_scheduler_activation_execution_history.v1"
DEFAULT_EXECUTION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-executions"
)
DEFAULT_DEPLOYMENT_RECEIPT_PATH = (
    ROOT / "state/release/propertyquarry-local-deployment.v1.json"
)
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 90
DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_AGE_SECONDS = 300.0
MAX_OBJECT_BYTES = 512 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
DEPLOY_SCRIPT = readiness.DEPLOY_SCRIPT
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CLAIM_ID = re.compile(r"pqsaec_[0-9a-f]{24}\Z")
_DECISION_ID = re.compile(r"pqsad_[0-9a-f]{24}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_DEPLOYED = re.compile(
    r"DEPLOYED local Docker deployment "
    r"runtime=([0-9a-f]{40}) "
    r"envelope=([0-9a-f]{40}) "
    r"receipt=(/[^\r\n]+)\Z"
)
_CLAIM_KEYS = {
    "schema",
    "claim_id",
    "status",
    "claimed_at",
    "expires_at",
    "request_id",
    "request_sha256",
    "semantic_request_sha256",
    "decision_id",
    "decision_sha256",
    "scope",
    "release_evidence",
    "deployment_argv",
    "authorization_consumed",
    "automatic_execution_allowed",
    "deployment_attempted",
    "deployment_or_restart_performed",
    "protected_operation_executed",
    "provider_quota_consumption_allowed",
    "delivery_authorized",
    "delivery_attempted",
    "sent",
    "secret_values_recorded",
    "integrity",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return authorization._canonical(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime | None:
    return authorization._parse_timestamp(value)


def _integrity(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(value)),
    }


def _integrity_valid(value: Mapping[str, Any]) -> bool:
    normalized = dict(value)
    integrity = normalized.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _private_object(
    path: Path,
    *,
    field: str,
) -> tuple[dict[str, Any], str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{field}_file_not_admissible")
    payload, _raw, digest = load_strict_json_object_snapshot(
        target,
        field=field,
        maximum_bytes=MAX_OBJECT_BYTES,
    )
    return payload, digest


def _private_directory(path: Path, *, create: bool) -> Path:
    target = Path(path).absolute()
    if not target.exists() and create:
        target.mkdir(parents=True, mode=0o700)
    metadata = target.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("scheduler_activation_execution_dir_not_admissible")
    return target


def _paths(execution_dir: Path, decision_id: str) -> tuple[Path, Path]:
    if _DECISION_ID.fullmatch(decision_id) is None:
        raise ValueError("scheduler_activation_execution_decision_id_invalid")
    target = Path(execution_dir).absolute()
    return (
        target / f"claim--{decision_id}.json",
        target / f"result--{decision_id}.json",
    )


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "execution_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate and explicitly approve a current exact activation request",
        "action_required": False,
        "interrupt_operator": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "authorization_consumed": False,
        "exact_scope_authorized": False,
        "manual_deployment_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "actions": [],
        "progress": {"current_evidence_verified": False},
    }


def _decision_admissible(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("status") == "verified"
        and value.get("decision") == "approve_exact_scope"
        and value.get("authorization_recorded") is True
        and value.get("exact_scope_authorized") is True
        and value.get("manual_deployment_authorized") is True
        and value.get("execution_authorized") is True
        and value.get("deployment_or_restart_authorized") is True
        and value.get("automatic_execution_allowed") is False
        and value.get("deployment_or_restart_performed") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and _DECISION_ID.fullmatch(str(value.get("decision_id") or ""))
        and _SHA256.fullmatch(str(value.get("decision_sha256") or ""))
        and authorization._REQUEST_ID.fullmatch(
            str(value.get("request_id") or "")
        )
        and _SHA256.fullmatch(str(value.get("request_sha256") or ""))
    )


def _deployment_argv(
    *,
    release_evidence: Mapping[str, Any],
) -> list[str]:
    return [
        "/usr/bin/bash",
        str(DEPLOY_SCRIPT),
        "--no-build",
        "--expected-runtime-commit",
        str(release_evidence.get("runtime_commit_sha") or ""),
        "--expected-envelope-commit",
        str(release_evidence.get("envelope_commit_sha") or ""),
        "--expected-web-image",
        str(release_evidence.get("web_image_digest") or ""),
        "--expected-render-image",
        str(release_evidence.get("render_image_digest") or ""),
    ]


def build_scheduler_activation_execution_claim(
    *,
    decision_verification: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    claimed_at = _now(now)
    if not _decision_admissible(decision_verification):
        raise ValueError("scheduler_activation_execution_decision_not_admissible")
    expires_at = _timestamp(decision_verification.get("expires_at"))
    if expires_at is None or claimed_at > expires_at:
        raise ValueError("scheduler_activation_execution_decision_not_current")
    release_evidence = dict(
        decision_verification.get("release_evidence") or {}
    )
    scope = dict(decision_verification.get("scope") or {})
    if not (
        _PROJECT.fullmatch(str(scope.get("compose_project") or ""))
        and _COMMIT.fullmatch(
            str(release_evidence.get("runtime_commit_sha") or "")
        )
        and _COMMIT.fullmatch(
            str(release_evidence.get("envelope_commit_sha") or "")
        )
        and _IMAGE.fullmatch(
            str(release_evidence.get("web_image_digest") or "")
        )
        and _IMAGE.fullmatch(
            str(release_evidence.get("render_image_digest") or "")
        )
        and _SHA256.fullmatch(
            str(release_evidence.get("preflight_script_sha256") or "")
        )
    ):
        raise ValueError("scheduler_activation_execution_release_not_admissible")
    identity = {
        "decision_id": str(decision_verification["decision_id"]),
        "decision_sha256": str(decision_verification["decision_sha256"]),
    }
    identity_digest = _sha256(_canonical(identity))
    claim: dict[str, Any] = {
        "schema": CLAIM_SCHEMA,
        "claim_id": f"pqsaec_{identity_digest[:24]}",
        "status": "claimed",
        "claimed_at": claimed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "request_id": str(decision_verification.get("request_id") or ""),
        "request_sha256": str(
            decision_verification.get("request_sha256") or ""
        ),
        "semantic_request_sha256": str(
            decision_verification.get("semantic_request_sha256") or ""
        ),
        "decision_id": identity["decision_id"],
        "decision_sha256": identity["decision_sha256"],
        "scope": scope,
        "release_evidence": release_evidence,
        "deployment_argv": _deployment_argv(
            release_evidence=release_evidence
        ),
        "authorization_consumed": True,
        "automatic_execution_allowed": False,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }
    claim["integrity"] = _integrity(claim)
    return claim


def verify_scheduler_activation_execution_claim_history(
    claim: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify an immutable claim without requiring its expired live request."""

    observed_now = _now(now)
    claimed_at = _timestamp(claim.get("claimed_at"))
    expires_at = _timestamp(claim.get("expires_at"))
    scope = claim.get("scope")
    release = claim.get("release_evidence")
    decision_id = str(claim.get("decision_id") or "")
    decision_digest = str(claim.get("decision_sha256") or "")
    expected_claim_digest = (
        _sha256(
            _canonical(
                {
                    "decision_id": decision_id,
                    "decision_sha256": decision_digest,
                }
            )
        )
        if decision_id and decision_digest
        else ""
    )
    expected_scope = (
        {
            **authorization._EXPECTED_SCOPE,
            "compose_project": scope.get("compose_project"),
        }
        if isinstance(scope, Mapping)
        else {}
    )
    if not (
        set(claim) == _CLAIM_KEYS
        and claim.get("schema") == CLAIM_SCHEMA
        and claim.get("status") == "claimed"
        and _integrity_valid(claim)
        and claimed_at is not None
        and expires_at is not None
        and claimed_at <= expires_at
        and _CLAIM_ID.fullmatch(str(claim.get("claim_id") or ""))
        and claim.get("claim_id")
        == f"pqsaec_{expected_claim_digest[:24]}"
        and authorization._REQUEST_ID.fullmatch(
            str(claim.get("request_id") or "")
        )
        and _SHA256.fullmatch(str(claim.get("request_sha256") or ""))
        and _SHA256.fullmatch(
            str(claim.get("semantic_request_sha256") or "")
        )
        and _DECISION_ID.fullmatch(decision_id)
        and _SHA256.fullmatch(decision_digest)
        and isinstance(scope, Mapping)
        and dict(scope) == expected_scope
        and _PROJECT.fullmatch(str(scope.get("compose_project") or ""))
        and isinstance(release, Mapping)
        and _COMMIT.fullmatch(str(release.get("runtime_commit_sha") or ""))
        and _COMMIT.fullmatch(str(release.get("envelope_commit_sha") or ""))
        and _IMAGE.fullmatch(str(release.get("web_image_digest") or ""))
        and _IMAGE.fullmatch(str(release.get("render_image_digest") or ""))
        and _SHA256.fullmatch(
            str(release.get("preflight_script_sha256") or "")
        )
        and claim.get("deployment_argv")
        == _deployment_argv(release_evidence=release)
        and claim.get("authorization_consumed") is True
        and claim.get("automatic_execution_allowed") is False
        and claim.get("deployment_attempted") is False
        and claim.get("deployment_or_restart_performed") is False
        and claim.get("protected_operation_executed") is False
        and claim.get("provider_quota_consumption_allowed") is False
        and claim.get("delivery_authorized") is False
        and claim.get("delivery_attempted") is False
        and claim.get("sent") is False
        and claim.get("secret_values_recorded") is False
    ):
        return _blocked(
            "scheduler_activation_execution_historical_claim_not_admissible",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": "claimed",
        "updated_at": observed_now.isoformat(),
        "claim_id": str(claim.get("claim_id") or ""),
        "claim_sha256": _sha256(_canonical(claim)),
        "claimed_at": claimed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "request_id": str(claim.get("request_id") or ""),
        "request_sha256": str(claim.get("request_sha256") or ""),
        "decision_id": decision_id,
        "decision_sha256": decision_digest,
        "scope": dict(scope),
        "release_evidence": dict(release),
        "authorization_recorded": True,
        "authorization_consumed": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"execution_claim_integrity_verified": True},
    }


def verify_scheduler_activation_execution_claim(
    claim: Mapping[str, Any],
    *,
    decision_verification: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    claimed_at = _timestamp(claim.get("claimed_at"))
    historical = verify_scheduler_activation_execution_claim_history(
        claim,
        now=observed_now,
    )
    if claimed_at is None or historical.get("status") != "verified":
        return _blocked(
            "scheduler_activation_execution_claim_integrity_invalid",
            now=observed_now,
        )
    try:
        expected = build_scheduler_activation_execution_claim(
            decision_verification=decision_verification,
            now=claimed_at,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_activation_execution_claim_source_not_admissible",
            now=observed_now,
        )
    if dict(claim) != expected or _CLAIM_ID.fullmatch(
        str(claim.get("claim_id") or "")
    ) is None:
        return _blocked(
            "scheduler_activation_execution_claim_binding_mismatch",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": "claimed",
        "updated_at": observed_now.isoformat(),
        "claim_id": str(claim.get("claim_id") or ""),
        "claim_sha256": _sha256(_canonical(claim)),
        "claimed_at": claimed_at.isoformat(),
        "expires_at": str(claim.get("expires_at") or ""),
        "request_id": str(claim.get("request_id") or ""),
        "request_sha256": str(claim.get("request_sha256") or ""),
        "decision_id": str(claim.get("decision_id") or ""),
        "decision_sha256": str(claim.get("decision_sha256") or ""),
        "scope": dict(claim.get("scope") or {}),
        "release_evidence": dict(claim.get("release_evidence") or {}),
        "authorization_consumed": True,
        "automatic_execution_allowed": False,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": True},
    }


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode(errors="replace")


def _deployment_observation(
    *,
    status: str,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    exit_code: int,
    blocking_reason: str,
    stdout: bytes,
    stderr: bytes,
    release_evidence: Mapping[str, Any],
    project: str,
    deployment_receipt_path: Path,
    reported_runtime_commit_sha: str = "",
    reported_envelope_commit_sha: str = "",
) -> dict[str, Any]:
    return {
        "schema": OBSERVATION_SCHEMA,
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "blocking_reason": blocking_reason,
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "compose_project": project,
        "expected_release_evidence": dict(release_evidence),
        "reported_runtime_commit_sha": reported_runtime_commit_sha,
        "reported_envelope_commit_sha": reported_envelope_commit_sha,
        "deployment_receipt_path": str(Path(deployment_receipt_path).absolute()),
        "deployment_attempted": True,
        "deployment_or_restart_performed": True,
        "protected_operation_executed": True,
        "provider_quota_consumed": False,
        "delivery_attempted": False,
        "secret_values_recorded": False,
    }


def run_scheduler_activation_deployment(
    *,
    release_evidence: Mapping[str, Any],
    project: str,
    deployment_receipt_path: Path,
    timeout_seconds: int = DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 60 <= timeout_seconds <= 1800
    ):
        raise ValueError("scheduler_activation_deployment_timeout_not_admissible")
    started_at = _now()
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PROPERTYQUARRY_COMPOSE_PROJECT_NAME"] = project
    environment["PROPERTYQUARRY_LOCAL_DEPLOYMENT_RECEIPT"] = str(
        Path(deployment_receipt_path).absolute()
    )
    environment[
        "PROPERTYQUARRY_GOVERNED_EXPECTED_DEPLOY_SCRIPT_SHA256"
    ] = str(release_evidence.get("preflight_script_sha256") or "")
    process: subprocess.Popen[bytes] | None = None
    stdout = b""
    stderr = b""
    exit_code = 126
    status = "failed"
    reason = "activation_deployment_process_unavailable"
    try:
        process = subprocess.Popen(
            _deployment_argv(release_evidence=release_evidence),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code = int(process.returncode or 0)
        except subprocess.TimeoutExpired as exc:
            stdout = _output_bytes(exc.output)
            stderr = _output_bytes(exc.stderr)
            exit_code = 124
            status = "timeout"
            reason = "activation_deployment_timeout"
            try:
                os.killpg(process.pid, signal.SIGTERM)
                more_stdout, more_stderr = process.communicate(timeout=5)
                stdout += more_stdout
                stderr += more_stderr
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
        if exit_code != 124:
            status = "succeeded" if exit_code == 0 else "failed"
            reason = "" if exit_code == 0 else "activation_deployment_failed"
    except OSError:
        pass
    completed_at = _now()
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    bounded_stdout = stdout[:MAX_CAPTURE_BYTES]
    bounded_stderr = stderr[:MAX_CAPTURE_BYTES]
    if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
        status = "failed"
        reason = "activation_deployment_output_too_large"
    match = None
    if status == "succeeded" and not stderr:
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        match = _DEPLOYED.fullmatch(lines[-1] if lines else "")
    if match is None and status == "succeeded":
        status = "failed"
        reason = "activation_deployment_output_not_admissible"
    reported_runtime = match.group(1) if match is not None else ""
    reported_envelope = match.group(2) if match is not None else ""
    reported_receipt = match.group(3) if match is not None else ""
    if match is not None and reported_receipt != str(
        Path(deployment_receipt_path).absolute()
    ):
        status = "failed"
        reason = "activation_deployment_receipt_path_mismatch"
    return _deployment_observation(
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
        blocking_reason=reason,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        release_evidence=release_evidence,
        project=project,
        deployment_receipt_path=deployment_receipt_path,
        reported_runtime_commit_sha=reported_runtime,
        reported_envelope_commit_sha=reported_envelope,
    )


def _project_preflight(
    value: Mapping[str, Any],
    *,
    release_evidence: Mapping[str, Any],
    project: str,
    now: datetime,
) -> dict[str, Any]:
    projected = readiness._preflight_projection(
        value,
        now=now,
        max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
        project=project,
    )
    if projected.get("status") != "ready":
        raise ValueError(
            str(projected.get("blocking_reason") or "activation_preflight_not_ready")
        )
    expected = {
        "runtime_commit_sha": projected.get("runtime_commit_sha"),
        "envelope_commit_sha": projected.get("envelope_commit_sha"),
        "web_image_digest": projected.get("web_image_digest"),
        "render_image_digest": projected.get("render_image_digest"),
        "preflight_script_sha256": projected.get("preflight_script_sha256"),
    }
    if expected != dict(release_evidence):
        raise ValueError("authorized_release_evidence_changed")
    return projected


def _project_deployment(
    value: Mapping[str, Any],
    *,
    release_evidence: Mapping[str, Any],
    project: str,
    deployment_receipt_path: Path,
) -> dict[str, Any]:
    required = {
        "schema",
        "status",
        "started_at",
        "completed_at",
        "duration_ms",
        "exit_code",
        "blocking_reason",
        "stdout_sha256",
        "stderr_sha256",
        "stdout_bytes",
        "stderr_bytes",
        "compose_project",
        "expected_release_evidence",
        "reported_runtime_commit_sha",
        "reported_envelope_commit_sha",
        "deployment_receipt_path",
        "deployment_attempted",
        "deployment_or_restart_performed",
        "protected_operation_executed",
        "provider_quota_consumed",
        "delivery_attempted",
        "secret_values_recorded",
    }
    status_value = str(value.get("status") or "")
    started_at = _timestamp(value.get("started_at"))
    completed_at = _timestamp(value.get("completed_at"))
    duration = value.get("duration_ms")
    exit_code = value.get("exit_code")
    if not (
        set(value) == required
        and value.get("schema") == OBSERVATION_SCHEMA
        and status_value in {"succeeded", "failed", "timeout"}
        and started_at is not None
        and completed_at is not None
        and completed_at >= started_at
        and isinstance(duration, int)
        and not isinstance(duration, bool)
        and 0 <= duration <= 1_800_000
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and 0 <= exit_code <= 255
        and _SHA256.fullmatch(str(value.get("stdout_sha256") or ""))
        and _SHA256.fullmatch(str(value.get("stderr_sha256") or ""))
        and isinstance(value.get("stdout_bytes"), int)
        and 0 <= int(value.get("stdout_bytes") or 0) <= MAX_CAPTURE_BYTES
        and isinstance(value.get("stderr_bytes"), int)
        and 0 <= int(value.get("stderr_bytes") or 0) <= MAX_CAPTURE_BYTES
        and value.get("compose_project") == project
        and dict(value.get("expected_release_evidence") or {})
        == dict(release_evidence)
        and value.get("deployment_receipt_path")
        == str(Path(deployment_receipt_path).absolute())
        and value.get("deployment_attempted") is True
        and value.get("deployment_or_restart_performed") is True
        and value.get("protected_operation_executed") is True
        and value.get("provider_quota_consumed") is False
        and value.get("delivery_attempted") is False
        and value.get("secret_values_recorded") is False
        and (
            status_value != "succeeded"
            or (
                exit_code == 0
                and value.get("blocking_reason") == ""
                and value.get("reported_runtime_commit_sha")
                == release_evidence.get("runtime_commit_sha")
                and value.get("reported_envelope_commit_sha")
                == release_evidence.get("envelope_commit_sha")
            )
        )
        and (
            status_value == "succeeded"
            or str(value.get("blocking_reason") or "").strip()
        )
    ):
        raise ValueError("scheduler_activation_deployment_observation_not_admissible")
    return dict(value)


def _verify_runtime_receipt(
    path: Path,
    *,
    release_evidence: Mapping[str, Any],
    project: str,
    now: datetime,
) -> dict[str, Any]:
    payload, digest = _private_object(
        path,
        field="scheduler_activation_runtime_deployment_receipt",
    )
    observed_at = _timestamp(payload.get("observed_at"))
    age = (now - observed_at).total_seconds() if observed_at else math.inf
    compose = payload.get("compose")
    images = payload.get("images")
    authority_row = payload.get("authority")
    services = payload.get("services")
    scheduler = (
        services.get("propertyquarry-scheduler")
        if isinstance(services, Mapping)
        else None
    )
    if not (
        payload.get("schema") == "propertyquarry.local_docker_deployment.v1"
        and payload.get("passed") is True
        and payload.get("failures") == []
        and payload.get("secret_values_recorded") is False
        and observed_at is not None
        and math.isfinite(age)
        and 0 <= age <= DEFAULT_MAX_AGE_SECONDS
        and payload.get("runtime_commit_sha")
        == release_evidence.get("runtime_commit_sha")
        and payload.get("envelope_head_sha")
        == release_evidence.get("envelope_commit_sha")
        and isinstance(images, Mapping)
        and images.get("web") == release_evidence.get("web_image_digest")
        and images.get("render") == release_evidence.get("render_image_digest")
        and isinstance(compose, Mapping)
        and compose.get("project") == project
        and isinstance(authority_row, Mapping)
        and authority_row.get("scope") == "local_docker"
        and authority_row.get("github_actions_used") is False
        and isinstance(scheduler, Mapping)
        and scheduler.get("status") == "running"
        and scheduler.get("health") == "healthy"
        and scheduler.get("image_id")
        == release_evidence.get("web_image_digest")
    ):
        raise ValueError("scheduler_activation_runtime_receipt_not_admissible")
    return {
        "status": "verified",
        "path": str(Path(path).absolute()),
        "sha256": digest,
        "observed_at": observed_at.isoformat(),
        "runtime_commit_sha": str(payload.get("runtime_commit_sha") or ""),
        "envelope_commit_sha": str(payload.get("envelope_head_sha") or ""),
        "web_image_digest": str(images.get("web") or ""),
        "render_image_digest": str(images.get("render") or ""),
        "scheduler_status": str(scheduler.get("status") or ""),
        "scheduler_health": str(scheduler.get("health") or ""),
    }


def _build_result(
    *,
    claim: Mapping[str, Any],
    status: str,
    blocking_reason: str,
    preflight_observation: Mapping[str, Any],
    deployment_observation: Mapping[str, Any] | None,
    runtime_receipt: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    deployment_attempted = deployment_observation is not None
    succeeded = status == "succeeded"
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "completed_at": now.isoformat(),
        "blocking_reason": blocking_reason,
        "claim_id": str(claim.get("claim_id") or ""),
        "claim_sha256": _sha256(_canonical(claim)),
        "request_id": str(claim.get("request_id") or ""),
        "request_sha256": str(claim.get("request_sha256") or ""),
        "decision_id": str(claim.get("decision_id") or ""),
        "decision_sha256": str(claim.get("decision_sha256") or ""),
        "scope": dict(claim.get("scope") or {}),
        "release_evidence": dict(claim.get("release_evidence") or {}),
        "preflight_observation": dict(preflight_observation),
        "deployment_observation": (
            dict(deployment_observation) if deployment_observation else {}
        ),
        "runtime_receipt": dict(runtime_receipt or {}),
        "authorization_recorded": True,
        "authorization_consumed": True,
        "exact_scope_authorized": True,
        "manual_deployment_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": deployment_attempted,
        "deployment_or_restart_performed": deployment_attempted,
        "protected_operation_executed": deployment_attempted,
        "runtime_receipt_verified": succeeded,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }
    result["integrity"] = _integrity(result)
    return result


def verify_scheduler_activation_execution_result(
    result: Mapping[str, Any],
    *,
    claim: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    status_value = str(result.get("status") or "")
    completed_at = _timestamp(result.get("completed_at"))
    deployment_attempted = status_value in {"deployment_failed", "succeeded"}
    claim_valid = bool(
        claim.get("schema") == CLAIM_SCHEMA
        and _integrity_valid(claim)
        and claim.get("claim_id") == result.get("claim_id")
    )
    deployment_valid = not deployment_attempted
    runtime_receipt_valid = status_value != "succeeded"
    if deployment_attempted:
        try:
            deployment_row = dict(result.get("deployment_observation") or {})
            _project_deployment(
                deployment_row,
                release_evidence=dict(result.get("release_evidence") or {}),
                project=str(dict(result.get("scope") or {}).get("compose_project") or ""),
                deployment_receipt_path=Path(
                    str(deployment_row.get("deployment_receipt_path") or "")
                ),
            )
            deployment_valid = True
        except (OSError, TypeError, ValueError):
            deployment_valid = False
    if status_value == "succeeded":
        runtime_row = result.get("runtime_receipt")
        release = dict(result.get("release_evidence") or {})
        runtime_receipt_valid = bool(
            isinstance(runtime_row, Mapping)
            and runtime_row.get("status") == "verified"
            and _SHA256.fullmatch(str(runtime_row.get("sha256") or ""))
            and runtime_row.get("runtime_commit_sha")
            == release.get("runtime_commit_sha")
            and runtime_row.get("envelope_commit_sha")
            == release.get("envelope_commit_sha")
            and runtime_row.get("web_image_digest")
            == release.get("web_image_digest")
            and runtime_row.get("render_image_digest")
            == release.get("render_image_digest")
            and runtime_row.get("scheduler_status") == "running"
            and runtime_row.get("scheduler_health") == "healthy"
        )
    if not (
        _integrity_valid(result)
        and claim_valid
        and deployment_valid
        and runtime_receipt_valid
        and result.get("schema") == RESULT_SCHEMA
        and status_value
        in {"blocked_before_execution", "deployment_failed", "succeeded"}
        and completed_at is not None
        and result.get("claim_id") == claim.get("claim_id")
        and result.get("claim_sha256") == _sha256(_canonical(claim))
        and result.get("request_id") == claim.get("request_id")
        and result.get("request_sha256") == claim.get("request_sha256")
        and result.get("decision_id") == claim.get("decision_id")
        and result.get("decision_sha256") == claim.get("decision_sha256")
        and dict(result.get("scope") or {}) == dict(claim.get("scope") or {})
        and dict(result.get("release_evidence") or {})
        == dict(claim.get("release_evidence") or {})
        and isinstance(result.get("preflight_observation"), Mapping)
        and result.get("authorization_recorded") is True
        and result.get("authorization_consumed") is True
        and result.get("exact_scope_authorized") is True
        and result.get("manual_deployment_authorized") is False
        and result.get("automatic_execution_allowed") is False
        and result.get("execution_authorized") is False
        and result.get("deployment_or_restart_authorized") is False
        and result.get("deployment_attempted") is deployment_attempted
        and result.get("deployment_or_restart_performed")
        is deployment_attempted
        and result.get("protected_operation_executed")
        is deployment_attempted
        and result.get("runtime_receipt_verified") is (status_value == "succeeded")
        and result.get("provider_quota_consumption_allowed") is False
        and result.get("delivery_authorized") is False
        and result.get("delivery_attempted") is False
        and result.get("sent") is False
        and result.get("secret_values_recorded") is False
        and (
            status_value == "succeeded"
            or str(result.get("blocking_reason") or "").strip()
        )
        and (status_value != "succeeded" or result.get("blocking_reason") == "")
        and (
            deployment_attempted
            or dict(result.get("deployment_observation") or {}) == {}
        )
        and (
            status_value == "succeeded"
            or dict(result.get("runtime_receipt") or {}) == {}
        )
    ):
        return _blocked(
            "scheduler_activation_execution_result_not_admissible",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "execution_state": status_value,
        "updated_at": observed_now.isoformat(),
        "completed_at": completed_at.isoformat(),
        "blocking_reason": str(result.get("blocking_reason") or ""),
        "claim_id": str(result.get("claim_id") or ""),
        "claim_sha256": str(result.get("claim_sha256") or ""),
        "request_id": str(result.get("request_id") or ""),
        "request_sha256": str(result.get("request_sha256") or ""),
        "decision_id": str(result.get("decision_id") or ""),
        "decision_sha256": str(result.get("decision_sha256") or ""),
        "scope": dict(result.get("scope") or {}),
        "release_evidence": dict(result.get("release_evidence") or {}),
        "runtime_receipt": dict(result.get("runtime_receipt") or {}),
        "authorization_recorded": True,
        "authorization_consumed": True,
        "exact_scope_authorized": True,
        "manual_deployment_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": deployment_attempted,
        "deployment_or_restart_performed": deployment_attempted,
        "protected_operation_executed": deployment_attempted,
        "runtime_receipt_verified": status_value == "succeeded",
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "action_required": status_value != "succeeded",
        "interrupt_operator": status_value != "succeeded",
        "actions": [],
        "progress": {
            "current_evidence_verified": bool(
                dict(result.get("preflight_observation") or {}).get("schema")
                == readiness.OBSERVATION_SCHEMA
            ),
            "execution_receipt_verified": True,
        },
    }


def _history_blocked(
    reason: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": HISTORY_SCHEMA,
        "status": "blocked",
        "history_state": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "inspect the private activation execution ledger without replaying "
            "or overwriting any claim"
        ),
        "execution_history_present": False,
        "action_required": False,
        "interrupt_operator": False,
        "authorization_recorded": False,
        "authorization_consumed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": False},
    }


def inspect_scheduler_activation_execution_history(
    *,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the latest integrity-verified execution, independent of consent TTL."""

    observed_now = _now(now)
    target_dir = Path(execution_dir).absolute()
    if not target_dir.exists():
        return {
            **_history_blocked(
                "scheduler_activation_execution_history_absent",
                now=observed_now,
            ),
            "status": "verified",
            "history_state": "no_execution_history",
            "blocking_reason": "",
            "next_action": "continue fresh scheduler continuity verification",
            "execution_history_present": False,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": 0,
                "result_count": 0,
            },
        }
    try:
        _private_directory(target_dir, create=False)
        candidates = sorted(target_dir.iterdir(), key=lambda path: path.name)
        if len(candidates) > 512:
            raise ValueError("scheduler_activation_execution_history_too_large")
        claims: dict[str, tuple[dict[str, Any], dict[str, Any], Path]] = {}
        results: dict[str, tuple[dict[str, Any], Path, str]] = {}
        for path in candidates:
            claim_match = re.fullmatch(
                r"claim--(pqsad_[0-9a-f]{24})\.json",
                path.name,
            )
            result_match = re.fullmatch(
                r"result--(pqsad_[0-9a-f]{24})\.json",
                path.name,
            )
            if claim_match is not None:
                decision_id = claim_match.group(1)
                claim, _claim_digest = _private_object(
                    path,
                    field="scheduler_activation_execution_historical_claim",
                )
                verification = (
                    verify_scheduler_activation_execution_claim_history(
                        claim,
                        now=observed_now,
                    )
                )
                if not (
                    verification.get("status") == "verified"
                    and verification.get("decision_id") == decision_id
                    and decision_id not in claims
                ):
                    raise ValueError(
                        "scheduler_activation_execution_history_claim_not_admissible"
                    )
                claims[decision_id] = (claim, verification, path)
            elif result_match is not None:
                decision_id = result_match.group(1)
                result, result_digest = _private_object(
                    path,
                    field="scheduler_activation_execution_historical_result",
                )
                if (
                    result.get("decision_id") != decision_id
                    or decision_id in results
                ):
                    raise ValueError(
                        "scheduler_activation_execution_history_result_not_admissible"
                    )
                results[decision_id] = (result, path, result_digest)
            else:
                raise ValueError(
                    "scheduler_activation_execution_history_entry_not_admissible"
                )
        if set(results) - set(claims):
            raise ValueError(
                "scheduler_activation_execution_history_result_without_claim"
            )
        rows: list[dict[str, Any]] = []
        for decision_id, (claim, claim_verification, claim_path) in claims.items():
            row = {
                **claim_verification,
                "history_state": "claimed_without_result",
                "claim_path": str(claim_path),
                "result_path": "",
                "result_sha256": "",
            }
            if decision_id in results:
                result, result_path, result_digest = results[decision_id]
                result_verification = (
                    verify_scheduler_activation_execution_result(
                        result,
                        claim=claim,
                        now=observed_now,
                    )
                )
                if result_verification.get("status") != "verified":
                    raise ValueError(
                        "scheduler_activation_execution_history_result_not_admissible"
                    )
                row = {
                    **result_verification,
                    "history_state": "execution_result",
                    "claimed_at": str(claim_verification.get("claimed_at") or ""),
                    "expires_at": str(claim_verification.get("expires_at") or ""),
                    "claim_path": str(claim_path),
                    "result_path": str(result_path),
                    "result_sha256": result_digest,
                }
            rows.append(row)
        if not rows:
            return {
                **_history_blocked(
                    "scheduler_activation_execution_history_absent",
                    now=observed_now,
                ),
                "status": "verified",
                "history_state": "no_execution_history",
                "blocking_reason": "",
                "next_action": "continue fresh scheduler continuity verification",
                "execution_history_present": False,
                "progress": {
                    "current_evidence_verified": True,
                    "claim_count": 0,
                    "result_count": 0,
                },
            }
        rows.sort(
            key=lambda row: (
                _timestamp(row.get("claimed_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                str(row.get("decision_id") or ""),
            )
        )
        latest = rows[-1]
        attempted = latest.get("deployment_attempted") is True
        return {
            "schema": HISTORY_SCHEMA,
            "status": "verified",
            "history_state": str(latest.get("history_state") or ""),
            "updated_at": observed_now.isoformat(),
            "blocking_reason": str(latest.get("blocking_reason") or ""),
            "next_action": (
                "settle the latest execution against fresh scheduler continuity"
            ),
            "execution_history_present": True,
            "latest_execution": latest,
            "latest_claim_id": str(latest.get("claim_id") or ""),
            "latest_claim_sha256": str(latest.get("claim_sha256") or ""),
            "latest_result_sha256": str(latest.get("result_sha256") or ""),
            "action_required": False,
            "interrupt_operator": False,
            "authorization_recorded": True,
            "authorization_consumed": True,
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "deployment_attempted": attempted,
            "deployment_or_restart_performed": attempted,
            "protected_operation_executed": attempted,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": len(claims),
                "result_count": len(results),
                "historical_claims_verified": True,
                "historical_results_verified": True,
            },
        }
    except (OSError, TypeError, ValueError):
        return _history_blocked(
            "scheduler_activation_execution_history_not_admissible",
            now=observed_now,
        )


def _pending(
    decision_verification: Mapping[str, Any],
    *,
    request_path: Path,
    readiness_path: Path,
    decision_dir: Path,
    execution_dir: Path,
    deployment_receipt_path: Path,
    now: datetime,
) -> dict[str, Any]:
    argv = [
        "python3",
        "scripts/propertyquarry_ooda_scheduler_activation_execution.py",
        "--execute-approved-deployment",
        "--request-id",
        str(decision_verification.get("request_id") or ""),
        "--request-sha256",
        str(decision_verification.get("request_sha256") or ""),
        "--decision-id",
        str(decision_verification.get("decision_id") or ""),
        "--decision-sha256",
        str(decision_verification.get("decision_sha256") or ""),
        "--request",
        str(Path(request_path).absolute()),
        "--readiness",
        str(Path(readiness_path).absolute()),
        "--decision-dir",
        str(Path(decision_dir).absolute()),
        "--execution-dir",
        str(Path(execution_dir).absolute()),
        "--deployment-receipt",
        str(Path(deployment_receipt_path).absolute()),
    ]
    release = dict(decision_verification.get("release_evidence") or {})
    action = {
        "lane": "scheduler_activation",
        "reason": "approved_scheduler_activation_pending_manual_execution",
        "source_generated_at": str(
            decision_verification.get("updated_at") or now.isoformat()
        ),
        "safe_next_action": (
            "manually invoke the exact one-shot governed activation command; "
            "it will revalidate the approved release before deployment"
        ),
        "consent_required": True,
        "manual_execution_authorized": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "database_migration",
            "container_create_or_replace",
            "scheduler_activation",
        ],
        "provider_quota_consumption_allowed": False,
        "request_id": str(decision_verification.get("request_id") or ""),
        "request_sha256": str(
            decision_verification.get("request_sha256") or ""
        ),
        "decision_id": str(decision_verification.get("decision_id") or ""),
        "decision_sha256": str(
            decision_verification.get("decision_sha256") or ""
        ),
        "runtime_commit_sha": str(release.get("runtime_commit_sha") or ""),
        "envelope_commit_sha": str(release.get("envelope_commit_sha") or ""),
        "web_image_digest": str(release.get("web_image_digest") or ""),
        "render_image_digest": str(release.get("render_image_digest") or ""),
        "preflight_script_sha256": str(
            release.get("preflight_script_sha256") or ""
        ),
    }
    return {
        "schema": VERIFY_SCHEMA,
        "status": "action_required",
        "execution_state": "authorized_pending_manual_execution",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_manual_activation_invocation_required",
        "next_action": str(action["safe_next_action"]),
        "request_id": str(decision_verification.get("request_id") or ""),
        "request_sha256": str(
            decision_verification.get("request_sha256") or ""
        ),
        "decision_id": str(decision_verification.get("decision_id") or ""),
        "decision_sha256": str(
            decision_verification.get("decision_sha256") or ""
        ),
        "expires_at": str(decision_verification.get("expires_at") or ""),
        "scope": dict(decision_verification.get("scope") or {}),
        "release_evidence": release,
        "execution_command_argv": argv,
        "execution_command": shlex.join(argv),
        "source_cycle_receipt_sha256": str(
            decision_verification.get("readiness_receipt_sha256") or ""
        ),
        "action_required": True,
        "interrupt_operator": True,
        "authorization_required": True,
        "authorization_recorded": True,
        "authorization_consumed": False,
        "exact_scope_authorized": True,
        "manual_deployment_authorized": True,
        "automatic_execution_allowed": False,
        "execution_authorized": True,
        "deployment_or_restart_authorized": True,
        "deployment_attempted": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "actions": [action],
        "progress": {
            "current_evidence_verified": True,
            "authorization_decision_verified": True,
            "execution_claim_recorded": False,
        },
    }


def _recovery_action(
    *,
    decision_verification: Mapping[str, Any],
    execution_state: str,
    claim_id: str,
    result_sha256: str = "",
) -> dict[str, Any]:
    return {
        "lane": "scheduler_activation",
        "reason": "scheduler_activation_execution_recovery_required",
        "source_generated_at": str(
            decision_verification.get("updated_at") or ""
        ),
        "safe_next_action": (
            "inspect current runtime and the immutable activation execution "
            "receipts; do not replay the deployment"
        ),
        "consent_required": False,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "protected_operations": ["runtime_recovery_inspection"],
        "provider_quota_consumption_allowed": False,
        "decision_id": str(decision_verification.get("decision_id") or ""),
        "decision_sha256": str(
            decision_verification.get("decision_sha256") or ""
        ),
        "execution_state": execution_state,
        "claim_id": claim_id,
        "result_sha256": result_sha256,
    }


def inspect_current_scheduler_activation_execution(
    *,
    request_path: Path = authorization.DEFAULT_REQUEST_PATH,
    readiness_path: Path = decision.DEFAULT_READINESS_PATH,
    decision_dir: Path = decision.DEFAULT_DECISION_DIR,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    deployment_receipt_path: Path = DEFAULT_DEPLOYMENT_RECEIPT_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    current_decision = (
        decision.verify_current_scheduler_activation_authorization_decision(
            request_path=request_path,
            readiness_path=readiness_path,
            decision_dir=decision_dir,
            now=observed_now,
        )
    )
    if current_decision.get("status") == "pending":
        return _blocked(
            "scheduler_activation_execution_authorization_pending",
            now=observed_now,
        )
    if current_decision.get("status") != "verified":
        return _blocked(
            str(
                current_decision.get("blocking_reason")
                or "scheduler_activation_execution_authority_unavailable"
            ),
            now=observed_now,
        )
    if current_decision.get("decision") != "approve_exact_scope":
        return {
            **_blocked(
                "scheduler_activation_execution_not_authorized",
                now=observed_now,
            ),
            "status": "ready",
            "execution_state": "negative_decision_recorded",
            "authorization_recorded": True,
        }
    if not _decision_admissible(current_decision):
        return _blocked(
            "scheduler_activation_execution_authority_not_admissible",
            now=observed_now,
        )
    decision_id = str(current_decision.get("decision_id") or "")
    claim_path, result_path = _paths(execution_dir, decision_id)
    if not Path(execution_dir).absolute().exists():
        return _pending(
            current_decision,
            request_path=request_path,
            readiness_path=readiness_path,
            decision_dir=decision_dir,
            execution_dir=execution_dir,
            deployment_receipt_path=deployment_receipt_path,
            now=observed_now,
        )
    try:
        _private_directory(execution_dir, create=False)
        claim_exists = claim_path.exists()
        result_exists = result_path.exists()
        if result_exists and not claim_exists:
            raise ValueError("scheduler_activation_execution_result_without_claim")
        if not claim_exists:
            return _pending(
                current_decision,
                request_path=request_path,
                readiness_path=readiness_path,
                decision_dir=decision_dir,
                execution_dir=execution_dir,
                deployment_receipt_path=deployment_receipt_path,
                now=observed_now,
            )
        claim, claim_digest = _private_object(
            claim_path,
            field="scheduler_activation_execution_claim",
        )
        claim_verification = verify_scheduler_activation_execution_claim(
            claim,
            decision_verification=current_decision,
            now=observed_now,
        )
        if claim_verification.get("status") != "verified":
            raise ValueError("scheduler_activation_execution_claim_not_admissible")
        if not result_exists:
            recovery_action = _recovery_action(
                decision_verification=current_decision,
                execution_state="claimed_without_result",
                claim_id=str(claim.get("claim_id") or ""),
            )
            return {
                **_blocked(
                    "scheduler_activation_execution_claimed_without_result",
                    now=observed_now,
                ),
                "status": "action_required",
                "execution_state": "claimed_without_result",
                "claim_id": str(claim.get("claim_id") or ""),
                "claim_sha256": claim_digest,
                "claim_path": str(claim_path),
                "authorization_recorded": True,
                "authorization_consumed": True,
                "action_required": True,
                "interrupt_operator": True,
                "next_action": (
                    "inspect current runtime and the immutable execution claim; "
                    "do not replay the deployment"
                ),
                "source_cycle_receipt_sha256": str(
                    current_decision.get("readiness_receipt_sha256") or ""
                ),
                "actions": [recovery_action],
            }
        result, result_digest = _private_object(
            result_path,
            field="scheduler_activation_execution_result",
        )
        result_verification = verify_scheduler_activation_execution_result(
            result,
            claim=claim,
            now=observed_now,
        )
        if result_verification.get("status") != "verified":
            raise ValueError("scheduler_activation_execution_result_not_admissible")
        result_verification.update(
            {
                "claim_path": str(claim_path),
                "result_path": str(result_path),
                "result_sha256": result_digest,
                "authorization_recorded": True,
            }
        )
        if result_verification.get("execution_state") != "succeeded":
            recovery_action = _recovery_action(
                decision_verification=current_decision,
                execution_state=str(
                    result_verification.get("execution_state") or "failed"
                ),
                claim_id=str(result_verification.get("claim_id") or ""),
                result_sha256=result_digest,
            )
            result_verification.update(
                {
                    "source_cycle_receipt_sha256": str(
                        current_decision.get("readiness_receipt_sha256") or ""
                    ),
                    "actions": [recovery_action],
                }
            )
        return result_verification
    except (OSError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_execution_evidence_not_admissible",
            now=observed_now,
        )


def execute_current_scheduler_activation(
    *,
    expected_request_id: str,
    expected_request_sha256: str,
    expected_decision_id: str,
    expected_decision_sha256: str,
    request_path: Path = authorization.DEFAULT_REQUEST_PATH,
    readiness_path: Path = decision.DEFAULT_READINESS_PATH,
    decision_dir: Path = decision.DEFAULT_DECISION_DIR,
    execution_dir: Path = DEFAULT_EXECUTION_DIR,
    deployment_receipt_path: Path = DEFAULT_DEPLOYMENT_RECEIPT_PATH,
    project: str = "property",
    preflight_timeout_seconds: int = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    deployment_timeout_seconds: int = DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS,
    now: datetime | None = None,
    preflight_runner: Callable[..., Mapping[str, Any]] = (
        readiness.observe_scheduler_activation_preflight
    ),
    deployment_runner: Callable[..., Mapping[str, Any]] = (
        run_scheduler_activation_deployment
    ),
) -> dict[str, Any]:
    claimed_now = _now(now)
    current = decision.verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=claimed_now,
    )
    if not _decision_admissible(current):
        return _blocked(
            "scheduler_activation_execution_approval_not_current",
            now=claimed_now,
        )
    if not (
        current.get("request_id") == expected_request_id
        and current.get("request_sha256") == expected_request_sha256
        and current.get("decision_id") == expected_decision_id
        and current.get("decision_sha256") == expected_decision_sha256
    ):
        return _blocked(
            "scheduler_activation_execution_expected_authority_mismatch",
            now=claimed_now,
        )
    if current.get("scope", {}).get("compose_project") != project:
        return _blocked(
            "scheduler_activation_execution_project_mismatch",
            now=claimed_now,
        )
    try:
        claim = build_scheduler_activation_execution_claim(
            decision_verification=current,
            now=claimed_now,
        )
        target_dir = _private_directory(execution_dir, create=True)
        claim_path, result_path = _paths(
            target_dir,
            str(current.get("decision_id") or ""),
        )
        atomic_write_bytes(
            claim_path,
            _canonical(claim),
            overwrite=False,
        )
    except OutputExistsError:
        return _blocked(
            "scheduler_activation_execution_already_claimed",
            now=claimed_now,
        )
    except (OSError, SecureFileIOError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_execution_claim_failed",
            now=claimed_now,
        )

    release_evidence = dict(current.get("release_evidence") or {})
    preflight: dict[str, Any]
    deployment_observation: dict[str, Any] | None = None
    runtime_receipt: dict[str, Any] | None = None
    result_status = "blocked_before_execution"
    blocking_reason = "activation_preflight_not_ready"
    try:
        raw_preflight = dict(
            preflight_runner(
                project=project,
                now=claimed_now,
                timeout_seconds=preflight_timeout_seconds,
            )
        )
        preflight = _project_preflight(
            raw_preflight,
            release_evidence=release_evidence,
            project=project,
            now=claimed_now,
        )
    except (OSError, TypeError, ValueError) as exc:
        preflight = dict(locals().get("raw_preflight") or {})
        blocking_reason = str(exc) or "activation_preflight_not_ready"
    else:
        launch_now = _now(None if now is None else now)
        refreshed = (
            decision.verify_current_scheduler_activation_authorization_decision(
                request_path=request_path,
                readiness_path=readiness_path,
                decision_dir=decision_dir,
                now=launch_now,
            )
        )
        if not (
            _decision_admissible(refreshed)
            and refreshed.get("decision_id") == current.get("decision_id")
            and refreshed.get("decision_sha256")
            == current.get("decision_sha256")
        ):
            blocking_reason = "scheduler_activation_execution_approval_expired_before_launch"
        else:
            try:
                deployment_observation = _project_deployment(
                    dict(
                        deployment_runner(
                            release_evidence=release_evidence,
                            project=project,
                            deployment_receipt_path=deployment_receipt_path,
                            timeout_seconds=deployment_timeout_seconds,
                        )
                    ),
                    release_evidence=release_evidence,
                    project=project,
                    deployment_receipt_path=deployment_receipt_path,
                )
                if deployment_observation.get("status") != "succeeded":
                    result_status = "deployment_failed"
                    blocking_reason = str(
                        deployment_observation.get("blocking_reason")
                        or "activation_deployment_failed"
                    )
                else:
                    runtime_receipt = _verify_runtime_receipt(
                        deployment_receipt_path,
                        release_evidence=release_evidence,
                        project=project,
                        now=_now(None if now is None else now),
                    )
                    result_status = "succeeded"
                    blocking_reason = ""
            except (OSError, TypeError, ValueError) as exc:
                result_status = "deployment_failed"
                blocking_reason = str(exc) or "activation_deployment_verification_failed"
                failure_now = _now(None if now is None else now)
                deployment_observation = _deployment_observation(
                    status="failed",
                    started_at=launch_now,
                    completed_at=failure_now,
                    duration_ms=max(
                        0,
                        int((failure_now - launch_now).total_seconds() * 1000),
                    ),
                    exit_code=126,
                    blocking_reason=blocking_reason,
                    stdout=b"",
                    stderr=b"",
                    release_evidence=release_evidence,
                    project=project,
                    deployment_receipt_path=deployment_receipt_path,
                )
    completed_now = _now(None if now is None else now)
    result = _build_result(
        claim=claim,
        status=result_status,
        blocking_reason=blocking_reason,
        preflight_observation=preflight,
        deployment_observation=deployment_observation,
        runtime_receipt=runtime_receipt,
        now=completed_now,
    )
    try:
        atomic_write_bytes(
            result_path,
            _canonical(result),
            overwrite=False,
        )
        verification = verify_scheduler_activation_execution_result(
            result,
            claim=claim,
            now=completed_now,
        )
        if verification.get("status") != "verified":
            raise ValueError("scheduler_activation_execution_result_verification_failed")
        verification.update(
            {
                "claim_path": str(claim_path),
                "result_path": str(result_path),
                "result_sha256": _sha256(_canonical(result)),
                "authorization_recorded": True,
            }
        )
        return verification
    except (OSError, SecureFileIOError, TypeError, ValueError):
        return {
            **_blocked(
                "scheduler_activation_execution_result_persistence_failed",
                now=completed_now,
            ),
            "execution_state": "result_persistence_failed",
            "authorization_recorded": True,
            "authorization_consumed": True,
            "deployment_attempted": deployment_observation is not None,
            "deployment_or_restart_performed": deployment_observation is not None,
            "protected_operation_executed": deployment_observation is not None,
            "claim_path": str(claim_path),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute one current exact PropertyQuarry scheduler activation approval."
        )
    )
    parser.add_argument(
        "--execute-approved-deployment",
        action="store_true",
        required=True,
        help="confirm deliberate invocation of the one-shot governed executor",
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--decision-sha256", required=True)
    parser.add_argument("--request", type=Path, default=authorization.DEFAULT_REQUEST_PATH)
    parser.add_argument("--readiness", type=Path, default=decision.DEFAULT_READINESS_PATH)
    parser.add_argument("--decision-dir", type=Path, default=decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--execution-dir", type=Path, default=DEFAULT_EXECUTION_DIR)
    parser.add_argument(
        "--deployment-receipt",
        type=Path,
        default=DEFAULT_DEPLOYMENT_RECEIPT_PATH,
    )
    parser.add_argument("--project", default="property")
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = execute_current_scheduler_activation(
        expected_request_id=args.request_id,
        expected_request_sha256=args.request_sha256,
        expected_decision_id=args.decision_id,
        expected_decision_sha256=args.decision_sha256,
        request_path=args.request,
        readiness_path=args.readiness,
        decision_dir=args.decision_dir,
        execution_dir=args.execution_dir,
        deployment_receipt_path=args.deployment_receipt,
        project=args.project,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("execution_state") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
