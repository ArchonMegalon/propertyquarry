#!/usr/bin/env python3
"""Settle governed scheduler activation history against fresh runtime truth."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_scheduler_activation_execution as execution
from scripts import propertyquarry_ooda_scheduler_continuity as continuity
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


RELEASE_OBSERVATION_SCHEMA = (
    "propertyquarry.ooda_scheduler_release_identity_observation.v1"
)
SCHEMA = "propertyquarry.ooda_scheduler_activation_settlement.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_scheduler_activation_settlement_verification.v1"
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-settlement.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-settlement-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = 300.0
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{12,64}\Z")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return execution._canonical(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    parsed = execution._timestamp(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    if not math.isfinite(age) or age < 0 or age > max_age_seconds:
        return None
    return parsed.isoformat()


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "settlement_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "refresh scheduler continuity and release identity, then reverify "
            "the immutable activation execution ledger"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "actions": [],
        "authorization_recorded": False,
        "authorization_consumed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "historical_deployment_attempted": False,
        "protected_operation_executed": False,
        "historical_protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": False},
    }


def _release_observation(
    *,
    status: str,
    observed_at: datetime,
    project: str,
    container_count: int,
    blocking_reason: str = "",
    container_id: str = "",
    container_state: str = "",
    container_health: str = "",
    image_digest: str = "",
    release_commit_sha: str = "",
    release_image_digest: str = "",
) -> dict[str, Any]:
    return {
        "schema": RELEASE_OBSERVATION_SCHEMA,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "blocking_reason": blocking_reason,
        "source": "local_docker",
        "compose_project": project,
        "scheduler_service": "propertyquarry-scheduler",
        "container_count": container_count,
        "container_id": container_id,
        "container_state": container_state,
        "container_health": container_health,
        "image_digest": image_digest,
        "release_commit_sha": release_commit_sha,
        "release_image_digest": release_image_digest,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }


def observe_current_scheduler_release_identity(
    *,
    project: str = "property",
    now: datetime | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    observed_at = _now(now)
    if _PROJECT.fullmatch(str(project or "")) is None:
        raise ValueError("scheduler_activation_settlement_project_not_admissible")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 30
    ):
        raise ValueError("scheduler_activation_settlement_timeout_not_admissible")
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "DOCKER_HOST": os.environ.get(
            "DOCKER_HOST", "unix:///var/run/docker.sock"
        ),
    }
    try:
        query = subprocess.run(
            [
                "/usr/bin/docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=propertyquarry-scheduler",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=0,
            blocking_reason="scheduler_release_identity_query_failed",
        )
    identifiers = [
        row.strip() for row in query.stdout.decode(errors="replace").splitlines()
        if row.strip()
    ]
    if (
        query.returncode != 0
        or query.stderr
        or len(query.stdout) > 64 * 1024
        or any(_CONTAINER.fullmatch(value) is None for value in identifiers)
    ):
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=0,
            blocking_reason="scheduler_release_identity_query_not_admissible",
        )
    if not identifiers:
        return _release_observation(
            status="absent",
            observed_at=observed_at,
            project=project,
            container_count=0,
        )
    if len(identifiers) != 1:
        return _release_observation(
            status="ambiguous",
            observed_at=observed_at,
            project=project,
            container_count=len(identifiers),
            blocking_reason="scheduler_release_identity_cardinality_ambiguous",
        )
    container_id = identifiers[0]
    try:
        inspected = subprocess.run(
            ["/usr/bin/docker", "inspect", container_id],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=1,
            blocking_reason="scheduler_release_identity_inspect_failed",
        )
    if (
        inspected.returncode != 0
        or inspected.stderr
        or len(inspected.stdout) > MAX_CAPTURE_BYTES
    ):
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=1,
            blocking_reason="scheduler_release_identity_inspect_not_admissible",
        )
    try:
        payload = json.loads(inspected.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise ValueError
        item = payload[0]
        if not isinstance(item, Mapping):
            raise ValueError
        config = item.get("Config")
        state = item.get("State")
        if not isinstance(config, Mapping) or not isinstance(state, Mapping):
            raise ValueError
        labels = config.get("Labels")
        if not isinstance(labels, Mapping):
            raise ValueError
        environment_rows = config.get("Env")
        if not isinstance(environment_rows, list):
            raise ValueError
        safe_environment: dict[str, str] = {}
        for row in environment_rows:
            if not isinstance(row, str) or "=" not in row:
                continue
            key, value = row.split("=", 1)
            if key in {
                "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
                "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
            }:
                safe_environment[key] = value
        health_row = state.get("Health")
        health = (
            str(health_row.get("Status") or "none")
            if isinstance(health_row, Mapping)
            else "none"
        )
        observed_container_id = str(item.get("Id") or "")
        image_digest = str(item.get("Image") or "")
        container_state = str(state.get("Status") or "")
        release_commit = safe_environment.get(
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA", ""
        )
        release_image = safe_environment.get(
            "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", ""
        )
        if not (
            _CONTAINER.fullmatch(observed_container_id)
            and observed_container_id.startswith(container_id)
            and labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.service")
            == "propertyquarry-scheduler"
            and container_state
            in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }
            and health in {"healthy", "starting", "unhealthy", "none"}
            and _IMAGE.fullmatch(image_digest)
            and _COMMIT.fullmatch(release_commit)
            and _IMAGE.fullmatch(release_image)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=1,
            blocking_reason="scheduler_release_identity_contract_not_admissible",
        )
    if container_state != "running" or health != "healthy":
        return _release_observation(
            status="blocked",
            observed_at=observed_at,
            project=project,
            container_count=1,
            blocking_reason="scheduler_release_identity_not_healthy",
        )
    return _release_observation(
        status="observed",
        observed_at=observed_at,
        project=project,
        container_count=1,
        container_id=observed_container_id,
        container_state=container_state,
        container_health=health,
        image_digest=image_digest,
        release_commit_sha=release_commit,
        release_image_digest=release_image,
    )


def _project_release_observation(
    value: Mapping[str, Any],
    *,
    now: datetime,
    project: str,
    max_age_seconds: float,
) -> dict[str, Any]:
    observed_at = _fresh_timestamp(
        value.get("observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    status_value = str(value.get("status") or "")
    count = value.get("container_count")
    expected = _release_observation(
        status=status_value,
        observed_at=execution._timestamp(observed_at) or now,
        project=project,
        container_count=int(count) if isinstance(count, int) else -1,
        blocking_reason=str(value.get("blocking_reason") or ""),
        container_id=str(value.get("container_id") or ""),
        container_state=str(value.get("container_state") or ""),
        container_health=str(value.get("container_health") or ""),
        image_digest=str(value.get("image_digest") or ""),
        release_commit_sha=str(value.get("release_commit_sha") or ""),
        release_image_digest=str(value.get("release_image_digest") or ""),
    )
    if not (
        observed_at is not None
        and status_value in {"observed", "absent", "ambiguous", "blocked"}
        and isinstance(count, int)
        and not isinstance(count, bool)
        and 0 <= count <= 256
        and dict(value) == expected
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and value.get("delivery_attempted") is False
        and value.get("sent") is False
        and value.get("secret_values_recorded") is False
        and (
            status_value != "observed"
            or (
                count == 1
                and _CONTAINER.fullmatch(str(value.get("container_id") or ""))
                and value.get("container_state") == "running"
                and value.get("container_health") == "healthy"
                and _IMAGE.fullmatch(str(value.get("image_digest") or ""))
                and _COMMIT.fullmatch(
                    str(value.get("release_commit_sha") or "")
                )
                and _IMAGE.fullmatch(
                    str(value.get("release_image_digest") or "")
                )
            )
        )
        and (status_value != "absent" or count == 0)
        and (status_value != "ambiguous" or count > 1)
        and (
            status_value == "observed"
            or not any(
                str(value.get(key) or "")
                for key in (
                    "container_id",
                    "container_state",
                    "container_health",
                    "image_digest",
                    "release_commit_sha",
                    "release_image_digest",
                )
            )
        )
    ):
        raise ValueError("scheduler_release_identity_observation_not_admissible")
    return {**dict(value), "observed_at": observed_at}


def _project_continuity(
    value: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    progress = value.get("progress")
    source = value.get("source_evidence")
    if not isinstance(progress, Mapping) or not isinstance(source, Mapping):
        raise ValueError("scheduler_activation_settlement_continuity_not_admissible")
    runtime = source.get("runtime_observation")
    witness = source.get("scheduler_iteration_witness")
    if not isinstance(runtime, Mapping) or not isinstance(witness, Mapping):
        raise ValueError("scheduler_activation_settlement_continuity_not_admissible")
    updated_at = _fresh_timestamp(
        value.get("updated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    state = str(value.get("continuity_state") or "")
    receipt_digest = str(value.get("continuity_receipt_sha256") or "")
    active = state == "active"
    if not (
        value.get("schema") == continuity.VERIFY_SCHEMA
        and value.get("status") == "verified"
        and state in {"active", "inactive", "degraded"}
        and updated_at is not None
        and _SHA256.fullmatch(receipt_digest)
        and progress.get("current_evidence_verified") is True
        and progress.get("receipt_integrity_verified") is True
        and value.get("verification_receipt_persisted") is True
        and (value.get("persistent_reevaluation_verified") is True) is active
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and _SHA256.fullmatch(str(runtime.get("sha256") or ""))
        and (
            not active
            or (
                runtime.get("scheduler_condition") == "running"
                and witness.get("status") == "verified"
                and witness.get("cycle_binding_verified") is True
                and witness.get("persistent_reevaluation_verified") is True
                and _SHA256.fullmatch(
                    str(witness.get("iteration_witness_sha256") or "")
                )
                and _SHA256.fullmatch(
                    str(witness.get("cycle_receipt_sha256") or "")
                )
            )
        )
    ):
        raise ValueError("scheduler_activation_settlement_continuity_not_admissible")
    return {
        "continuity_state": state,
        "updated_at": updated_at,
        "continuity_receipt_sha256": receipt_digest,
        "runtime_observation_sha256": str(runtime.get("sha256") or ""),
        "scheduler_condition": str(runtime.get("scheduler_condition") or ""),
        "iteration_witness_sha256": str(
            witness.get("iteration_witness_sha256") or ""
        ),
        "cycle_receipt_sha256": str(
            witness.get("cycle_receipt_sha256") or ""
        ),
        "persistent_reevaluation_verified": active,
    }


def _project_history(value: Mapping[str, Any]) -> dict[str, Any]:
    progress = value.get("progress")
    state = str(value.get("history_state") or "")
    present = value.get("execution_history_present") is True
    latest = value.get("latest_execution")
    latest_row = dict(latest) if isinstance(latest, Mapping) else {}
    release = latest_row.get("release_evidence")
    latest_admissible = bool(
        not present
        or (
            isinstance(latest, Mapping)
            and latest_row.get("status") == "verified"
            and execution._CLAIM_ID.fullmatch(
                str(latest_row.get("claim_id") or "")
            )
            and latest_row.get("claim_id") == value.get("latest_claim_id")
            and latest_row.get("claim_sha256")
            == value.get("latest_claim_sha256")
            and (
                state != "execution_result"
                or latest_row.get("result_sha256")
                == value.get("latest_result_sha256")
            )
            and execution._DECISION_ID.fullmatch(
                str(latest_row.get("decision_id") or "")
            )
            and isinstance(release, Mapping)
            and _COMMIT.fullmatch(
                str(release.get("runtime_commit_sha") or "")
            )
            and _COMMIT.fullmatch(
                str(release.get("envelope_commit_sha") or "")
            )
            and _IMAGE.fullmatch(str(release.get("web_image_digest") or ""))
            and _IMAGE.fullmatch(
                str(release.get("render_image_digest") or "")
            )
            and _SHA256.fullmatch(
                str(release.get("preflight_script_sha256") or "")
            )
            and latest_row.get("authorization_consumed") is True
            and latest_row.get("automatic_execution_allowed") is False
            and latest_row.get("execution_authorized") is False
            and latest_row.get("deployment_or_restart_authorized") is False
            and latest_row.get("provider_quota_consumption_allowed") is False
            and latest_row.get("delivery_authorized") is False
        )
    )
    if not (
        value.get("schema") == execution.HISTORY_SCHEMA
        and value.get("status") == "verified"
        and state
        in {"no_execution_history", "claimed_without_result", "execution_result"}
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and present is (state != "no_execution_history")
        and latest_admissible
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and (
            not present
            or (
                _SHA256.fullmatch(
                    str(value.get("latest_claim_sha256") or "")
                )
                and execution._CLAIM_ID.fullmatch(
                    str(value.get("latest_claim_id") or "")
                )
                and (
                    state != "execution_result"
                    or _SHA256.fullmatch(
                        str(value.get("latest_result_sha256") or "")
                    )
                )
            )
        )
    ):
        raise ValueError("scheduler_activation_execution_history_not_admissible")
    return {
        "history_state": state,
        "execution_history_present": present,
        "history_sha256": _sha256(_canonical(value)),
        "latest_claim_id": str(value.get("latest_claim_id") or ""),
        "latest_claim_sha256": str(value.get("latest_claim_sha256") or ""),
        "latest_result_sha256": str(value.get("latest_result_sha256") or ""),
        "latest_execution": latest_row,
        "historical_deployment_attempted": (
            value.get("deployment_attempted") is True
        ),
        "historical_protected_operation_executed": (
            value.get("protected_operation_executed") is True
        ),
    }


def _settlement_state(
    *,
    history: Mapping[str, Any],
    continuity_source: Mapping[str, Any],
    release: Mapping[str, Any],
) -> tuple[str, str, str, bool]:
    continuity_state = str(continuity_source.get("continuity_state") or "")
    history_state = str(history.get("history_state") or "")
    if history_state == "no_execution_history":
        if continuity_state == "active":
            return (
                "externally_active",
                "",
                "continue fresh continuity checks without attributing this runtime to governed activation history",
                False,
            )
        return (
            "no_governed_execution",
            "",
            "continue fresh continuity verification",
            False,
        )
    latest = dict(history.get("latest_execution") or {})
    execution_state = str(latest.get("execution_state") or "claimed")
    if history_state == "claimed_without_result":
        return (
            "recovery_required",
            "scheduler_activation_execution_claimed_without_result",
            "inspect current runtime and immutable execution receipts; do not replay the deployment",
            True,
        )
    if execution_state != "succeeded":
        return (
            "recovery_required",
            str(latest.get("blocking_reason") or "scheduler_activation_execution_failed"),
            "inspect current runtime and immutable execution receipts; do not replay the deployment",
            True,
        )
    expected = dict(latest.get("release_evidence") or {})
    exact_runtime = bool(
        release.get("status") == "observed"
        and release.get("release_commit_sha")
        == expected.get("runtime_commit_sha")
        and release.get("image_digest") == expected.get("web_image_digest")
        and release.get("release_image_digest")
        == expected.get("web_image_digest")
    )
    if continuity_state != "active":
        return (
            "recovery_required",
            "scheduler_activation_success_not_currently_active",
            "inspect current runtime and settled execution receipts; do not replay the deployment",
            True,
        )
    if not exact_runtime:
        return (
            "recovery_required",
            "scheduler_activation_current_release_identity_mismatch",
            "inspect the current scheduler release identity and execution receipts; do not replay the deployment",
            True,
        )
    return (
        "settled_success",
        "",
        "continue fresh cycle-bound scheduler continuity verification",
        False,
    )


def build_scheduler_activation_settlement_receipt(
    *,
    scheduler_continuity: Mapping[str, Any],
    execution_history: Mapping[str, Any],
    release_identity_observation: Mapping[str, Any],
    project: str = "property",
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    if _PROJECT.fullmatch(str(project or "")) is None:
        raise ValueError("scheduler_activation_settlement_project_not_admissible")
    continuity_source = _project_continuity(
        scheduler_continuity,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    history = _project_history(execution_history)
    release = _project_release_observation(
        release_identity_observation,
        now=observed_now,
        project=project,
        max_age_seconds=max_age_seconds,
    )
    if release.get("status") == "blocked":
        raise ValueError(
            str(release.get("blocking_reason") or "scheduler_release_identity_blocked")
        )
    if (
        continuity_source.get("continuity_state") == "active"
    ) is not (release.get("status") == "observed"):
        raise ValueError("scheduler_continuity_release_identity_disagrees")
    state, reason, next_action, action_required = _settlement_state(
        history=history,
        continuity_source=continuity_source,
        release=release,
    )
    latest = dict(history.get("latest_execution") or {})
    action = {
        "lane": "scheduler_activation_settlement",
        "reason": reason,
        "source_generated_at": observed_now.isoformat(),
        "safe_next_action": next_action,
        "consent_required": False,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "protected_operations": ["runtime_recovery_inspection"],
        "provider_quota_consumption_allowed": False,
        "claim_id": str(history.get("latest_claim_id") or ""),
        "claim_sha256": str(history.get("latest_claim_sha256") or ""),
        "result_sha256": str(history.get("latest_result_sha256") or ""),
        "decision_id": str(latest.get("decision_id") or ""),
        "execution_state": str(latest.get("execution_state") or ""),
        "expected_runtime_commit_sha": str(
            dict(latest.get("release_evidence") or {}).get(
                "runtime_commit_sha"
            )
            or ""
        ),
        "observed_runtime_commit_sha": str(
            release.get("release_commit_sha") or ""
        ),
        "observed_image_digest": str(release.get("image_digest") or ""),
    }
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": state,
        "updated_at": observed_now.isoformat(),
        "blocking_reason": reason,
        "next_action": next_action,
        "compose_project": project,
        "source_evidence": {
            "scheduler_continuity": continuity_source,
            "execution_history": history,
            "scheduler_release_identity": release,
        },
        "source_bindings": {
            "continuity_receipt_sha256": continuity_source[
                "continuity_receipt_sha256"
            ],
            "runtime_observation_sha256": continuity_source[
                "runtime_observation_sha256"
            ],
            "iteration_witness_sha256": continuity_source[
                "iteration_witness_sha256"
            ],
            "cycle_receipt_sha256": continuity_source[
                "cycle_receipt_sha256"
            ],
            "execution_history_sha256": history["history_sha256"],
            "latest_claim_sha256": history["latest_claim_sha256"],
            "latest_result_sha256": history["latest_result_sha256"],
            "release_identity_sha256": _sha256(_canonical(release)),
        },
        "action_required": action_required,
        "interrupt_operator": False,
        "actions": [action] if action_required else [],
        "authorization_recorded": bool(
            history.get("execution_history_present")
        ),
        "authorization_consumed": bool(
            history.get("execution_history_present")
        ),
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "historical_deployment_attempted": history[
            "historical_deployment_attempted"
        ],
        "protected_operation_executed": False,
        "historical_protected_operation_executed": history[
            "historical_protected_operation_executed"
        ],
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "receipt_persisted": True,
        "secret_values_recorded": False,
        "progress": {
            "current_evidence_verified": True,
            "continuity_receipt_verified": True,
            "execution_history_verified": True,
            "release_identity_observed": release.get("status") == "observed",
            "governed_execution_settled": state == "settled_success",
        },
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def verify_scheduler_activation_settlement_receipt(
    receipt: Mapping[str, Any],
    *,
    scheduler_continuity: Mapping[str, Any],
    execution_history: Mapping[str, Any],
    release_identity_observation: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    updated_at = _fresh_timestamp(
        receipt.get("updated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if updated_at is None:
        return _blocked(
            "scheduler_activation_settlement_not_fresh",
            now=observed_now,
        )
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _blocked(
            "scheduler_activation_settlement_integrity_invalid",
            now=observed_now,
        )
    try:
        expected = build_scheduler_activation_settlement_receipt(
            scheduler_continuity=scheduler_continuity,
            execution_history=execution_history,
            release_identity_observation=release_identity_observation,
            project=str(receipt.get("compose_project") or ""),
            now=execution._timestamp(updated_at),
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_activation_settlement_source_not_admissible",
            now=observed_now,
        )
    if dict(receipt) != expected:
        return _blocked(
            "scheduler_activation_settlement_source_binding_mismatch",
            now=observed_now,
        )
    state = str(receipt.get("status") or "")
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "settlement_state": state,
        "updated_at": observed_now.isoformat(),
        "settlement_observed_at": updated_at,
        "settlement_receipt_sha256": _sha256(_canonical(receipt)),
        "blocking_reason": str(receipt.get("blocking_reason") or ""),
        "next_action": str(receipt.get("next_action") or ""),
        "compose_project": str(receipt.get("compose_project") or ""),
        "source_evidence": dict(receipt.get("source_evidence") or {}),
        "source_bindings": dict(receipt.get("source_bindings") or {}),
        "source_cycle_receipt_sha256": str(
            dict(receipt.get("source_bindings") or {}).get(
                "continuity_receipt_sha256"
            )
            or ""
        ),
        "action_required": receipt.get("action_required") is True,
        "interrupt_operator": False,
        "actions": list(receipt.get("actions") or []),
        "authorization_recorded": receipt.get("authorization_recorded") is True,
        "authorization_consumed": receipt.get("authorization_consumed") is True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "historical_deployment_attempted": (
            receipt.get("historical_deployment_attempted") is True
        ),
        "protected_operation_executed": False,
        "historical_protected_operation_executed": (
            receipt.get("historical_protected_operation_executed") is True
        ),
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "receipt_integrity_verified": True,
        },
    }


def project_scheduler_activation_settlement_handoff(
    settlement: Mapping[str, Any],
) -> dict[str, Any]:
    """Adapt verified settlement truth to the shared presentation contract."""

    if settlement.get("status") != "verified":
        return {
            **dict(settlement),
            "status": "blocked",
            "action_required": False,
            "interrupt_operator": False,
            "actions": [],
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }
    action_required = settlement.get("action_required") is True
    return {
        **dict(settlement),
        "verification_status": "verified",
        "status": "action_required" if action_required else "ready",
        "interrupt_operator": False,
        "actions": list(settlement.get("actions") or []),
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def materialize_current_scheduler_activation_settlement_bundle(
    *,
    scheduler_continuity: Mapping[str, Any],
    execution_dir: Path = execution.DEFAULT_EXECUTION_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    project: str = "property",
    now: datetime | None = None,
    release_identity_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    history = execution.inspect_scheduler_activation_execution_history(
        execution_dir=execution_dir,
        now=observed_now,
    )
    release_observation = dict(
        release_identity_observation
        or observe_current_scheduler_release_identity(
            project=project,
            now=observed_now,
        )
    )
    try:
        receipt = build_scheduler_activation_settlement_receipt(
            scheduler_continuity=scheduler_continuity,
            execution_history=history,
            release_identity_observation=release_observation,
            project=project,
            now=observed_now,
        )
        atomic_write_bytes(
            Path(receipt_path).absolute(),
            _canonical(receipt),
            overwrite=True,
        )
        verification = verify_scheduler_activation_settlement_receipt(
            receipt,
            scheduler_continuity=scheduler_continuity,
            execution_history=history,
            release_identity_observation=release_observation,
            now=observed_now,
        )
        if verification.get("status") != "verified":
            raise ValueError("scheduler_activation_settlement_verification_failed")
    except (OSError, TypeError, ValueError):
        verification = _blocked(
            "scheduler_activation_settlement_materialization_failed",
            now=observed_now,
        )
    verification["receipt_path"] = str(Path(receipt_path).absolute())
    verification["verification_path"] = str(Path(verification_path).absolute())
    verification["execution_dir"] = str(Path(execution_dir).absolute())
    verification["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(verification_path).absolute(),
            _canonical(verification),
            overwrite=True,
        )
    except Exception:
        verification["verification_receipt_persisted"] = False
        verification.update(
            {
                "status": "blocked",
                "settlement_state": "blocked",
                "blocking_reason": (
                    "scheduler_activation_settlement_verification_persistence_failed"
                ),
                "action_required": False,
                "interrupt_operator": False,
                "actions": [],
            }
        )
    return verification
