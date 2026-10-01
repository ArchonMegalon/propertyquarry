#!/usr/bin/env python3
"""Materialize and verify a non-executing PropertyQuarry runtime review packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_local_deployment_receipt as local_deployment
from scripts import propertyquarry_runtime_deploy_v2 as runtime_deploy
from scripts import propertyquarry_launch_room as launch_room
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_runtime_review_packet.v6"
VERIFY_SCHEMA = "propertyquarry.ooda_runtime_review_verification.v1"
RUNTIME_OBSERVATION_SCHEMA = "propertyquarry.ooda_runtime_observation.v2"
RUNTIME_OBSERVATION_VERIFY_SCHEMA = (
    "propertyquarry.ooda_runtime_observation_verification.v2"
)
DEFAULT_PACKET_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-review-packet.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-review-verification.json"
)
DEFAULT_RUNTIME_OBSERVATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-observation.json"
)
DEFAULT_RUNTIME_OBSERVATION_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-observation-verification.json"
)
DEFAULT_CYCLE_RECEIPT = operator_status.DEFAULT_CYCLE_RECEIPT
DEFAULT_SIGNAL_DIR = operator_status.DEFAULT_SIGNAL_DIR
DEFAULT_LIVE_MOBILE_RECEIPT = Path(
    "_completion/smoke/property-public-origin-observation-latest.json"
)
DEFAULT_DEPLOYMENT_ENV_PATH = Path(".env")
DEFAULT_DEPLOYMENT_ENV_OPERATOR_IMPORT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "deployment-environment-operator-import.env"
)
DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "deployment-environment-local-runtime-bindings.env"
)
DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS = tuple(
    path.relative_to(runtime_deploy.PROPERTY_ROOT)
    for path in runtime_deploy.ENV_FILES
)
DEFAULT_RELEASE_MANIFEST = ROOT / launch_room.RELEASE_MANIFEST_PATH
DEFAULT_MAX_AGE_SECONDS = 1800.0
MAX_PACKET_BYTES = 512 * 1024
MAX_LIVE_MOBILE_RECEIPT_BYTES = 1024 * 1024
MAX_RELEASE_MANIFEST_BYTES = 1024 * 1024
MAX_DEPLOYMENT_ENV_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SERVICE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_HEALTH = frozenset({"healthy", "starting", "unhealthy", "none"})
PROPERTYQUARRY_SCHEDULER_SERVICE = "propertyquarry-scheduler"
_PUBLIC_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)
_PROTECTED_OPERATIONS = [
    "runtime_configuration_change",
    "deployment_or_restart",
]
_RUNTIME_CONSENT_DECISIONS = [
    "authorize_exact_preview",
    "reject",
    "defer",
]
_RUNTIME_CONSENT_REQUEST_ID = re.compile(
    r"pqruntimeconsent_[0-9a-f]{24}\Z"
)
_RUNTIME_CONSENT_FRESHNESS_WINDOW_SECONDS = int(DEFAULT_MAX_AGE_SECONDS)
_TUNNEL_ACTION_REASON = "live_runtime_tunnel_unavailable"
_HOST_ADMISSION_ACTION_REASON = "live_runtime_host_admission_rejected"
_REVIEW_ACTION_REASONS = frozenset(
    {_TUNNEL_ACTION_REASON, _HOST_ADMISSION_ACTION_REASON}
)
_CLOUDFLARE_TUNNEL_FAILURE = {
    "provider": "cloudflare",
    "code": "1033",
    "reason": "cloudflare_tunnel_unavailable",
    "http_status": 530,
}
_PUBLIC_ORIGIN_OBSERVATION_SCHEMA = "propertyquarry.ooda_public_origin_observation.v1"
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_REQUIRED_ENV_EXPRESSION = re.compile(
    r"\$\{([A-Z][A-Z0-9_]{0,127}):\?[^}]*\}"
)
_DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES = (
    {
        "classification": "external_account_material",
        "resolution_lane": "governed_operator_import",
        "operator_value_required": True,
        "keys": frozenset(
            {
                "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
                "PROPERTYQUARRY_CF_TUNNEL_TOKEN",
                "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
                "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
            }
        ),
    },
    {
        "classification": "local_secret_generation",
        "resolution_lane": "governed_local_secret_generation",
        "operator_value_required": False,
        "keys": frozenset(
            {
                "EA_SIGNING_SECRET",
                "POSTGRES_PASSWORD",
                "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
                "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
                "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN",
            }
        ),
    },
    {
        "classification": "database_role_binding",
        "resolution_lane": "governed_database_role_bootstrap",
        "operator_value_required": False,
        "keys": frozenset(
            {
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
                "PROPERTYQUARRY_API_DATABASE_URL",
                "PROPERTYQUARRY_API_INGRESS_DATABASE_URL",
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
                "PROPERTYQUARRY_RENDER_DATABASE_URL",
                "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
                "PROPERTYQUARRY_WORKER_DATABASE_URL",
            }
        ),
    },
    {
        "classification": "runtime_binding_derivation",
        "resolution_lane": "repository_runtime_binding_derivation",
        "operator_value_required": False,
        "keys": frozenset(
            {
                "PROPERTYQUARRY_OODA_GOLD_SOURCE_DIR",
                "PROPERTYQUARRY_OODA_PUBLIC_ORIGIN_SOURCE_DIR",
                "PROPERTYQUARRY_OODA_SCENE_SOURCE_DIR",
                "PROPERTYQUARRY_OODA_SIGNAL_DIR",
                "PROPERTYQUARRY_OODA_STAGE_GID",
                "PROPERTYQUARRY_OODA_STAGE_RECEIPT_DIR",
                "PROPERTYQUARRY_OODA_STAGE_UID",
            }
        ),
    },
)
_LOCAL_RUNTIME_BINDING_PATHS = {
    "PROPERTYQUARRY_OODA_GOLD_SOURCE_DIR": Path(
        "_completion/property_gold_status"
    ),
    "PROPERTYQUARRY_OODA_PUBLIC_ORIGIN_SOURCE_DIR": Path("_completion/smoke"),
    "PROPERTYQUARRY_OODA_SCENE_SOURCE_DIR": Path(
        "_completion/scene_video_readiness"
    ),
    "PROPERTYQUARRY_OODA_SIGNAL_DIR": Path(
        "_completion/propertyquarry_ooda_signal_ingress"
    ),
    "PROPERTYQUARRY_OODA_STAGE_RECEIPT_DIR": Path(
        "_completion/propertyquarry_ooda_notification_cycle"
    ),
}
_LOCAL_RUNTIME_BINDING_IDENTITY_KEYS = {
    "PROPERTYQUARRY_OODA_STAGE_UID",
    "PROPERTYQUARRY_OODA_STAGE_GID",
}
_LOCAL_GENERATED_SECRET_KEYS = frozenset(
    {"PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"}
)
_LOCAL_CANDIDATE_KEYS = frozenset(
    set(_LOCAL_RUNTIME_BINDING_PATHS)
    | _LOCAL_RUNTIME_BINDING_IDENTITY_KEYS
    | _LOCAL_GENERATED_SECRET_KEYS
)
_LOCAL_GENERATED_SECRET = re.compile(r"[A-Za-z0-9_-]{64}\Z")


def _compose_command(operation: str) -> list[str]:
    command = [
        "/usr/bin/docker",
        "compose",
        "--project-name",
        local_deployment.DEFAULT_COMPOSE_PROJECT,
    ]
    for path in DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS:
        command.extend(["--env-file", path.as_posix()])
    for path in local_deployment.COMPOSE_FILES:
        command.extend(["--file", path.as_posix()])
    if operation == "start":
        return [*command, "up", "--detach", "--build"]
    if operation == "rollback_to_absent":
        return [*command, "down", "--remove-orphans"]
    raise ValueError("recovery_operation_not_admissible")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate the review packet from a fresh approved evaluate-only cycle",
        "progress": {"current_evidence_verified": False},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _run(command: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "DOCKER_HOST": os.environ.get(
                    "DOCKER_HOST", "unix:///var/run/docker.sock"
                ),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout="",
            stderr="",
        )


def _run_bytes(
    command: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=b"",
            stderr=b"",
        )


def _container_health_from_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if "(healthy)" in normalized:
        return "healthy"
    if "(unhealthy)" in normalized:
        return "unhealthy"
    if "(health: starting)" in normalized:
        return "starting"
    return "none"


def _observe_runtime(
    *,
    project: str = "property",
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _now(now).isoformat()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", project):
        raise ValueError("compose_project_not_admissible")
    result = _run(
        [
            "/usr/bin/docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}',
        ]
    )
    if result.returncode != 0 or len(result.stdout.encode()) > 65536:
        raise ValueError("runtime_observation_failed")
    rows: list[dict[str, str]] = []
    for raw in result.stdout.splitlines():
        values = raw.split("\t")
        if len(values) != 3:
            raise ValueError("runtime_observation_shape_invalid")
        service, state, status = (value.strip() for value in values)
        if (
            not _SERVICE.fullmatch(service)
            or state
            not in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }
        ):
            raise ValueError("runtime_observation_shape_invalid")
        rows.append(
            {
                "service": service,
                "state": state,
                "health": _container_health_from_status(status),
            }
        )
    rows.sort(key=lambda row: (row["service"], row["state"], row["health"]))
    fingerprint_payload = {"project": project, "services": rows}
    return {
        "source": "local_docker",
        "observed_at": observed_at,
        "query_status": "pass",
        "project": project,
        "container_count": len(rows),
        "services": rows,
        "fingerprint_sha256": _sha256(_canonical(fingerprint_payload)),
    }


def _runtime_observation_blocked(
    reason: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema": RUNTIME_OBSERVATION_VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "repeat the read-only runtime observation without deploying or restarting anything",
        "action_required": False,
        "interrupt_operator": False,
        "progress": {"current_evidence_verified": False},
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _runtime_observation_projection(
    runtime_posture: Mapping[str, Any],
) -> dict[str, Any]:
    rows = list(runtime_posture.get("services") or [])
    running_count = sum(
        1
        for row in rows
        if isinstance(row, Mapping) and row.get("state") == "running"
    )
    scheduler_rows = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("service") == PROPERTYQUARRY_SCHEDULER_SERVICE
    ]
    running_scheduler_count = sum(
        1 for row in scheduler_rows if row.get("state") == "running"
    )
    healthy_scheduler_count = sum(
        1
        for row in scheduler_rows
        if row.get("state") == "running" and row.get("health") == "healthy"
    )
    progress = {
        "container_count": len(rows),
        "running_container_count": running_count,
        "non_running_container_count": len(rows) - running_count,
        "scheduler_container_count": len(scheduler_rows),
        "running_scheduler_container_count": running_scheduler_count,
        "healthy_scheduler_container_count": healthy_scheduler_count,
    }
    runtime_condition = "present" if rows else "absent"
    if not scheduler_rows:
        scheduler_condition = "absent"
        scheduler_state = ""
        scheduler_health = "none"
        next_action = (
            "the persistent PropertyQuarry OODA scheduler is absent; safe host "
            "ticks remain evaluate-only, and any deployment or start requires "
            "separate explicit consent"
        )
    elif len(scheduler_rows) > 1:
        scheduler_condition = "ambiguous"
        scheduler_state = "multiple"
        scheduler_health = "multiple"
        next_action = (
            "multiple PropertyQuarry OODA scheduler containers were observed; do "
            "not trust persistent reevaluation until their cardinality is resolved "
            "under separate deployment or restart consent"
        )
    else:
        scheduler_state = str(scheduler_rows[0].get("state") or "")
        scheduler_health = str(scheduler_rows[0].get("health") or "none")
        if scheduler_state == "running" and scheduler_health == "healthy":
            scheduler_condition = "running"
            next_action = (
                "verify a fresh cycle-bound PropertyQuarry OODA scheduler iteration "
                "witness before treating persistent reevaluation as active; any "
                "deployment or restart remains separately consent-gated"
            )
        elif scheduler_state != "running":
            scheduler_condition = "not_running"
            next_action = (
                "the PropertyQuarry OODA scheduler exists but is not running; "
                "inspect its current state without treating reevaluation as active, "
                "and require separate explicit consent for any restart"
            )
        else:
            scheduler_condition = (
                scheduler_health
                if scheduler_health in {"starting", "unhealthy"}
                else "health_unverified"
            )
            next_action = (
                "the PropertyQuarry OODA scheduler container is running without "
                "verified healthy scheduler posture; do not treat persistent "
                "reevaluation as active, and require separate explicit consent for "
                "any restart"
            )
    return {
        "runtime_condition": runtime_condition,
        "scheduler_service": PROPERTYQUARRY_SCHEDULER_SERVICE,
        "scheduler_condition": scheduler_condition,
        "scheduler_state": scheduler_state,
        "scheduler_health": scheduler_health,
        "scheduler_container_healthy": scheduler_condition == "running",
        "persistent_reevaluation_running": False,
        "progress": progress,
        "next_action": next_action,
    }


def build_runtime_observation_receipt(
    runtime_posture: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a durable, non-executing receipt from one local Docker observation."""

    observed_at = _now(now).isoformat()
    rows = runtime_posture.get("services")
    normalized_rows = list(rows) if isinstance(rows, list) else []
    sorted_rows = sorted(
        normalized_rows,
        key=lambda row: (
            str(row.get("service") or "") if isinstance(row, Mapping) else "",
            str(row.get("state") or "") if isinstance(row, Mapping) else "",
            str(row.get("health") or "") if isinstance(row, Mapping) else "",
        ),
    )
    expected_fingerprint = _sha256(
        _canonical(
            {
                "project": runtime_posture.get("project"),
                "services": normalized_rows,
            }
        )
    )
    if not (
        set(runtime_posture)
        == {
            "source",
            "observed_at",
            "query_status",
            "project",
            "container_count",
            "services",
            "fingerprint_sha256",
        }
        and runtime_posture.get("source") == "local_docker"
        and runtime_posture.get("query_status") == "pass"
        and re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,62}",
            str(runtime_posture.get("project") or ""),
        )
        and runtime_posture.get("observed_at") == observed_at
        and isinstance(runtime_posture.get("container_count"), int)
        and not isinstance(runtime_posture.get("container_count"), bool)
        and runtime_posture.get("container_count") == len(normalized_rows)
        and normalized_rows == sorted_rows
        and all(
            isinstance(row, dict)
            and set(row) == {"service", "state", "health"}
            and _SERVICE.fullmatch(str(row.get("service") or ""))
            and row.get("state")
            in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }
            and row.get("health") in _HEALTH
            for row in normalized_rows
        )
        and runtime_posture.get("fingerprint_sha256") == expected_fingerprint
    ):
        raise ValueError("runtime_observation_not_admissible")
    projection = _runtime_observation_projection(runtime_posture)
    receipt: dict[str, Any] = {
        "schema": RUNTIME_OBSERVATION_SCHEMA,
        "generated_at": observed_at,
        "updated_at": observed_at,
        "status": "observed",
        "runtime_condition": projection["runtime_condition"],
        "scheduler_service": projection["scheduler_service"],
        "scheduler_condition": projection["scheduler_condition"],
        "scheduler_state": projection["scheduler_state"],
        "scheduler_health": projection["scheduler_health"],
        "scheduler_container_healthy": projection[
            "scheduler_container_healthy"
        ],
        "persistent_reevaluation_running": projection[
            "persistent_reevaluation_running"
        ],
        "blocking_reason": "",
        "next_action": projection["next_action"],
        "action_required": False,
        "interrupt_operator": False,
        "progress": projection["progress"],
        "runtime_posture": dict(runtime_posture),
        "consent_gate": {
            "required_for": ["deployment_or_restart"],
            "authorization_recorded": False,
            "execution_authorized": False,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def verify_runtime_observation_receipt(
    receipt: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify freshness, integrity, shape, and the no-authority boundary."""

    observed_now = _now(now)
    age_limit = float(max_age_seconds)
    generated_at = operator_status._parse_fresh_timestamp(
        receipt.get("generated_at"),
        now=observed_now,
        max_age_seconds=age_limit,
    )
    if generated_at is None:
        return _runtime_observation_blocked(
            "runtime_observation_not_fresh",
            now=observed_now,
        )
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _runtime_observation_blocked(
            "runtime_observation_integrity_invalid",
            now=observed_now,
        )
    runtime = receipt.get("runtime_posture")
    if not isinstance(runtime, Mapping):
        return _runtime_observation_blocked(
            "runtime_observation_contract_not_admissible",
            now=observed_now,
        )
    try:
        rebuilt = build_runtime_observation_receipt(runtime, now=datetime.fromisoformat(generated_at))
    except (TypeError, ValueError):
        return _runtime_observation_blocked(
            "runtime_observation_contract_not_admissible",
            now=observed_now,
        )
    if dict(receipt) != rebuilt:
        return _runtime_observation_blocked(
            "runtime_observation_contract_not_admissible",
            now=observed_now,
        )
    return {
        "schema": RUNTIME_OBSERVATION_VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "runtime_condition": str(receipt.get("runtime_condition") or ""),
        "scheduler_service": str(receipt.get("scheduler_service") or ""),
        "scheduler_condition": str(receipt.get("scheduler_condition") or ""),
        "scheduler_state": str(receipt.get("scheduler_state") or ""),
        "scheduler_health": str(receipt.get("scheduler_health") or ""),
        "scheduler_container_healthy": (
            receipt.get("scheduler_container_healthy") is True
        ),
        "persistent_reevaluation_running": (
            receipt.get("persistent_reevaluation_running") is True
        ),
        "runtime_observed_at": generated_at,
        "runtime_observation_sha256": _sha256(_canonical(receipt)),
        "action_required": False,
        "interrupt_operator": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "current_evidence_verified": False,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def materialize_current_runtime_observation(
    *,
    write_path: Path = DEFAULT_RUNTIME_OBSERVATION_PATH,
    project: str = local_deployment.DEFAULT_COMPOSE_PROJECT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    receipt = build_runtime_observation_receipt(
        _observe_runtime(project=project, now=observed_now),
        now=observed_now,
    )
    atomic_write_bytes(
        Path(write_path).absolute(),
        _canonical(receipt),
        overwrite=True,
    )
    return receipt


def verify_current_runtime_observation(
    *,
    observation_path: Path = DEFAULT_RUNTIME_OBSERVATION_PATH,
    project: str = local_deployment.DEFAULT_COMPOSE_PROJECT,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        metadata = Path(observation_path).lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("runtime_observation_file_not_admissible")
        receipt, _raw, _digest = load_strict_json_object_snapshot(
            Path(observation_path),
            field="runtime_observation",
            maximum_bytes=MAX_PACKET_BYTES,
        )
    except Exception:
        return _runtime_observation_blocked(
            "runtime_observation_file_not_admissible",
            now=observed_now,
        )
    verification = verify_runtime_observation_receipt(
        receipt,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if verification.get("status") != "verified":
        return verification
    try:
        current_runtime = _observe_runtime(project=project, now=observed_now)
    except Exception:
        return _runtime_observation_blocked(
            "current_runtime_observation_unavailable",
            now=observed_now,
        )
    receipt_runtime = dict(receipt.get("runtime_posture") or {})
    if not (
        receipt_runtime.get("project") == current_runtime.get("project")
        and receipt_runtime.get("fingerprint_sha256")
        == current_runtime.get("fingerprint_sha256")
    ):
        return _runtime_observation_blocked(
            "runtime_observation_current_binding_mismatch",
            now=observed_now,
        )
    verification["progress"]["current_evidence_verified"] = True
    verification["current_observed_at"] = str(
        current_runtime.get("observed_at") or ""
    )
    return verification


def materialize_current_runtime_observation_bundle(
    *,
    observation_path: Path = DEFAULT_RUNTIME_OBSERVATION_PATH,
    verification_path: Path = DEFAULT_RUNTIME_OBSERVATION_VERIFICATION_PATH,
    project: str = local_deployment.DEFAULT_COMPOSE_PROJECT,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    materialize_current_runtime_observation(
        write_path=observation_path,
        project=project,
        now=observed_now,
    )
    verification = verify_current_runtime_observation(
        observation_path=observation_path,
        project=project,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    verification["observation_path"] = str(observation_path)
    verification["verification_path"] = str(verification_path)
    verification["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(verification_path).absolute(),
            _canonical(verification),
            overwrite=True,
        )
    except Exception:
        verification["verification_receipt_persisted"] = False
    return verification


def _release_posture(*, now: datetime | None = None) -> dict[str, Any]:
    head = _run(
        ["/usr/bin/git", "rev-parse", "--verify", "HEAD^{commit}"],
        timeout=30.0,
    )
    worktree = _run(
        [
            "/usr/bin/git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        timeout=30.0,
    )
    tracked_diff = _run_bytes(
        [
            "/usr/bin/git",
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
        ]
    )
    untracked = _run_bytes(
        [
            "/usr/bin/git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ]
    )
    if (
        head.returncode != 0
        or worktree.returncode != 0
        or tracked_diff.returncode != 0
        or untracked.returncode != 0
        or not re.fullmatch(r"[0-9a-f]{40}\n?", head.stdout)
        or len(worktree.stdout.encode()) > 1024 * 1024
        or len(tracked_diff.stdout) > 32 * 1024 * 1024
        or len(untracked.stdout) > 1024 * 1024
    ):
        raise ValueError("release_posture_failed")
    rows = [row for row in worktree.stdout.splitlines() if row]
    worktree_digest = hashlib.sha256()
    worktree_digest.update(b"tracked-diff\0")
    worktree_digest.update(tracked_diff.stdout)
    worktree_digest.update(b"\0untracked-files\0")
    total_untracked_bytes = 0
    for raw_path in sorted(value for value in untracked.stdout.split(b"\0") if value):
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("release_posture_failed")
        path = ROOT / relative
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 16 * 1024 * 1024
            ):
                raise ValueError("release_posture_failed")
            total_untracked_bytes += metadata.st_size
            if total_untracked_bytes > 64 * 1024 * 1024:
                raise ValueError("release_posture_failed")
            worktree_digest.update(raw_path)
            worktree_digest.update(b"\0")
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    raise ValueError("release_posture_failed")
                worktree_digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
                or after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns
                or after.st_ctime_ns != metadata.st_ctime_ns
                or after.st_mode != metadata.st_mode
                or after.st_uid != metadata.st_uid
                or after.st_nlink != metadata.st_nlink
            ):
                raise ValueError("release_posture_failed")
            worktree_digest.update(b"\0")
        finally:
            os.close(descriptor)
    return {
        "source": "local_git",
        "observed_at": _now(now).isoformat(),
        "head_sha": head.stdout.strip(),
        "worktree_clean": not rows,
        "changed_path_count": len(rows),
        "worktree_fingerprint_sha256": worktree_digest.hexdigest(),
        "changed_paths_recorded": False,
    }


def _snapshot_evidence(
    *,
    cycle_receipt_path: Path,
    signal_dir: Path,
) -> dict[str, Any]:
    paths = {
        "cycle_receipt": Path(cycle_receipt_path),
        "approval_manifest": Path(signal_dir) / "manifest.json",
    }
    result: dict[str, Any] = {}
    for name, path in paths.items():
        metadata = path.lstat()
        expected_private = name == "cycle_receipt"
        rejected_mode = 0o077 if expected_private else 0o022
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & rejected_mode
        ):
            raise ValueError(f"{name}_not_admissible")
        payload, raw, digest = load_strict_json_object_snapshot(
            path,
            field=name,
            maximum_bytes=operator_status.MAX_RECEIPT_BYTES,
        )
        result[name] = {
            "sha256": digest,
            "bytes": len(raw),
            "generated_at": str(payload.get("generated_at") or "").strip(),
        }
    return result


def _rooted(path: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def _admissible_snapshot(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
    private: bool,
) -> tuple[bytes, str]:
    target = _rooted(path)
    metadata = target.lstat()
    rejected_mode = 0o077 if private else 0o022
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > maximum_bytes
        or stat.S_IMODE(metadata.st_mode) & rejected_mode
    ):
        raise ValueError(f"{field}_not_admissible")
    descriptor = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or opened.st_mode != metadata.st_mode
            or opened.st_uid != metadata.st_uid
            or opened.st_nlink != metadata.st_nlink
        ):
            raise ValueError(f"{field}_not_admissible")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise ValueError(f"{field}_not_admissible")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or after.st_mode != opened.st_mode
            or after.st_uid != opened.st_uid
            or after.st_nlink != opened.st_nlink
        ):
            raise ValueError(f"{field}_not_admissible")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    return raw, _sha256(raw)


def _read_private_text_without_digest(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> tuple[str, int]:
    """Read one owner-only regular file without computing a content digest."""

    target = Path(path)
    metadata = target.lstat()
    file_mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or metadata.st_size > maximum_bytes
        or file_mode != 0o600
    ):
        raise ValueError(f"{field}_not_admissible")
    descriptor = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or opened.st_mode != metadata.st_mode
            or opened.st_uid != metadata.st_uid
            or opened.st_nlink != metadata.st_nlink
        ):
            raise ValueError(f"{field}_not_admissible")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise ValueError(f"{field}_not_admissible")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or after.st_mode != opened.st_mode
            or after.st_uid != opened.st_uid
            or after.st_nlink != opened.st_nlink
        ):
            raise ValueError(f"{field}_not_admissible")
    finally:
        os.close(descriptor)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field}_not_admissible") from exc
    if "\x00" in text:
        raise ValueError(f"{field}_not_admissible")
    return text, file_mode


def _dotenv_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError("deployment_environment_syntax_invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        if _ENV_KEY.fullmatch(key) is None or key in values:
            raise ValueError("deployment_environment_syntax_invalid")
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {"'", '"'}
        ):
            normalized_value = normalized_value[1:-1].strip()
        values[key] = normalized_value
    return values


def _dotenv_key_presence(text: str) -> dict[str, bool]:
    return {key: bool(value) for key, value in _dotenv_values(text).items()}


def inspect_deployment_environment_operator_import(
    *,
    required_keys: list[str],
    root: Path = ROOT,
    import_path: Path = DEFAULT_DEPLOYMENT_ENV_OPERATOR_IMPORT_PATH,
) -> dict[str, Any]:
    """Inspect an exact private drop fragment without recording or hashing values."""

    expected = sorted(set(required_keys))
    if expected != required_keys or any(
        _ENV_KEY.fullmatch(key) is None for key in expected
    ):
        raise ValueError("deployment_environment_operator_import_not_admissible")
    root_path = Path(root).resolve(strict=True)
    requested_path = Path(import_path)
    target = requested_path if requested_path.is_absolute() else root_path / requested_path
    display_path = (
        requested_path
        if not requested_path.is_absolute()
        else Path(os.path.relpath(target, root_path))
    )
    if display_path.is_absolute() or ".." in display_path.parts:
        raise ValueError("deployment_environment_operator_import_path_not_admissible")

    def posture(
        *,
        status: str,
        blocking_reason: str,
        file_status: str,
        file_mode: int,
        inspection_complete: bool,
        present_count: int,
        missing: list[str],
        unresolved: list[str],
        unexpected: list[str],
        ready: bool,
    ) -> dict[str, Any]:
        return {
            "schema": "propertyquarry.deployment_environment_operator_import.v1",
            "status": status,
            "blocking_reason": blocking_reason,
            "path": display_path.as_posix(),
            "file_status": file_status,
            "file_mode": file_mode,
            "inspection_complete": inspection_complete,
            "required_keys": expected,
            "required_key_count": len(expected),
            "present_nonempty_required_key_count": present_count,
            "missing_required_keys": missing,
            "missing_required_key_count": len(missing),
            "unresolved_required_keys": unresolved,
            "unresolved_required_key_count": len(unresolved),
            "unexpected_keys": unexpected,
            "unexpected_key_count": len(unexpected),
            "import_ready_for_review": ready,
            "configuration_merge_authorized": False,
            "configuration_merged": False,
            "deployment_or_restart_authorized": False,
            "environment_values_recorded": False,
            "environment_values_hashed": False,
            "secret_values_recorded": False,
        }

    if not expected:
        return posture(
            status="not_required",
            blocking_reason="",
            file_status="not_evaluated",
            file_mode=-1,
            inspection_complete=True,
            present_count=0,
            missing=[],
            unresolved=[],
            unexpected=[],
            ready=False,
        )
    try:
        text, file_mode = _read_private_text_without_digest(
            target,
            field="deployment_environment_operator_import",
            maximum_bytes=MAX_DEPLOYMENT_ENV_BYTES,
        )
    except FileNotFoundError:
        return posture(
            status="awaiting_operator_import",
            blocking_reason="operator_import_file_missing",
            file_status="missing",
            file_mode=-1,
            inspection_complete=True,
            present_count=0,
            missing=expected,
            unresolved=[],
            unexpected=[],
            ready=False,
        )
    except (OSError, ValueError):
        return posture(
            status="blocked",
            blocking_reason="operator_import_file_not_admissible",
            file_status="not_admissible",
            file_mode=(
                stat.S_IMODE(target.lstat().st_mode)
                if target.exists() and not target.is_symlink()
                else -1
            ),
            inspection_complete=False,
            present_count=0,
            missing=[],
            unresolved=expected,
            unexpected=[],
            ready=False,
        )
    try:
        presence = _dotenv_key_presence(text)
    except ValueError:
        return posture(
            status="blocked",
            blocking_reason="operator_import_file_invalid",
            file_status="invalid",
            file_mode=file_mode,
            inspection_complete=False,
            present_count=0,
            missing=[],
            unresolved=expected,
            unexpected=[],
            ready=False,
        )
    unexpected = sorted(set(presence) - set(expected))
    missing = sorted(key for key in expected if not presence.get(key))
    present_count = len(expected) - len(missing)
    if unexpected:
        return posture(
            status="blocked",
            blocking_reason="operator_import_contains_unexpected_keys",
            file_status="admissible",
            file_mode=file_mode,
            inspection_complete=True,
            present_count=present_count,
            missing=missing,
            unresolved=[],
            unexpected=unexpected,
            ready=False,
        )
    if missing:
        return posture(
            status="incomplete",
            blocking_reason="operator_import_required_keys_missing",
            file_status="admissible",
            file_mode=file_mode,
            inspection_complete=True,
            present_count=present_count,
            missing=missing,
            unresolved=[],
            unexpected=[],
            ready=False,
        )
    return posture(
        status="ready_for_review",
        blocking_reason="configuration_merge_authority_required",
        file_status="admissible",
        file_mode=file_mode,
        inspection_complete=True,
        present_count=len(expected),
        missing=[],
        unresolved=[],
        unexpected=[],
        ready=True,
    )


def _local_runtime_binding_values(*, root: Path = ROOT) -> dict[str, str]:
    root_path = Path(root).resolve(strict=True)
    owners: set[tuple[int, int]] = set()
    values: dict[str, str] = {}
    for key, relative in _LOCAL_RUNTIME_BINDING_PATHS.items():
        target = root_path / relative
        metadata = target.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid < 1
            or metadata.st_gid < 1
            or mode & 0o002
            or mode & 0o500 != 0o500
            or (
                key
                in {
                    "PROPERTYQUARRY_OODA_SIGNAL_DIR",
                    "PROPERTYQUARRY_OODA_STAGE_RECEIPT_DIR",
                }
                and mode & 0o200 == 0
            )
        ):
            raise ValueError("local_runtime_binding_source_not_admissible")
        owners.add((metadata.st_uid, metadata.st_gid))
        values[key] = str(target)
    if len(owners) != 1:
        raise ValueError("local_runtime_binding_owner_not_admissible")
    uid, gid = next(iter(owners))
    values["PROPERTYQUARRY_OODA_STAGE_UID"] = str(uid)
    values["PROPERTYQUARRY_OODA_STAGE_GID"] = str(gid)
    expected_keys = sorted(
        set(_LOCAL_RUNTIME_BINDING_PATHS)
        | _LOCAL_RUNTIME_BINDING_IDENTITY_KEYS
    )
    if sorted(values) != expected_keys:
        raise ValueError("local_runtime_binding_contract_not_admissible")
    return values


def _local_runtime_binding_candidate_bytes(
    *,
    root: Path = ROOT,
    generated_secret: str,
) -> bytes:
    if _LOCAL_GENERATED_SECRET.fullmatch(generated_secret) is None:
        raise ValueError("local_runtime_generated_secret_not_admissible")
    values = {
        **_local_runtime_binding_values(root=root),
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN": generated_secret,
    }
    if set(values) != _LOCAL_CANDIDATE_KEYS:
        raise ValueError("local_runtime_binding_contract_not_admissible")
    return (
        "".join(f"{key}={values[key]}\n" for key in sorted(values))
    ).encode("utf-8")


def inspect_local_runtime_binding_candidate(
    *,
    root: Path = ROOT,
    candidate_path: Path = DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH,
) -> dict[str, Any]:
    """Verify a private local candidate without projecting or hashing values."""

    root_path = Path(root).resolve(strict=True)
    requested_path = Path(candidate_path)
    target = requested_path if requested_path.is_absolute() else root_path / requested_path
    display_path = (
        requested_path
        if not requested_path.is_absolute()
        else Path(os.path.relpath(target, root_path))
    )
    if display_path.is_absolute() or ".." in display_path.parts:
        raise ValueError("local_runtime_binding_candidate_path_not_admissible")
    required_keys = sorted(_LOCAL_CANDIDATE_KEYS)

    def posture(
        *,
        status: str,
        reason: str,
        file_status: str,
        file_mode: int,
        sources_ready: bool,
        candidate_current: bool,
    ) -> dict[str, Any]:
        return {
            "schema": "propertyquarry.deployment_environment_local_runtime_candidate.v2",
            "source": (
                "repository_runtime_binding_derivation_and_local_secret_generation"
            ),
            "status": status,
            "blocking_reason": reason,
            "path": display_path.as_posix(),
            "file_status": file_status,
            "file_mode": file_mode,
            "required_keys": required_keys,
            "required_key_count": len(required_keys),
            "derived_runtime_binding_key_count": len(required_keys)
            - len(_LOCAL_GENERATED_SECRET_KEYS),
            "locally_generated_secret_key_count": len(
                _LOCAL_GENERATED_SECRET_KEYS
            ),
            "source_directory_count": len(_LOCAL_RUNTIME_BINDING_PATHS),
            "source_directories_ready": sources_ready,
            "common_owner_identity_resolved": sources_ready,
            "candidate_matches_current_derivation": candidate_current,
            "candidate_secret_policy_verified": candidate_current,
            "candidate_ready_for_merge_review": candidate_current,
            "candidate_values_recorded_in_receipt": False,
            "candidate_values_hashed": False,
            "candidate_contains_secret_values": True,
            "configuration_merge_authorized": False,
            "configuration_merged": False,
            "deployment_or_restart_authorized": False,
            "secret_values_recorded": False,
        }

    try:
        expected_bindings = _local_runtime_binding_values(root=root_path)
    except (OSError, ValueError):
        return posture(
            status="blocked",
            reason="local_runtime_binding_sources_not_admissible",
            file_status="not_evaluated",
            file_mode=-1,
            sources_ready=False,
            candidate_current=False,
        )
    try:
        text, file_mode = _read_private_text_without_digest(
            target,
            field="local_runtime_binding_candidate",
            maximum_bytes=MAX_DEPLOYMENT_ENV_BYTES,
        )
    except FileNotFoundError:
        return posture(
            status="candidate_missing",
            reason="local_runtime_binding_candidate_missing",
            file_status="missing",
            file_mode=-1,
            sources_ready=True,
            candidate_current=False,
        )
    except (OSError, ValueError):
        return posture(
            status="blocked",
            reason="local_runtime_binding_candidate_not_admissible",
            file_status="not_admissible",
            file_mode=-1,
            sources_ready=True,
            candidate_current=False,
        )
    try:
        values = _dotenv_values(text)
    except ValueError:
        values = {}
    generated_secret = values.get(
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN",
        "",
    )
    current = bool(
        set(values) == _LOCAL_CANDIDATE_KEYS
        and _LOCAL_GENERATED_SECRET.fullmatch(generated_secret)
        and all(
            secrets.compare_digest(values.get(key, ""), value)
            for key, value in expected_bindings.items()
        )
    )
    return posture(
        status="ready_for_merge_review" if current else "candidate_stale",
        reason=(
            "configuration_merge_authority_required"
            if current
            else "local_runtime_binding_candidate_not_current"
        ),
        file_status="admissible",
        file_mode=file_mode,
        sources_ready=True,
        candidate_current=current,
    )


def materialize_local_runtime_binding_candidate(
    *,
    root: Path = ROOT,
    candidate_path: Path = DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH,
) -> dict[str, Any]:
    """Stage an idempotent private candidate without merging or deploying it."""

    root_path = Path(root).resolve(strict=True)
    requested_path = Path(candidate_path)
    target = requested_path if requested_path.is_absolute() else root_path / requested_path
    display_path = (
        requested_path
        if not requested_path.is_absolute()
        else Path(os.path.relpath(target, root_path))
    )
    if display_path.is_absolute() or ".." in display_path.parts:
        raise ValueError("local_runtime_binding_candidate_path_not_admissible")
    try:
        generated_secret = ""
        try:
            existing_text, _mode = _read_private_text_without_digest(
                target,
                field="local_runtime_binding_candidate",
                maximum_bytes=MAX_DEPLOYMENT_ENV_BYTES,
            )
            existing_values = _dotenv_values(existing_text)
            existing_keys = set(existing_values)
            legacy_keys = (
                set(_LOCAL_RUNTIME_BINDING_PATHS)
                | _LOCAL_RUNTIME_BINDING_IDENTITY_KEYS
            )
            if existing_keys == _LOCAL_CANDIDATE_KEYS:
                generated_secret = existing_values.get(
                    "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN",
                    "",
                )
                if _LOCAL_GENERATED_SECRET.fullmatch(generated_secret) is None:
                    raise ValueError(
                        "local_runtime_generated_secret_not_admissible"
                    )
            elif existing_keys != legacy_keys:
                raise ValueError("local_runtime_binding_candidate_not_admissible")
        except FileNotFoundError:
            pass
        if not generated_secret:
            generated_secret = secrets.token_urlsafe(48)
        payload = _local_runtime_binding_candidate_bytes(
            root=root_path,
            generated_secret=generated_secret,
        )
        atomic_write_bytes(target, payload, overwrite=True)
    except (OSError, ValueError):
        return inspect_local_runtime_binding_candidate(
            root=root_path,
            candidate_path=display_path,
        )
    return inspect_local_runtime_binding_candidate(
        root=root_path,
        candidate_path=display_path,
    )


def _deployment_environment_requirements(
    *,
    root: Path = ROOT,
    environment_paths: Sequence[Path] | None = None,
) -> tuple[list[str], str]:
    source_paths = [path.as_posix() for path in local_deployment.COMPOSE_FILES]
    environment_sources = [
        Path(path).as_posix()
        for path in (environment_paths or (DEFAULT_DEPLOYMENT_ENV_PATH,))
    ]
    required: set[str] = set()
    for relative in local_deployment.COMPOSE_FILES:
        raw, _digest = _admissible_snapshot(
            Path(root) / relative,
            field="deployment_compose_source",
            maximum_bytes=MAX_RELEASE_MANIFEST_BYTES,
            private=False,
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("deployment_compose_source_not_admissible") from exc
        required.update(_REQUIRED_ENV_EXPRESSION.findall(text))
    keys = sorted(required)
    if not keys:
        raise ValueError("deployment_environment_requirements_not_admissible")
    fingerprint = _sha256(
        _canonical(
            {
                "requirement_sources": source_paths,
                "environment_sources": environment_sources,
                "required_keys": keys,
            }
        )
    )
    return keys, fingerprint


def _deployment_environment_intake_plan(
    *,
    required_keys: list[str],
    missing_required_keys: list[str],
    unresolved_required_keys: list[str],
    requirement_fingerprint_sha256: str,
    operator_import_posture: Mapping[str, Any] | None = None,
    local_runtime_candidate_posture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify value-free requirements without guessing unknown keys."""

    required = set(required_keys)
    missing = set(missing_required_keys)
    unresolved = set(unresolved_required_keys)
    classified: set[str] = set()
    rows: list[dict[str, Any]] = []
    external_missing: list[str] = []
    locally_stageable: list[str] = []
    for contract in _DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES:
        keys = sorted(required & set(contract["keys"]))
        classified.update(keys)
        missing_keys = sorted(missing & set(keys))
        unresolved_keys = sorted(unresolved & set(keys))
        if contract["operator_value_required"] is True:
            external_missing.extend(missing_keys)
        else:
            locally_stageable.extend(missing_keys)
        rows.append(
            {
                "classification": contract["classification"],
                "resolution_lane": contract["resolution_lane"],
                "operator_value_required": contract[
                    "operator_value_required"
                ],
                "required_keys": keys,
                "required_key_count": len(keys),
                "present_nonempty_key_count": (
                    len(keys) - len(missing_keys) - len(unresolved_keys)
                ),
                "missing_keys": missing_keys,
                "missing_key_count": len(missing_keys),
                "unresolved_keys": unresolved_keys,
                "unresolved_key_count": len(unresolved_keys),
            }
        )
    unclassified = sorted(required - classified)
    external_missing = sorted(external_missing)
    locally_stageable = sorted(locally_stageable)
    operator_import = dict(
        operator_import_posture
        or {
            "schema": "propertyquarry.deployment_environment_operator_import.v1",
            "status": (
                "awaiting_operator_import"
                if external_missing
                else "not_required"
            ),
            "blocking_reason": (
                "operator_import_file_missing" if external_missing else ""
            ),
            "path": DEFAULT_DEPLOYMENT_ENV_OPERATOR_IMPORT_PATH.as_posix(),
            "file_status": (
                "missing" if external_missing else "not_evaluated"
            ),
            "file_mode": -1,
            "inspection_complete": True,
            "required_keys": external_missing,
            "required_key_count": len(external_missing),
            "present_nonempty_required_key_count": 0,
            "missing_required_keys": external_missing,
            "missing_required_key_count": len(external_missing),
            "unresolved_required_keys": [],
            "unresolved_required_key_count": 0,
            "unexpected_keys": [],
            "unexpected_key_count": 0,
            "import_ready_for_review": False,
            "configuration_merge_authorized": False,
            "configuration_merged": False,
            "deployment_or_restart_authorized": False,
            "environment_values_recorded": False,
            "environment_values_hashed": False,
            "secret_values_recorded": False,
        }
    )
    local_candidate_keys = sorted(required & _LOCAL_CANDIDATE_KEYS)
    local_runtime_candidate = dict(
        local_runtime_candidate_posture
        or {
            "schema": "propertyquarry.deployment_environment_local_runtime_candidate.v2",
            "source": (
                "repository_runtime_binding_derivation_and_local_secret_generation"
            ),
            "status": "candidate_missing",
            "blocking_reason": "local_runtime_binding_candidate_missing",
            "path": DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH.as_posix(),
            "file_status": "missing",
            "file_mode": -1,
            "required_keys": local_candidate_keys,
            "required_key_count": len(local_candidate_keys),
            "derived_runtime_binding_key_count": len(
                set(local_candidate_keys) - _LOCAL_GENERATED_SECRET_KEYS
            ),
            "locally_generated_secret_key_count": len(
                set(local_candidate_keys) & _LOCAL_GENERATED_SECRET_KEYS
            ),
            "source_directory_count": len(_LOCAL_RUNTIME_BINDING_PATHS),
            "source_directories_ready": False,
            "common_owner_identity_resolved": False,
            "candidate_matches_current_derivation": False,
            "candidate_secret_policy_verified": False,
            "candidate_ready_for_merge_review": False,
            "candidate_values_recorded_in_receipt": False,
            "candidate_values_hashed": False,
            "candidate_contains_secret_values": bool(
                set(local_candidate_keys) & _LOCAL_GENERATED_SECRET_KEYS
            ),
            "configuration_merge_authorized": False,
            "configuration_merged": False,
            "deployment_or_restart_authorized": False,
            "secret_values_recorded": False,
        }
    )
    locally_staged = (
        sorted(set(locally_stageable) & set(local_candidate_keys))
        if local_runtime_candidate.get("candidate_ready_for_merge_review") is True
        else []
    )
    locally_pending = sorted(set(locally_stageable) - set(locally_staged))
    local_merge_review_keys = (
        list(locally_staged) if locally_staged and not locally_pending else []
    )
    import_ready = operator_import.get("import_ready_for_review") is True
    import_blocked = operator_import.get("status") == "blocked"
    operator_external_input_keys = (
        [] if import_ready else list(external_missing)
    )
    merge_review_keys = list(external_missing) if import_ready else []
    import_path = str(operator_import.get("path") or "").strip()
    if unresolved:
        status = "environment_inspection_blocked"
        blocking_reason = "deployment_environment_presence_unresolved"
        next_action = (
            "repair the private deployment environment file, then rerun the "
            "value-free readiness inspection"
        )
    elif unclassified:
        status = "classification_required"
        blocking_reason = "deployment_environment_requirements_unclassified"
        next_action = (
            "classify the named required keys in the governed deployment "
            "environment registry before requesting or staging any values"
        )
    elif external_missing:
        if import_ready:
            status = "operator_merge_review_required"
            blocking_reason = "external_account_material_merge_review_required"
            next_action = (
                "review the staged external account import at "
                + import_path
                + " and record explicit runtime configuration merge authority "
                "separately; no values were recorded or hashed"
            )
        elif import_blocked:
            status = "operator_import_blocked"
            blocking_reason = str(
                operator_import.get("blocking_reason")
                or "operator_import_file_not_admissible"
            )
            next_action = (
                "repair the private operator-import fragment at "
                + import_path
                + " so it is owner-only and contains exactly the named keys; "
                "do not paste values into chat: "
                + ", ".join(external_missing)
            )
        else:
            status = "operator_input_required"
            blocking_reason = "external_account_material_required"
            next_action = (
                "place a private dotenv fragment containing exactly the named "
                "keys at "
                + import_path
                + "; set mode 0600 and do not paste values into chat: "
                + ", ".join(external_missing)
            )
    elif locally_pending:
        status = "local_staging_required"
        blocking_reason = "local_deployment_configuration_staging_required"
        next_action = (
            "materialize a private, review-only candidate for the named local "
            "requirements; do not apply configuration or restart services"
        )
    elif local_merge_review_keys:
        status = "local_merge_review_required"
        blocking_reason = "local_deployment_configuration_merge_review_required"
        next_action = (
            "review the staged local deployment candidate at "
            + str(local_runtime_candidate.get("path") or "")
            + " and record explicit runtime configuration merge authority "
            "separately; no values were recorded or hashed"
        )
    else:
        status = "ready"
        blocking_reason = ""
        next_action = (
            "review the exact recovery preview and record deployment or "
            "restart authority separately"
        )
    return {
        "schema": "propertyquarry.deployment_environment_intake_plan.v1",
        "status": status,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "requirement_fingerprint_sha256": requirement_fingerprint_sha256,
        "required_key_count": len(required_keys),
        "missing_required_key_count": len(missing_required_keys),
        "unresolved_required_key_count": len(unresolved_required_keys),
        "classifications": rows,
        "operator_import": operator_import,
        "local_runtime_binding_candidate": local_runtime_candidate,
        "operator_external_input_required": bool(
            operator_external_input_keys
        ),
        "operator_external_input_keys": operator_external_input_keys,
        "operator_external_input_key_count": len(
            operator_external_input_keys
        ),
        "operator_merge_review_required": bool(merge_review_keys),
        "operator_merge_review_keys": merge_review_keys,
        "operator_merge_review_key_count": len(merge_review_keys),
        "locally_stageable_keys": locally_stageable,
        "locally_stageable_key_count": len(locally_stageable),
        "locally_staged_keys": locally_staged,
        "locally_staged_key_count": len(locally_staged),
        "locally_pending_keys": locally_pending,
        "locally_pending_key_count": len(locally_pending),
        "local_merge_review_required": bool(local_merge_review_keys),
        "local_merge_review_keys": local_merge_review_keys,
        "local_merge_review_key_count": len(local_merge_review_keys),
        "unclassified_required_keys": unclassified,
        "unclassified_required_key_count": len(unclassified),
        "plan_materialization_allowed": True,
        "configuration_apply_authorized": False,
        "deployment_or_restart_authorized": False,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }


def _deployment_environment_blocked(
    *,
    required_keys: list[str],
    requirement_fingerprint_sha256: str,
    env_path: Path,
    file_status: str,
    file_mode: int,
    reason: str,
    environment_sources: list[str],
    environment_layers: list[dict[str, Any]],
) -> dict[str, Any]:
    unresolved = list(required_keys)
    intake_plan = _deployment_environment_intake_plan(
        required_keys=required_keys,
        missing_required_keys=[],
        unresolved_required_keys=unresolved,
        requirement_fingerprint_sha256=requirement_fingerprint_sha256,
    )
    return {
        "source": "private_dotenv_layer_presence_inspection",
        "status": "blocked",
        "blocking_reason": reason,
        "path": Path(env_path).as_posix(),
        "file_status": file_status,
        "file_mode": file_mode,
        "inspection_complete": False,
        "environment_sources": environment_sources,
        "environment_source_count": len(environment_sources),
        "environment_layers": environment_layers,
        "requirement_sources": [
            path.as_posix() for path in local_deployment.COMPOSE_FILES
        ],
        "requirement_fingerprint_sha256": requirement_fingerprint_sha256,
        "required_keys": required_keys,
        "required_key_count": len(required_keys),
        "present_nonempty_required_key_count": 0,
        "missing_required_keys": [],
        "missing_required_key_count": 0,
        "unresolved_required_keys": unresolved,
        "unresolved_required_key_count": len(required_keys),
        "configuration_complete": False,
        "intake_plan": intake_plan,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }


def inspect_deployment_environment(
    *,
    root: Path = ROOT,
    env_path: Path = DEFAULT_DEPLOYMENT_ENV_PATH,
    operator_import_path: Path = DEFAULT_DEPLOYMENT_ENV_OPERATOR_IMPORT_PATH,
    environment_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Inspect ordered private env layers without projecting or hashing values."""

    root_path = Path(root).resolve(strict=True)
    requested_path = Path(env_path)
    requested_layers = tuple(
        environment_paths
        if environment_paths is not None
        else (
            DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS
            if root_path == ROOT.resolve()
            and (
                requested_path == DEFAULT_DEPLOYMENT_ENV_PATH
                or requested_path
                == root_path / DEFAULT_DEPLOYMENT_ENV_PATH
            )
            else (requested_path,)
        )
    )
    if not requested_layers:
        raise ValueError("deployment_environment_layers_not_admissible")
    resolved_layers: list[tuple[Path, Path]] = []
    for raw_path in requested_layers:
        layer_path = Path(raw_path)
        target = layer_path if layer_path.is_absolute() else root_path / layer_path
        display = (
            layer_path
            if not layer_path.is_absolute()
            else Path(os.path.relpath(target, root_path))
        )
        if display.is_absolute() or ".." in display.parts:
            raise ValueError("deployment_environment_path_not_admissible")
        resolved_layers.append((display, target))
    environment_sources = [path.as_posix() for path, _target in resolved_layers]
    if len(environment_sources) != len(set(environment_sources)):
        raise ValueError("deployment_environment_layers_not_admissible")
    display_path = resolved_layers[0][0]
    required_keys, requirement_fingerprint = _deployment_environment_requirements(
        root=root_path,
        environment_paths=[path for path, _target in resolved_layers],
    )
    effective_values: dict[str, str] = {}
    environment_layers: list[dict[str, Any]] = []
    for layer_display, target in resolved_layers:
        try:
            text, layer_mode = _read_private_text_without_digest(
                target,
                field="deployment_environment",
                maximum_bytes=MAX_DEPLOYMENT_ENV_BYTES,
            )
            values = _dotenv_values(text)
        except FileNotFoundError:
            environment_layers.append(
                {
                    "path": layer_display.as_posix(),
                    "file_status": "missing",
                    "file_mode": -1,
                    "inspection_complete": False,
                    "present_nonempty_required_key_count": 0,
                    "environment_values_recorded": False,
                    "environment_values_hashed": False,
                    "secret_values_recorded": False,
                }
            )
            return _deployment_environment_blocked(
                required_keys=required_keys,
                requirement_fingerprint_sha256=requirement_fingerprint,
                env_path=display_path,
                file_status="missing",
                file_mode=-1,
                reason="deployment_environment_file_missing",
                environment_sources=environment_sources,
                environment_layers=environment_layers,
            )
        except (OSError, ValueError):
            layer_mode = (
                stat.S_IMODE(target.lstat().st_mode)
                if target.exists() and not target.is_symlink()
                else -1
            )
            environment_layers.append(
                {
                    "path": layer_display.as_posix(),
                    "file_status": "not_admissible",
                    "file_mode": layer_mode,
                    "inspection_complete": False,
                    "present_nonempty_required_key_count": 0,
                    "environment_values_recorded": False,
                    "environment_values_hashed": False,
                    "secret_values_recorded": False,
                }
            )
            return _deployment_environment_blocked(
                required_keys=required_keys,
                requirement_fingerprint_sha256=requirement_fingerprint,
                env_path=display_path,
                file_status="not_admissible",
                file_mode=layer_mode,
                reason="deployment_environment_file_not_admissible",
                environment_sources=environment_sources,
                environment_layers=environment_layers,
            )
        environment_layers.append(
            {
                "path": layer_display.as_posix(),
                "file_status": "admissible",
                "file_mode": layer_mode,
                "inspection_complete": True,
                "present_nonempty_required_key_count": sum(
                    bool(values.get(key)) for key in required_keys
                ),
                "environment_values_recorded": False,
                "environment_values_hashed": False,
                "secret_values_recorded": False,
            }
        )
        effective_values.update(values)
    file_mode = environment_layers[0]["file_mode"]
    presence = {key: bool(value) for key, value in effective_values.items()}
    missing = sorted(key for key in required_keys if not presence.get(key))
    present_count = len(required_keys) - len(missing)
    complete = not missing
    preliminary_plan = _deployment_environment_intake_plan(
        required_keys=required_keys,
        missing_required_keys=missing,
        unresolved_required_keys=[],
        requirement_fingerprint_sha256=requirement_fingerprint,
    )
    operator_import = inspect_deployment_environment_operator_import(
        required_keys=list(
            preliminary_plan["operator_external_input_keys"]
        ),
        root=root_path,
        import_path=operator_import_path,
    )
    local_runtime_candidate = inspect_local_runtime_binding_candidate(
        root=root_path,
    )
    intake_plan = _deployment_environment_intake_plan(
        required_keys=required_keys,
        missing_required_keys=missing,
        unresolved_required_keys=[],
        requirement_fingerprint_sha256=requirement_fingerprint,
        operator_import_posture=operator_import,
        local_runtime_candidate_posture=local_runtime_candidate,
    )
    return {
        "source": "private_dotenv_layer_presence_inspection",
        "status": "ready" if complete else "incomplete",
        "blocking_reason": (
            "" if complete else "required_deployment_environment_keys_missing"
        ),
        "path": display_path.as_posix(),
        "file_status": "admissible",
        "file_mode": file_mode,
        "inspection_complete": True,
        "environment_sources": environment_sources,
        "environment_source_count": len(environment_sources),
        "environment_layers": environment_layers,
        "requirement_sources": [
            path.as_posix() for path in local_deployment.COMPOSE_FILES
        ],
        "requirement_fingerprint_sha256": requirement_fingerprint,
        "required_keys": required_keys,
        "required_key_count": len(required_keys),
        "present_nonempty_required_key_count": present_count,
        "missing_required_keys": missing,
        "missing_required_key_count": len(missing),
        "unresolved_required_keys": [],
        "unresolved_required_key_count": 0,
        "configuration_complete": complete,
        "intake_plan": intake_plan,
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }


def _deployment_environment_posture_admissible(
    posture: Mapping[str, Any],
) -> bool:
    try:
        sources = list(posture.get("environment_sources") or [])
        if not sources or any(not isinstance(item, str) for item in sources):
            return False
        paths = [Path(item) for item in sources]
        if any(
            path.is_absolute()
            or ".." in path.parts
            or path.name in {"", ".", ".."}
            for path in paths
        ):
            return False
        expected = inspect_deployment_environment(
            env_path=paths[0],
            environment_paths=paths,
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return dict(posture) == expected


def _local_origin(value: object) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("live_mobile_base_origin_not_admissible") from exc
    hostname = str(parsed.hostname or "").strip().lower()
    if hostname in {"localhost", "::1"}:
        hostname = "127.0.0.1"
    if (
        parsed.scheme != "http"
        or hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("live_mobile_base_origin_not_admissible")
    return f"http://127.0.0.1:{port}"


def _public_origin(value: object) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("release_public_origin_not_admissible") from exc
    hostname = str(parsed.hostname or "").strip().lower()
    if (
        parsed.scheme != "https"
        or not _PUBLIC_HOST.fullmatch(hostname)
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("release_public_origin_not_admissible")
    return f"https://{hostname}", hostname


def _probe_fingerprint_payload(probe: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "source_sha256": probe.get("source_sha256"),
        "source_generated_at": probe.get("source_generated_at"),
        "status": probe.get("status"),
        "base_origin": probe.get("base_origin"),
        "host_header": probe.get("host_header"),
        "failure_code": probe.get("failure_code"),
    }
    if probe.get("failure_code") == "cloudflare_1033_tunnel_unavailable":
        fields.update(
            {
                "edge_provider": probe.get("edge_provider"),
                "edge_error_code": probe.get("edge_error_code"),
                "http_status": probe.get("http_status"),
                "failure_reason": probe.get("failure_reason"),
            }
        )
    return fields


def _live_mobile_probe_posture(
    *,
    receipt_path: Path,
    expected_source_generated_at: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    target = _rooted(receipt_path)
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("live_mobile_receipt_not_admissible")
    payload, raw, digest = load_strict_json_object_snapshot(
        target,
        field="live_mobile_receipt",
        maximum_bytes=MAX_LIVE_MOBILE_RECEIPT_BYTES,
    )
    generated_at = str(payload.get("generated_at") or "").strip()
    if not (
        payload.get("status") == "blocked"
        and generated_at == str(expected_source_generated_at or "").strip()
        and operator_status._parse_fresh_timestamp(
            generated_at,
            now=_now(now),
            max_age_seconds=24.0 * 3600.0,
        )
        is not None
    ):
        raise ValueError("live_mobile_receipt_not_admissible")

    if payload.get("schema") == _PUBLIC_ORIGIN_OBSERVATION_SCHEMA:
        observation = payload.get("observation")
        request = payload.get("request")
        if not (
            set(payload)
            == {
                "schema",
                "generated_at",
                "status",
                "origin",
                "observation",
                "request",
                "response_headers_recorded",
                "response_content_recorded",
                "action_required",
                "interrupt_operator",
                "automatic_execution_allowed",
                "execution_authorized",
                "deployment_or_restart_authorized",
                "protected_operation_executed",
                "provider_quota_consumption_allowed",
                "delivery_authorized",
                "delivery_attempted",
                "sent",
            }
            and observation
            == {
                "edge_provider": "cloudflare",
                "error_code": 1033,
                "http_status": 530,
                "reason": "cloudflare_tunnel_unavailable",
            }
            and request
            == {
                "credentials_sent": False,
                "method": "GET",
                "path": "/",
                "redirects_followed": False,
                "tls_validation": "system_trust_store",
            }
            and payload.get("response_headers_recorded") is False
            and payload.get("response_content_recorded") is False
            and payload.get("action_required") is False
            and payload.get("interrupt_operator") is False
            and payload.get("automatic_execution_allowed") is False
            and payload.get("execution_authorized") is False
            and payload.get("deployment_or_restart_authorized") is False
            and payload.get("protected_operation_executed") is False
            and payload.get("provider_quota_consumption_allowed") is False
            and payload.get("delivery_authorized") is False
            and payload.get("delivery_attempted") is False
            and payload.get("sent") is False
        ):
            raise ValueError("live_mobile_receipt_not_admissible")
        base_origin, public_host = _public_origin(payload.get("origin"))
        posture: dict[str, Any] = {
            "source": "public_https_origin_observation",
            "source_sha256": digest,
            "source_bytes": len(raw),
            "source_generated_at": generated_at,
            "status": "blocked",
            "base_origin": base_origin,
            "host_header": public_host,
            "failure_code": "cloudflare_1033_tunnel_unavailable",
            "edge_provider": "cloudflare",
            "edge_error_code": "1033",
            "http_status": 530,
            "failure_reason": "tunnel_unavailable",
            "secret_values_recorded": False,
        }
        posture["fingerprint_sha256"] = _sha256(
            _canonical(_probe_fingerprint_payload(posture))
        )
        return posture

    edge_failure = payload.get("edge_failure")
    routes = list(payload.get("routes") or [])
    route_count: object = payload.get("route_count")
    if (
        edge_failure == _CLOUDFLARE_TUNNEL_FAILURE
        and payload.get("error") == "cloudflare_error_1033_tunnel_unavailable"
        and isinstance(route_count, int)
        and not isinstance(route_count, bool)
        and route_count == len(routes) >= 1
        and all(
            isinstance(row, dict)
            and row.get("ok") is False
            and row.get("status_code") == 530
            and isinstance(row.get("metrics"), dict)
            and row["metrics"].get("edge_failure") == _CLOUDFLARE_TUNNEL_FAILURE
            for row in routes
        )
    ):
        base_origin, public_host = _public_origin(payload.get("base_url"))
        configured_host = str(payload.get("host_header") or "").strip().lower()
        if configured_host not in {"", public_host}:
            raise ValueError("live_mobile_receipt_not_admissible")
        posture: dict[str, Any] = {
            "source": "public_https_live_mobile_receipt",
            "source_sha256": digest,
            "source_bytes": len(raw),
            "source_generated_at": generated_at,
            "status": "blocked",
            "base_origin": base_origin,
            "host_header": public_host,
            "failure_code": "cloudflare_1033_tunnel_unavailable",
            "edge_provider": "cloudflare",
            "edge_error_code": "1033",
            "http_status": 530,
            "failure_reason": "tunnel_unavailable",
            "secret_values_recorded": False,
        }
        posture["fingerprint_sha256"] = _sha256(
            _canonical(_probe_fingerprint_payload(posture))
        )
        return posture
    if edge_failure is not None or payload.get("error") == "cloudflare_error_1033_tunnel_unavailable":
        raise ValueError("live_mobile_receipt_not_admissible")

    base_origin = _local_origin(payload.get("base_url"))
    host_header = str(payload.get("host_header") or "").strip().lower()
    errors = [str(payload.get("error") or "").strip()]
    errors.extend(
        str(row.get("error") or "").strip()
        for row in list(payload.get("coverage_checks") or [])
        if isinstance(row, dict) and row.get("ok") is not True
    )
    normalized_errors = " ".join(errors).lower()
    if not (
        _PUBLIC_HOST.fullmatch(host_header)
        and any(
            marker in normalized_errors
            for marker in ("http error 421", "misdirected request", "host_not_allowed")
        )
    ):
        raise ValueError("live_mobile_receipt_not_admissible")
    posture = {
        "source": "local_private_live_mobile_receipt",
        "source_sha256": digest,
        "source_bytes": len(raw),
        "source_generated_at": generated_at,
        "status": "blocked",
        "base_origin": base_origin,
        "host_header": host_header,
        "failure_code": "http_421_misdirected_request",
        "secret_values_recorded": False,
    }
    posture["fingerprint_sha256"] = _sha256(
        _canonical(_probe_fingerprint_payload(posture))
    )
    return posture


def _release_authority_posture(
    *,
    manifest_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    raw, digest = _admissible_snapshot(
        manifest_path,
        field="release_manifest",
        maximum_bytes=MAX_RELEASE_MANIFEST_BYTES,
        private=False,
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("release_manifest_not_admissible") from exc
    if (
        text.count(launch_room.MANIFEST_START) != 1
        or text.count(launch_room.MANIFEST_END) != 1
    ):
        raise ValueError("release_manifest_not_admissible")
    marked = text.split(launch_room.MANIFEST_START, 1)[1].split(
        launch_room.MANIFEST_END,
        1,
    )[0]
    match = re.search(r"```json\s*(\{.*\})\s*```", marked, flags=re.DOTALL)
    if match is None:
        raise ValueError("release_manifest_not_admissible")
    try:
        values = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("release_manifest_not_admissible") from exc
    if not isinstance(values, dict):
        raise ValueError("release_manifest_not_admissible")
    public_origin, public_host = _public_origin(values.get("release_public_origin"))
    generated_at = str(values.get("release_generated_at") or "").strip()
    deployment_id = str(values.get("release_deployment_id") or "").strip()
    if not (
        values.get("release_repository") == local_deployment.REPOSITORY
        and operator_status._parse_fresh_timestamp(
            generated_at,
            now=_now(now),
            max_age_seconds=365.0 * 24.0 * 3600.0,
        )
        is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", deployment_id)
    ):
        raise ValueError("release_manifest_not_admissible")
    return {
        "source": "repository_release_manifest",
        "source_sha256": digest,
        "source_bytes": len(raw),
        "release_repository": local_deployment.REPOSITORY,
        "release_public_origin": public_origin,
        "release_public_host": public_host,
        "release_deployment_id": deployment_id,
        "release_generated_at": generated_at,
        "secret_values_recorded": False,
    }


def _configuration_proposal(
    *,
    probe_posture: Mapping[str, Any],
    release_authority: Mapping[str, Any],
    runtime_posture: Mapping[str, Any],
    release_posture: Mapping[str, Any],
    deployment_environment_posture: Mapping[str, Any],
) -> dict[str, Any]:
    required_services = sorted(local_deployment.SERVICE_CONTRACT)
    observed_services = {
        str(row.get("service") or "")
        for row in list(runtime_posture.get("services") or [])
        if isinstance(row, dict)
    }
    missing_services = sorted(set(required_services) - observed_services)
    failure_code = str(probe_posture.get("failure_code") or "")
    edge_tunnel_unavailable = failure_code == "cloudflare_1033_tunnel_unavailable"
    target_origin = (
        _public_origin(probe_posture.get("base_origin"))[0]
        if edge_tunnel_unavailable
        else _local_origin(local_deployment.DEFAULT_LOCAL_ORIGIN)
    )
    current_origin = (
        _public_origin(probe_posture.get("base_origin"))[0]
        if edge_tunnel_unavailable
        else _local_origin(probe_posture.get("base_origin"))
    )
    public_origin = str(release_authority.get("release_public_origin") or "")
    public_host = str(release_authority.get("release_public_host") or "")
    probe_host = str(probe_posture.get("host_header") or "")
    target_mismatch = current_origin != target_origin
    host_mismatch = probe_host != public_host
    findings: list[str] = []
    if edge_tunnel_unavailable:
        findings.append("public_edge_tunnel_connector_unavailable")
    if target_mismatch:
        findings.append("live_probe_not_targeting_dedicated_runtime")
    if host_mismatch:
        findings.append("live_probe_host_not_bound_to_release_origin")
    if missing_services:
        findings.append("dedicated_runtime_services_absent")
    if not findings:
        findings.append("configuration_aligned_pending_authorized_reprobe")
    incident = (
        {
            "kind": "edge_connector_unavailable",
            "provider": str(probe_posture.get("edge_provider") or ""),
            "code": str(probe_posture.get("edge_error_code") or ""),
            "http_status": int(probe_posture.get("http_status") or 0),
            "reason": str(probe_posture.get("failure_reason") or ""),
            "source": "public_https_probe",
        }
        if edge_tunnel_unavailable
        else {
            "kind": "host_admission_rejected",
            "provider": "application_edge",
            "code": "http_421",
            "http_status": 421,
            "reason": "misdirected_request",
            "source": "local_host_header_probe",
        }
    )
    all_required_services_absent = len(missing_services) == len(required_services)
    recovery_blockers = ["deployment_environment_authority_not_verified"]
    if deployment_environment_posture.get("configuration_complete") is not True:
        recovery_blockers.insert(0, "deployment_environment_not_ready")
    if release_posture.get("worktree_clean") is not True:
        recovery_blockers.append("release_worktree_not_clean")
    if not all_required_services_absent:
        recovery_blockers.append("rollback_to_observed_state_not_exact")
    recovery_preview = (
        {
            "operation": "deployment_or_restart",
            "compose_project": local_deployment.DEFAULT_COMPOSE_PROJECT,
            "compose_files": [
                path.as_posix() for path in local_deployment.COMPOSE_FILES
            ],
            "connector_service": "propertyquarry-cloudflared",
            "application_service": "propertyquarry-api",
            "service_targets": [
                {
                    "service": service,
                    "required_state": local_deployment.SERVICE_CONTRACT[service],
                    "observed_state": (
                        "absent" if service in missing_services else "present"
                    ),
                }
                for service in required_services
            ],
            "source_binding": {
                "runtime_fingerprint_sha256": str(
                    runtime_posture.get("fingerprint_sha256") or ""
                ),
                "release_head_sha": str(release_posture.get("head_sha") or ""),
                "release_worktree_fingerprint_sha256": str(
                    release_posture.get("worktree_fingerprint_sha256") or ""
                ),
            },
            "deployment_environment": dict(deployment_environment_posture),
            "command": _compose_command("start"),
            "rollback_command": (
                _compose_command("rollback_to_absent")
                if all_required_services_absent
                else []
            ),
            "pre_state": (
                "all_required_services_absent"
                if all_required_services_absent
                else "required_services_present_or_partial"
            ),
            "reversible_to_observed_state": all_required_services_absent,
            "blocking_reasons": recovery_blockers,
            "deployment_environment_values_recorded": False,
            "deployment_environment_authority_verified": False,
            "eligible_for_authorization": False,
            "preview_only": True,
            "command_recorded": True,
            "rollback_command_recorded": all_required_services_absent,
            "authorization_recorded": False,
            "execution_authorized": False,
            "deployment_or_restart_performed": False,
        }
        if edge_tunnel_unavailable
        else {}
    )
    return {
        "status": "review_required",
        "incident": incident,
        "recovery_preview": recovery_preview,
        "findings": findings,
        "current_probe_origin": current_origin,
        "proposed_probe_origin": target_origin,
        "current_probe_host": probe_host,
        "proposed_probe_host": public_host,
        "release_public_origin": public_origin,
        "compose_project": local_deployment.DEFAULT_COMPOSE_PROJECT,
        "required_services": required_services,
        "missing_services": missing_services,
        "probe_target_change_proposed": target_mismatch,
        "probe_host_change_proposed": host_mismatch,
        "dedicated_runtime_present": not missing_services,
        "authorization_required": True,
        "execution_authorized": False,
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "provider_quota_consumption_allowed": False,
        "secret_values_recorded": False,
    }


def _review_packet_next_action(
    reason: str,
    proposal: Mapping[str, Any],
) -> str:
    if reason == _TUNNEL_ACTION_REASON:
        recovery_preview = proposal.get("recovery_preview")
        environment = (
            recovery_preview.get("deployment_environment")
            if isinstance(recovery_preview, Mapping)
            else None
        )
        intake_plan = (
            environment.get("intake_plan")
            if isinstance(environment, Mapping)
            else None
        )
        if isinstance(intake_plan, Mapping):
            next_action = str(intake_plan.get("next_action") or "").strip()
            if next_action:
                return next_action
    return (
        "review this packet and record explicit authority separately before "
        "any runtime configuration change, deployment, or restart"
    )


def _runtime_consent_gate(
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind consent to one policy-bounded preview without binding secret values."""

    recovery_preview = proposal.get("recovery_preview")
    environment = (
        recovery_preview.get("deployment_environment")
        if isinstance(recovery_preview, Mapping)
        else None
    )
    intake = (
        environment.get("intake_plan")
        if isinstance(environment, Mapping)
        else None
    )
    local_merge_keys = (
        list(intake.get("local_merge_review_keys") or [])
        if isinstance(intake, Mapping)
        and intake.get("local_merge_review_required") is True
        else []
    )
    if not local_merge_keys:
        return {
            "required": True,
            "authorization_recorded": False,
            "execution_authorized": False,
            "protected_operations": list(_PROTECTED_OPERATIONS),
        }
    candidate = intake.get("local_runtime_binding_candidate")
    if not (
        isinstance(candidate, Mapping)
        and candidate.get("status") == "ready_for_merge_review"
        and candidate.get("candidate_ready_for_merge_review") is True
        and local_merge_keys == sorted(set(local_merge_keys))
        and len(local_merge_keys) == intake.get("local_merge_review_key_count")
    ):
        raise ValueError("runtime_consent_scope_not_admissible")
    recovery_preview_sha256 = _sha256(_canonical(dict(recovery_preview)))
    authorization_scope = {
        "operation": "add_missing_local_environment_candidate",
        "recovery_preview_sha256": recovery_preview_sha256,
        "candidate_path": str(candidate.get("path") or ""),
        "candidate_policy_schema": str(candidate.get("schema") or ""),
        "candidate_review_keys": local_merge_keys,
        "candidate_review_key_count": len(local_merge_keys),
        "configuration_merge_policy": "add_missing_keys_only",
        "existing_environment_values_overwrite_allowed": False,
        "rollback_required": True,
        "protected_operations": ["runtime_configuration_change"],
    }
    request_id = "pqruntimeconsent_" + _sha256(
        _canonical(authorization_scope)
    )[:24]
    instruction = (
        "reply exactly 'Authorize "
        + request_id
        + "' to authorize only this current add-only configuration merge; "
        "any changed or stale preview requires a new request"
    )
    return {
        "required": True,
        "request_state": "exact_preview_authorization_pending",
        "request_id": request_id,
        "freshness_window_seconds": (
            _RUNTIME_CONSENT_FRESHNESS_WINDOW_SECONDS
        ),
        "decision_options": list(_RUNTIME_CONSENT_DECISIONS),
        "authorization_instruction": instruction,
        "authorization_method_required": "authenticated_operator_instruction",
        "authorization_scope": authorization_scope,
        "authorization_evidence_recorded": False,
        "authorization_recorded": False,
        "configuration_merge_authorized": False,
        "deployment_or_restart_authorized": False,
        "execution_authorized": False,
        "protected_operations": list(_PROTECTED_OPERATIONS),
    }


def _review_items(
    reason: str,
    *,
    release_clean: bool,
    proposal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if reason not in _REVIEW_ACTION_REASONS:
        raise ValueError("runtime_review_reason_not_admissible")
    if reason == _TUNNEL_ACTION_REASON:
        recovery_preview = proposal.get("recovery_preview")
        deployment_environment = (
            recovery_preview.get("deployment_environment")
            if isinstance(recovery_preview, Mapping)
            else None
        )
        intake_plan = (
            deployment_environment.get("intake_plan") or {}
            if isinstance(deployment_environment, Mapping)
            else {}
        )
        return [
            {
                "id": "public_probe",
                "status": "blocked_at_edge",
                "protected_operation": "deployment_or_restart",
                "question": "Confirm the normalized public HTTPS probe is bound to the intended PropertyQuarry release origin and edge incident.",
            },
            {
                "id": "edge_connector",
                "status": "requires_review",
                "protected_operation": "deployment_or_restart",
                "question": "Review the exact PropertyQuarry edge connector identity and governed runtime target without starting or restarting it.",
            },
            {
                "id": "deployment_environment_intake",
                "status": str(intake_plan.get("status") or "blocked"),
                "protected_operation": "runtime_configuration_change",
                "question": str(intake_plan.get("next_action") or ""),
            },
            {
                "id": "dedicated_runtime",
                "status": (
                    "ready"
                    if proposal.get("dedicated_runtime_present") is True
                    else "blocked_missing_services"
                ),
                "protected_operation": "deployment_or_restart",
                "question": "Confirm the dedicated PropertyQuarry Compose project and OODA staging service are the intended runtime target.",
            },
            {
                "id": "release_worktree",
                "status": "ready" if release_clean else "blocked_dirty_worktree",
                "protected_operation": "deployment_or_restart",
                "question": "Prepare a reviewed clean release envelope without discarding or rewriting unrelated operator changes.",
            },
        ]
    return [
        {
            "id": "probe_target",
            "status": (
                "change_proposed"
                if proposal.get("probe_target_change_proposed") is True
                else "ready"
            ),
            "protected_operation": "runtime_configuration_change",
            "question": "Confirm the live probe should target the dedicated PropertyQuarry local origin recorded in this proposal.",
        },
        {
            "id": "host_admission",
            "status": (
                "requires_review"
                if proposal.get("probe_host_change_proposed") is True
                else "ready"
            ),
            "protected_operation": "runtime_configuration_change",
            "question": "Confirm the intended PropertyQuarry public host is admitted consistently at the edge and application boundary.",
        },
        {
            "id": "public_origin",
            "status": "ready",
            "protected_operation": "runtime_configuration_change",
            "question": "Confirm the release public-origin authority and host-admission authority describe the same public site.",
        },
        {
            "id": "dedicated_runtime",
            "status": (
                "ready"
                if proposal.get("dedicated_runtime_present") is True
                else "blocked_missing_services"
            ),
            "protected_operation": "deployment_or_restart",
            "question": "Confirm the dedicated PropertyQuarry Compose project and OODA staging service are the intended runtime target.",
        },
        {
            "id": "release_worktree",
            "status": "ready" if release_clean else "blocked_dirty_worktree",
            "protected_operation": "deployment_or_restart",
            "question": "Prepare a reviewed clean release envelope without discarding or rewriting unrelated operator changes.",
        },
    ]


def build_review_packet(
    *,
    status: Mapping[str, Any],
    snapshot_evidence: Mapping[str, Any],
    runtime_posture: Mapping[str, Any],
    release_posture: Mapping[str, Any],
    probe_posture: Mapping[str, Any],
    release_authority: Mapping[str, Any],
    deployment_environment_posture: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _now(now).isoformat()
    actions = list(status.get("actions") or [])
    action_reason = (
        str(actions[0].get("reason") or "").strip()
        if len(actions) == 1 and isinstance(actions[0], dict)
        else ""
    )
    if not (
        status.get("schema") == operator_status.SCHEMA
        and status.get("status") == "action_required"
        and status.get("action_required") is True
        and status.get("interrupt_operator") is True
        and status.get("automatic_execution_allowed") is False
        and status.get("provider_quota_consumption_allowed") is False
        and status.get("protected_operation_executed") is False
        and len(actions) == 1
        and isinstance(actions[0], dict)
        and actions[0].get("lane") == "gold_live_runtime"
        and action_reason in _REVIEW_ACTION_REASONS
        and actions[0].get("safe_next_action")
        == operator_status._GOLD_ACTIONS[action_reason]
        and actions[0].get("consent_required") is True
        and actions[0].get("automatic_execution_allowed") is False
        and actions[0].get("protected_operations") == _PROTECTED_OPERATIONS
        and actions[0].get("provider_quota_consumption_allowed") is False
    ):
        raise ValueError("operator_action_not_admissible_for_runtime_review")
    if runtime_posture.get("query_status") != "pass":
        raise ValueError("runtime_posture_not_admissible")
    if release_posture.get("changed_paths_recorded") is not False:
        raise ValueError("release_posture_not_admissible")
    action = dict(actions[0])
    if not (
        probe_posture.get("source_generated_at") == action.get("source_generated_at")
        and probe_posture.get("secret_values_recorded") is False
        and release_authority.get("secret_values_recorded") is False
    ):
        raise ValueError("runtime_configuration_evidence_not_admissible")
    environment_posture = (
        dict(deployment_environment_posture)
        if deployment_environment_posture is not None
        else inspect_deployment_environment()
        if action_reason == _TUNNEL_ACTION_REASON
        else {}
    )
    if action_reason == _TUNNEL_ACTION_REASON and not (
        _deployment_environment_posture_admissible(environment_posture)
    ):
        raise ValueError("deployment_environment_posture_not_admissible")
    proposal = _configuration_proposal(
        probe_posture=probe_posture,
        release_authority=release_authority,
        runtime_posture=runtime_posture,
        release_posture=release_posture,
        deployment_environment_posture=environment_posture,
    )
    review_items = _review_items(
        str(action.get("reason") or ""),
        release_clean=release_posture.get("worktree_clean") is True,
        proposal=proposal,
    )
    packet: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": observed_at,
        "updated_at": observed_at,
        "status": "review_ready",
        "blocking_reason": action_reason,
        "next_action": _review_packet_next_action(action_reason, proposal),
        "progress": {
            "approved_action_count": 1,
            "review_item_count": len(review_items),
            "runtime_container_count": int(runtime_posture.get("container_count") or 0),
            "release_worktree_clean": release_posture.get("worktree_clean") is True,
            "probe_target_change_proposed": proposal[
                "probe_target_change_proposed"
            ],
            "dedicated_runtime_present": proposal["dedicated_runtime_present"],
        },
        "source_evidence": dict(snapshot_evidence),
        "action": action,
        "action_sha256": _sha256(_canonical(action)),
        "probe_posture": dict(probe_posture),
        "release_authority": dict(release_authority),
        "configuration_proposal": proposal,
        "runtime_posture": dict(runtime_posture),
        "release_posture": dict(release_posture),
        "review_items": review_items,
        "consent_gate": _runtime_consent_gate(proposal),
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "secret_values_recorded": False,
        "changed_paths_recorded": False,
    }
    packet["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(packet)),
    }
    return packet


def verify_review_packet(
    packet: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    age_limit = float(max_age_seconds)
    generated_raw = str(packet.get("generated_at") or "").strip()
    try:
        generated_at = datetime.fromisoformat(generated_raw.replace("Z", "+00:00"))
    except ValueError:
        return _blocked("review_packet_timestamp_invalid", now=observed_now)
    if generated_at.tzinfo is None:
        return _blocked("review_packet_timestamp_invalid", now=observed_now)
    age_seconds = (observed_now - generated_at.astimezone(timezone.utc)).total_seconds()
    if (
        not math.isfinite(age_limit)
        or not 60.0 <= age_limit <= 86400.0
        or not math.isfinite(age_seconds)
        or age_seconds < -30.0
        or age_seconds > age_limit
    ):
        return _blocked("review_packet_not_fresh", now=observed_now)
    normalized = dict(packet)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
    ):
        return _blocked("review_packet_integrity_invalid", now=observed_now)
    action = packet.get("action")
    consent = packet.get("consent_gate")
    sources = packet.get("source_evidence")
    runtime = packet.get("runtime_posture")
    release = packet.get("release_posture")
    probe = packet.get("probe_posture")
    release_authority = packet.get("release_authority")
    proposal = packet.get("configuration_proposal")
    progress = packet.get("progress")
    action_reason = (
        str(action.get("reason") or "").strip()
        if isinstance(action, dict)
        else ""
    )
    expected_action = {
        "lane": "gold_live_runtime",
        "reason": action_reason,
        "source_generated_at": str(action.get("source_generated_at") or "")
        if isinstance(action, dict)
        else "",
        "safe_next_action": operator_status._GOLD_ACTIONS.get(action_reason, ""),
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "provider_quota_consumption_allowed": False,
    }
    action_source_fresh = (
        operator_status._parse_fresh_timestamp(
            expected_action["source_generated_at"],
            now=observed_now,
            max_age_seconds=24.0 * 3600.0,
        )
        is not None
    )
    runtime_rows = runtime.get("services") if isinstance(runtime, dict) else None
    runtime_contract = (
        isinstance(runtime, dict)
        and set(runtime)
        == {
            "source",
            "observed_at",
            "query_status",
            "project",
            "container_count",
            "services",
            "fingerprint_sha256",
        }
        and isinstance(runtime_rows, list)
        and isinstance(runtime.get("container_count"), int)
        and not isinstance(runtime.get("container_count"), bool)
        and runtime.get("container_count") == len(runtime_rows)
        and all(
            isinstance(row, dict)
            and set(row) == {"service", "state", "health"}
            and _SERVICE.fullmatch(str(row.get("service") or ""))
            and row.get("state")
            in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
            and row.get("health") in _HEALTH
            for row in runtime_rows
        )
        and runtime.get("fingerprint_sha256")
        == _sha256(
            _canonical(
                {
                    "project": runtime.get("project"),
                    "services": runtime_rows,
                }
            )
        )
        and operator_status._parse_fresh_timestamp(
            runtime.get("observed_at"),
            now=observed_now,
            max_age_seconds=age_limit,
        )
        is not None
    )
    release_contract = (
        isinstance(release, dict)
        and set(release)
        == {
            "source",
            "observed_at",
            "head_sha",
            "worktree_clean",
            "changed_path_count",
            "worktree_fingerprint_sha256",
            "changed_paths_recorded",
        }
        and re.fullmatch(r"[0-9a-f]{40}", str(release.get("head_sha") or ""))
        and isinstance(release.get("worktree_clean"), bool)
        and isinstance(release.get("changed_path_count"), int)
        and not isinstance(release.get("changed_path_count"), bool)
        and release.get("changed_path_count") >= 0
        and release.get("worktree_clean") is (release.get("changed_path_count") == 0)
        and operator_status._parse_fresh_timestamp(
            release.get("observed_at"),
            now=observed_now,
            max_age_seconds=age_limit,
        )
        is not None
    )
    probe_fingerprint = (
        _sha256(_canonical(_probe_fingerprint_payload(probe)))
        if isinstance(probe, dict)
        else ""
    )
    try:
        normalized_probe_origin = (
            _public_origin(probe.get("base_origin"))[0]
            if isinstance(probe, dict)
            and action_reason == _TUNNEL_ACTION_REASON
            else _local_origin(probe.get("base_origin"))
            if isinstance(probe, dict)
            else ""
        )
        normalized_release_origin, normalized_release_host = (
            _public_origin(release_authority.get("release_public_origin"))
            if isinstance(release_authority, dict)
            else ("", "")
        )
        recovery_preview = (
            proposal.get("recovery_preview")
            if isinstance(proposal, Mapping)
            else None
        )
        environment_posture = (
            recovery_preview.get("deployment_environment")
            if action_reason == _TUNNEL_ACTION_REASON
            and isinstance(recovery_preview, Mapping)
            else {}
        )
        if action_reason == _TUNNEL_ACTION_REASON and not (
            isinstance(environment_posture, Mapping)
            and _deployment_environment_posture_admissible(
                environment_posture
            )
        ):
            raise ValueError("deployment_environment_posture_not_admissible")
        expected_proposal = (
            _configuration_proposal(
                probe_posture=probe,
                release_authority=release_authority,
                runtime_posture=runtime,
                release_posture=release,
                deployment_environment_posture=environment_posture,
            )
            if isinstance(probe, dict)
            and isinstance(release_authority, dict)
            and isinstance(runtime, dict)
            else {}
        )
        expected_review_items = (
            _review_items(
                action_reason,
                release_clean=release.get("worktree_clean") is True,
                proposal=expected_proposal,
            )
            if isinstance(release, dict) and expected_proposal
            else []
        )
        expected_consent = (
            _runtime_consent_gate(expected_proposal)
            if expected_proposal
            else {}
        )
    except (TypeError, ValueError):
        return _blocked("review_packet_contract_not_admissible", now=observed_now)
    probe_common_contract = (
        isinstance(probe, dict)
        and _SHA256.fullmatch(str(probe.get("source_sha256") or ""))
        and isinstance(probe.get("source_bytes"), int)
        and not isinstance(probe.get("source_bytes"), bool)
        and 0 < probe.get("source_bytes") <= MAX_LIVE_MOBILE_RECEIPT_BYTES
        and probe.get("source_generated_at") == expected_action["source_generated_at"]
        and action_source_fresh
        and probe.get("status") == "blocked"
        and probe.get("base_origin") == normalized_probe_origin
        and probe.get("secret_values_recorded") is False
        and probe.get("fingerprint_sha256") == probe_fingerprint
    )
    legacy_probe_contract = bool(
        probe_common_contract
        and action_reason == _HOST_ADMISSION_ACTION_REASON
        and set(probe)
        == {
            "source",
            "source_sha256",
            "source_bytes",
            "source_generated_at",
            "status",
            "base_origin",
            "host_header",
            "failure_code",
            "secret_values_recorded",
            "fingerprint_sha256",
        }
        and probe.get("source") == "local_private_live_mobile_receipt"
        and _PUBLIC_HOST.fullmatch(str(probe.get("host_header") or ""))
        and probe.get("failure_code") == "http_421_misdirected_request"
    )
    edge_probe_contract = bool(
        probe_common_contract
        and action_reason == _TUNNEL_ACTION_REASON
        and set(probe)
        == {
            "source",
            "source_sha256",
            "source_bytes",
            "source_generated_at",
            "status",
            "base_origin",
            "host_header",
            "failure_code",
            "edge_provider",
            "edge_error_code",
            "http_status",
            "failure_reason",
            "secret_values_recorded",
            "fingerprint_sha256",
        }
        and probe.get("source")
        in {"public_https_live_mobile_receipt", "public_https_origin_observation"}
        and _PUBLIC_HOST.fullmatch(str(probe.get("host_header") or ""))
        and probe.get("host_header") == normalized_release_host
        and probe.get("failure_code") == "cloudflare_1033_tunnel_unavailable"
        and {
            "provider": probe.get("edge_provider"),
            "code": probe.get("edge_error_code"),
            "reason": "cloudflare_tunnel_unavailable",
            "http_status": probe.get("http_status"),
        }
        == _CLOUDFLARE_TUNNEL_FAILURE
        and probe.get("failure_reason") == "tunnel_unavailable"
    )
    probe_contract = legacy_probe_contract or edge_probe_contract
    release_authority_contract = (
        isinstance(release_authority, dict)
        and set(release_authority)
        == {
            "source",
            "source_sha256",
            "source_bytes",
            "release_repository",
            "release_public_origin",
            "release_public_host",
            "release_deployment_id",
            "release_generated_at",
            "secret_values_recorded",
        }
        and release_authority.get("source") == "repository_release_manifest"
        and _SHA256.fullmatch(str(release_authority.get("source_sha256") or ""))
        and isinstance(release_authority.get("source_bytes"), int)
        and not isinstance(release_authority.get("source_bytes"), bool)
        and 0 < release_authority.get("source_bytes") <= MAX_RELEASE_MANIFEST_BYTES
        and release_authority.get("release_repository") == local_deployment.REPOSITORY
        and release_authority.get("release_public_origin") == normalized_release_origin
        and release_authority.get("release_public_host") == normalized_release_host
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}",
            str(release_authority.get("release_deployment_id") or ""),
        )
        and operator_status._parse_fresh_timestamp(
            release_authority.get("release_generated_at"),
            now=observed_now,
            max_age_seconds=365.0 * 24.0 * 3600.0,
        )
        is not None
        and release_authority.get("secret_values_recorded") is False
    )
    expected_root_keys = {
        "schema",
        "generated_at",
        "updated_at",
        "status",
        "blocking_reason",
        "next_action",
        "progress",
        "source_evidence",
        "action",
        "action_sha256",
        "probe_posture",
        "release_authority",
        "configuration_proposal",
        "runtime_posture",
        "release_posture",
        "review_items",
        "consent_gate",
        "automatic_execution_allowed",
        "execution_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "secret_values_recorded",
        "changed_paths_recorded",
        "integrity",
    }
    if not (
        set(packet) == expected_root_keys
        and packet.get("schema") == SCHEMA
        and packet.get("updated_at") == packet.get("generated_at")
        and packet.get("status") == "review_ready"
        and action_reason in _REVIEW_ACTION_REASONS
        and packet.get("blocking_reason") == action_reason
        and packet.get("next_action")
        == _review_packet_next_action(action_reason, expected_proposal)
        and progress
        == {
            "approved_action_count": 1,
            "review_item_count": len(expected_review_items),
            "runtime_container_count": runtime.get("container_count")
            if isinstance(runtime, dict)
            else -1,
            "release_worktree_clean": release.get("worktree_clean") is True
            if isinstance(release, dict)
            else False,
            "probe_target_change_proposed": expected_proposal.get(
                "probe_target_change_proposed"
            ),
            "dedicated_runtime_present": expected_proposal.get(
                "dedicated_runtime_present"
            ),
        }
        and isinstance(action, dict)
        and action == expected_action
        and action_source_fresh
        and packet.get("action_sha256") == _sha256(_canonical(action))
        and probe_contract
        and release_authority_contract
        and proposal == expected_proposal
        and isinstance(consent, dict)
        and consent == expected_consent
        and packet.get("automatic_execution_allowed") is False
        and packet.get("execution_authorized") is False
        and packet.get("protected_operation_executed") is False
        and packet.get("provider_quota_consumption_allowed") is False
        and packet.get("delivery_authorized") is False
        and packet.get("secret_values_recorded") is False
        and packet.get("changed_paths_recorded") is False
        and isinstance(sources, dict)
        and set(sources) == {"cycle_receipt", "approval_manifest"}
        and all(
            isinstance(row, dict)
            and set(row) == {"sha256", "bytes", "generated_at"}
            and _SHA256.fullmatch(str(row.get("sha256") or ""))
            and isinstance(row.get("bytes"), int)
            and row.get("bytes") > 0
            and operator_status._parse_fresh_timestamp(
                row.get("generated_at"),
                now=observed_now,
                max_age_seconds=age_limit,
            )
            is not None
            for row in sources.values()
        )
        and runtime_contract
        and runtime.get("source") == "local_docker"
        and runtime.get("query_status") == "pass"
        and runtime.get("project") == local_deployment.DEFAULT_COMPOSE_PROJECT
        and _SHA256.fullmatch(str(runtime.get("fingerprint_sha256") or ""))
        and release_contract
        and release.get("source") == "local_git"
        and release.get("changed_paths_recorded") is False
        and _SHA256.fullmatch(str(release.get("worktree_fingerprint_sha256") or ""))
        and packet.get("review_items") == expected_review_items
    ):
        return _blocked("review_packet_contract_not_admissible", now=observed_now)
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": action_reason,
        "next_action": str(packet.get("next_action") or ""),
        "progress": {
            "current_evidence_verified": False,
            "review_item_count": len(expected_review_items),
            "runtime_container_count": int(runtime.get("container_count") or 0),
            "release_worktree_clean": release.get("worktree_clean") is True,
            "probe_target_change_proposed": expected_proposal[
                "probe_target_change_proposed"
            ],
            "dedicated_runtime_present": expected_proposal[
                "dedicated_runtime_present"
            ],
        },
        "configuration_proposal": {
            "incident": dict(expected_proposal["incident"]),
            "recovery_preview": dict(expected_proposal["recovery_preview"]),
            "current_probe_origin": expected_proposal["current_probe_origin"],
            "proposed_probe_origin": expected_proposal["proposed_probe_origin"],
            "current_probe_host": expected_proposal["current_probe_host"],
            "proposed_probe_host": expected_proposal["proposed_probe_host"],
            "release_public_origin": expected_proposal["release_public_origin"],
            "compose_project": expected_proposal["compose_project"],
            "missing_service_count": len(expected_proposal["missing_services"]),
            "authorization_required": True,
            "execution_authorized": False,
            "protected_operations": list(_PROTECTED_OPERATIONS),
        },
        "consent_request": dict(expected_consent),
        "packet_sha256": _sha256(_canonical(packet)),
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def operator_presentation_context(
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only stable, verified runtime decision scope for interruption novelty."""

    proposal = verification.get("configuration_proposal")
    progress = verification.get("progress")
    if not (
        verification.get("schema") == VERIFY_SCHEMA
        and verification.get("status") == "verified"
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and verification.get("execution_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
        and isinstance(proposal, Mapping)
        and proposal.get("authorization_required") is True
        and proposal.get("execution_authorized") is False
        and proposal.get("protected_operations") == _PROTECTED_OPERATIONS
    ):
        raise ValueError("runtime_review_not_admissible_for_presentation")
    incident = proposal.get("incident")
    if verification.get("blocking_reason") == _TUNNEL_ACTION_REASON:
        if incident != {
            "kind": "edge_connector_unavailable",
            "provider": "cloudflare",
            "code": "1033",
            "http_status": 530,
            "reason": "tunnel_unavailable",
            "source": "public_https_probe",
        }:
            raise ValueError("runtime_review_not_admissible_for_presentation")
        recovery_preview = proposal.get("recovery_preview")
        expected_services = sorted(local_deployment.SERVICE_CONTRACT)
        service_targets = (
            list(recovery_preview.get("service_targets") or [])
            if isinstance(recovery_preview, Mapping)
            else []
        )
        source_binding = (
            recovery_preview.get("source_binding")
            if isinstance(recovery_preview, Mapping)
            else None
        )
        deployment_environment = (
            recovery_preview.get("deployment_environment")
            if isinstance(recovery_preview, Mapping)
            else None
        )
        environment_intake = (
            deployment_environment.get("intake_plan")
            if isinstance(deployment_environment, Mapping)
            else None
        )
        expected_consent = _runtime_consent_gate(proposal)
        recovery_blockers = (
            list(recovery_preview.get("blocking_reasons") or [])
            if isinstance(recovery_preview, Mapping)
            else []
        )
        all_services_absent = bool(service_targets) and all(
            isinstance(row, Mapping) and row.get("observed_state") == "absent"
            for row in service_targets
        )
        if not (
            isinstance(recovery_preview, Mapping)
            and recovery_preview.get("operation") == "deployment_or_restart"
            and recovery_preview.get("compose_project")
            == local_deployment.DEFAULT_COMPOSE_PROJECT
            and recovery_preview.get("compose_files")
            == [path.as_posix() for path in local_deployment.COMPOSE_FILES]
            and recovery_preview.get("connector_service")
            == "propertyquarry-cloudflared"
            and recovery_preview.get("application_service")
            == "propertyquarry-api"
            and all(isinstance(row, Mapping) for row in service_targets)
            and [str(row.get("service") or "") for row in service_targets]
            == expected_services
            and all(
                isinstance(row, Mapping)
                and row.get("required_state")
                == local_deployment.SERVICE_CONTRACT[str(row.get("service") or "")]
                and row.get("observed_state") in {"absent", "present"}
                for row in service_targets
            )
            and isinstance(source_binding, Mapping)
            and set(source_binding)
            == {
                "runtime_fingerprint_sha256",
                "release_head_sha",
                "release_worktree_fingerprint_sha256",
            }
            and _SHA256.fullmatch(
                str(source_binding.get("runtime_fingerprint_sha256") or "")
            )
            and re.fullmatch(
                r"[0-9a-f]{40}",
                str(source_binding.get("release_head_sha") or ""),
            )
            and _SHA256.fullmatch(
                str(
                    source_binding.get("release_worktree_fingerprint_sha256")
                    or ""
                )
            )
            and isinstance(deployment_environment, Mapping)
            and _deployment_environment_posture_admissible(
                deployment_environment
            )
            and isinstance(environment_intake, Mapping)
            and verification.get("consent_request") == expected_consent
            and recovery_preview.get("command") == _compose_command("start")
            and recovery_preview.get("rollback_command")
            == (
                _compose_command("rollback_to_absent")
                if all_services_absent
                else []
            )
            and recovery_preview.get("pre_state")
            == (
                "all_required_services_absent"
                if all_services_absent
                else "required_services_present_or_partial"
            )
            and recovery_preview.get("reversible_to_observed_state")
            is all_services_absent
            and recovery_blockers
            and "deployment_environment_authority_not_verified"
            in recovery_blockers
            and (
                "deployment_environment_not_ready" in recovery_blockers
            )
            is (
                deployment_environment.get("configuration_complete")
                is not True
            )
            and recovery_preview.get("deployment_environment_values_recorded")
            is False
            and recovery_preview.get("deployment_environment_authority_verified")
            is False
            and recovery_preview.get("eligible_for_authorization") is False
            and recovery_preview.get("preview_only") is True
            and recovery_preview.get("command_recorded") is True
            and recovery_preview.get("rollback_command_recorded")
            is all_services_absent
            and recovery_preview.get("authorization_recorded") is False
            and recovery_preview.get("execution_authorized") is False
            and recovery_preview.get("deployment_or_restart_performed") is False
        ):
            raise ValueError("runtime_review_not_admissible_for_presentation")
        current_origin, current_host = _public_origin(
            proposal.get("current_probe_origin")
        )
        proposed_origin, proposed_host = _public_origin(
            proposal.get("proposed_probe_origin")
        )
        release_origin, release_host = _public_origin(
            proposal.get("release_public_origin")
        )
        compose_project = str(proposal.get("compose_project") or "").strip()
        if not (
            current_origin == proposed_origin == release_origin
            and current_host == proposed_host == release_host
            and proposal.get("current_probe_host") == current_host
            and proposal.get("proposed_probe_host") == proposed_host
            and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", compose_project)
        ):
            raise ValueError("runtime_review_not_admissible_for_presentation")
        return {
            "schema": "propertyquarry.ooda_operator_presentation_context.v1",
            "lane": "gold_live_runtime",
            "scope": {
                "operation": "deployment_or_restart_review",
                "change_id": "propertyquarry_edge_connector_recovery",
                "edge_provider": "cloudflare",
                "edge_error_code": "1033",
                "http_status": 530,
                "failure_reason": "tunnel_unavailable",
                "recovery_preview_sha256": _sha256(
                    _canonical(dict(recovery_preview))
                ),
                "deployment_environment_intake": {
                    "status": environment_intake["status"],
                    "blocking_reason": environment_intake[
                        "blocking_reason"
                    ],
                    "next_action": environment_intake["next_action"],
                    "operator_external_input_required": environment_intake[
                        "operator_external_input_required"
                    ],
                    "operator_external_input_keys": list(
                        environment_intake["operator_external_input_keys"]
                    ),
                    "operator_external_input_key_count": environment_intake[
                        "operator_external_input_key_count"
                    ],
                    "operator_import_path": environment_intake[
                        "operator_import"
                    ]["path"],
                    "operator_import_status": environment_intake[
                        "operator_import"
                    ]["status"],
                    "operator_import_file_status": environment_intake[
                        "operator_import"
                    ]["file_status"],
                    "operator_import_ready_for_review": environment_intake[
                        "operator_import"
                    ]["import_ready_for_review"],
                    "operator_merge_review_required": environment_intake[
                        "operator_merge_review_required"
                    ],
                    "operator_merge_review_keys": list(
                        environment_intake["operator_merge_review_keys"]
                    ),
                    "operator_merge_review_key_count": environment_intake[
                        "operator_merge_review_key_count"
                    ],
                    "locally_stageable_key_count": environment_intake[
                        "locally_stageable_key_count"
                    ],
                    "locally_staged_key_count": environment_intake[
                        "locally_staged_key_count"
                    ],
                    "locally_pending_key_count": environment_intake[
                        "locally_pending_key_count"
                    ],
                    "local_merge_review_required": environment_intake[
                        "local_merge_review_required"
                    ],
                    "local_merge_review_keys": list(
                        environment_intake["local_merge_review_keys"]
                    ),
                    "local_merge_review_key_count": environment_intake[
                        "local_merge_review_key_count"
                    ],
                    "local_runtime_candidate_path": environment_intake[
                        "local_runtime_binding_candidate"
                    ]["path"],
                    "local_runtime_candidate_status": environment_intake[
                        "local_runtime_binding_candidate"
                    ]["status"],
                    "local_runtime_candidate_ready_for_merge_review": environment_intake[
                        "local_runtime_binding_candidate"
                    ]["candidate_ready_for_merge_review"],
                    "local_runtime_candidate_values_hashed": False,
                    "configuration_merge_authorized": False,
                    "configuration_merged": False,
                    "configuration_apply_authorized": False,
                    "deployment_or_restart_authorized": False,
                    "environment_values_recorded": False,
                    "secret_values_recorded": False,
                },
                **(
                    {"consent_request": dict(expected_consent)}
                    if expected_consent.get("request_id")
                    else {}
                ),
                "release_public_origin": release_origin,
                "compose_project": compose_project,
                "protected_operations": list(_PROTECTED_OPERATIONS),
            },
        }
    if incident != {
        "kind": "host_admission_rejected",
        "provider": "application_edge",
        "code": "http_421",
        "http_status": 421,
        "reason": "misdirected_request",
        "source": "local_host_header_probe",
    }:
        raise ValueError("runtime_review_not_admissible_for_presentation")
    current_origin = _local_origin(proposal.get("current_probe_origin"))
    proposed_origin = _local_origin(proposal.get("proposed_probe_origin"))
    current_host = str(proposal.get("current_probe_host") or "").strip().lower()
    proposed_host = str(proposal.get("proposed_probe_host") or "").strip().lower()
    release_origin, release_host = _public_origin(
        proposal.get("release_public_origin")
    )
    compose_project = str(proposal.get("compose_project") or "").strip()
    if not (
        _PUBLIC_HOST.fullmatch(current_host)
        and _PUBLIC_HOST.fullmatch(proposed_host)
        and proposed_host == release_host
        and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", compose_project)
    ):
        raise ValueError("runtime_review_not_admissible_for_presentation")
    return {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "propertyquarry_local_probe_origin",
            "current_probe_origin": current_origin,
            "proposed_probe_origin": proposed_origin,
            "current_probe_host": current_host,
            "proposed_probe_host": proposed_host,
            "release_public_origin": release_origin,
            "compose_project": compose_project,
            "protected_operations": list(_PROTECTED_OPERATIONS),
        },
    }


def materialize_current_review_packet(
    *,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = DEFAULT_DEPLOYMENT_ENV_PATH,
    write_path: Path = DEFAULT_PACKET_PATH,
    project: str = local_deployment.DEFAULT_COMPOSE_PROJECT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    status = operator_status.load_operator_status(
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        now=observed_now,
    )
    actions = list(status.get("actions") or [])
    source_generated_at = (
        str(actions[0].get("source_generated_at") or "").strip()
        if len(actions) == 1 and isinstance(actions[0], dict)
        else ""
    )
    action_reason = (
        str(actions[0].get("reason") or "").strip()
        if len(actions) == 1 and isinstance(actions[0], dict)
        else ""
    )
    packet = build_review_packet(
        status=status,
        snapshot_evidence=_snapshot_evidence(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
        ),
        runtime_posture=_observe_runtime(project=project, now=observed_now),
        release_posture=_release_posture(now=observed_now),
        probe_posture=_live_mobile_probe_posture(
            receipt_path=live_mobile_receipt_path,
            expected_source_generated_at=source_generated_at,
            now=observed_now,
        ),
        release_authority=_release_authority_posture(
            manifest_path=release_manifest_path,
            now=observed_now,
        ),
        deployment_environment_posture=(
            inspect_deployment_environment(env_path=deployment_env_path)
            if action_reason == _TUNNEL_ACTION_REASON
            else None
        ),
        now=observed_now,
    )
    atomic_write_bytes(
        Path(write_path).absolute(),
        _canonical(packet),
        overwrite=True,
    )
    return packet


def verify_current_review_packet(
    *,
    packet_path: Path = DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = DEFAULT_DEPLOYMENT_ENV_PATH,
    project: str = local_deployment.DEFAULT_COMPOSE_PROJECT,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        metadata = Path(packet_path).lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("packet_file_not_admissible")
        packet, _raw, _digest = load_strict_json_object_snapshot(
            Path(packet_path),
            field="runtime_review_packet",
            maximum_bytes=MAX_PACKET_BYTES,
        )
    except Exception:
        return _blocked("review_packet_file_not_admissible", now=observed_now)
    verification = verify_review_packet(
        packet,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if verification["status"] != "verified":
        return verification
    try:
        current_status = operator_status.load_operator_status(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        current_sources = _snapshot_evidence(
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
        )
        current_runtime = _observe_runtime(project=project, now=observed_now)
        current_release = _release_posture(now=observed_now)
        current_actions = list(current_status.get("actions") or [])
        current_source_generated_at = (
            str(current_actions[0].get("source_generated_at") or "").strip()
            if len(current_actions) == 1 and isinstance(current_actions[0], dict)
            else ""
        )
        current_probe = _live_mobile_probe_posture(
            receipt_path=live_mobile_receipt_path,
            expected_source_generated_at=current_source_generated_at,
            now=observed_now,
        )
        current_release_authority = _release_authority_posture(
            manifest_path=release_manifest_path,
            now=observed_now,
        )
        current_action_reason = (
            str(current_actions[0].get("reason") or "").strip()
            if len(current_actions) == 1 and isinstance(current_actions[0], dict)
            else ""
        )
        current_deployment_environment = (
            inspect_deployment_environment(env_path=deployment_env_path)
            if current_action_reason == _TUNNEL_ACTION_REASON
            else {}
        )
    except Exception:
        return _blocked("current_review_evidence_unavailable", now=observed_now)
    actions = list(current_status.get("actions") or [])
    if not (
        current_status.get("status") == "action_required"
        and len(actions) == 1
        and packet.get("action_sha256") == _sha256(_canonical(actions[0]))
        and packet.get("source_evidence") == current_sources
        and packet.get("probe_posture") == current_probe
        and packet.get("release_authority") == current_release_authority
        and packet.get("runtime_posture", {}).get("fingerprint_sha256")
        == current_runtime.get("fingerprint_sha256")
        and packet.get("release_posture", {}).get("head_sha")
        == current_release.get("head_sha")
        and packet.get("release_posture", {}).get("worktree_clean")
        == current_release.get("worktree_clean")
        and packet.get("release_posture", {}).get("changed_path_count")
        == current_release.get("changed_path_count")
        and packet.get("release_posture", {}).get("worktree_fingerprint_sha256")
        == current_release.get("worktree_fingerprint_sha256")
        and (
            packet.get("configuration_proposal", {})
            .get("recovery_preview", {})
            .get("deployment_environment", {})
            == current_deployment_environment
        )
    ):
        return _blocked("review_packet_current_binding_mismatch", now=observed_now)
    verification["progress"]["current_evidence_verified"] = True
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage or verify a consent-gated review, or record a read-only runtime observation."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-current", action="store_true")
    mode.add_argument("--observe-runtime", action="store_true")
    mode.add_argument("--verify-runtime-observation", action="store_true")
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET_PATH)
    parser.add_argument(
        "--verification-write",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--runtime-observation",
        type=Path,
        default=DEFAULT_RUNTIME_OBSERVATION_PATH,
    )
    parser.add_argument(
        "--runtime-observation-verification-write",
        type=Path,
        default=DEFAULT_RUNTIME_OBSERVATION_VERIFICATION_PATH,
    )
    parser.add_argument("--cycle-receipt", type=Path, default=DEFAULT_CYCLE_RECEIPT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL_DIR)
    parser.add_argument(
        "--live-mobile-receipt",
        type=Path,
        default=DEFAULT_LIVE_MOBILE_RECEIPT,
    )
    parser.add_argument(
        "--release-manifest",
        type=Path,
        default=DEFAULT_RELEASE_MANIFEST,
    )
    parser.add_argument(
        "--deployment-env",
        type=Path,
        default=DEFAULT_DEPLOYMENT_ENV_PATH,
    )
    parser.add_argument("--project", default=local_deployment.DEFAULT_COMPOSE_PROJECT)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    runtime_observation_mode = bool(
        args.observe_runtime or args.verify_runtime_observation
    )
    verification_write_path = (
        args.runtime_observation_verification_write
        if runtime_observation_mode
        else args.verification_write
    )
    try:
        if args.observe_runtime:
            observation = materialize_current_runtime_observation(
                write_path=args.runtime_observation,
                project=args.project,
            )
            result = verify_current_runtime_observation(
                observation_path=args.runtime_observation,
                project=args.project,
                max_age_seconds=args.max_age_seconds,
            )
            result["observation_generated_at"] = str(
                observation.get("generated_at") or ""
            )
        elif args.verify_runtime_observation:
            result = verify_current_runtime_observation(
                observation_path=args.runtime_observation,
                project=args.project,
                max_age_seconds=args.max_age_seconds,
            )
        elif args.verify_current:
            result = verify_current_review_packet(
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                project=args.project,
                max_age_seconds=args.max_age_seconds,
            )
        else:
            packet = materialize_current_review_packet(
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                write_path=args.packet,
                project=args.project,
            )
            result = verify_current_review_packet(
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                project=args.project,
                max_age_seconds=args.max_age_seconds,
            )
            result["packet_path"] = str(args.packet)
            result["packet_generated_at"] = str(packet.get("generated_at") or "")
    except Exception:
        result = (
            _runtime_observation_blocked(
                "runtime_observation_materialization_failed",
                now=None,
            )
            if runtime_observation_mode
            else _blocked("runtime_review_materialization_failed", now=None)
        )
    if runtime_observation_mode:
        result["observation_path"] = str(args.runtime_observation)
        result["verification_path"] = str(verification_write_path)
    else:
        result["packet_path"] = str(args.packet)
    result["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(verification_write_path).absolute(),
            _canonical(result),
            overwrite=True,
        )
    except Exception:
        result["verification_receipt_persisted"] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
