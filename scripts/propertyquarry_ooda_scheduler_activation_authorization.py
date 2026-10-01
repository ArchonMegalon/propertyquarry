#!/usr/bin/env python3
"""Stage an expiring, non-executing scheduler activation authorization request."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_scheduler_activation_authorization_request.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_authorization_request_verification.v1"
)
HANDOFF_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_authorization_handoff.v1"
)
DEFAULT_REQUEST_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-authorization-request.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-authorization-request-verification.json"
)
DEFAULT_PRESENTATION_STATE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-presentation-state.json"
)
DEFAULT_MAX_AGE_SECONDS = 300.0
DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 1800
DEFAULT_REFRESH_BEFORE_SECONDS = 300
MAX_REQUEST_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"pqsar_[0-9a-f]{24}\Z")
_DECISION_OPTIONS = ["approve_exact_scope", "reject", "defer"]
_PROTECTED_OPERATIONS = [
    "authoritative_local_propertyquarry_deployment",
    "database_migration",
    "container_create_or_replace",
    "scheduler_activation",
]
_EXPECTED_SCOPE = {
    "operation": "authoritative_local_propertyquarry_deployment",
    "activation_target": (
        "propertyquarry_scheduler_via_full_release_envelope"
    ),
    "preflight_command": (
        "bash scripts/deploy_propertyquarry.sh --preflight-only --no-build"
    ),
    "deployment_command": "bash scripts/deploy_propertyquarry.sh --no-build",
    "requested_execution_mode": (
        "manual_after_separate_explicit_authorization"
    ),
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    if not math.isfinite(age) or age < 0 or age > max_age_seconds:
        return None
    return parsed.isoformat()


def _readiness_projection(
    value: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    scope = value.get("scope")
    source_evidence = value.get("source_evidence")
    source_bindings = value.get("source_bindings")
    progress = value.get("progress")
    if not all(
        isinstance(row, Mapping)
        for row in (scope, source_evidence, source_bindings, progress)
    ):
        raise ValueError("scheduler_activation_readiness_not_admissible")
    preflight = source_evidence.get("activation_preflight")
    if not isinstance(preflight, Mapping):
        raise ValueError("scheduler_activation_readiness_not_admissible")
    updated_at = _fresh_timestamp(
        value.get("updated_at"), now=now, max_age_seconds=max_age_seconds
    )
    observed_at = _fresh_timestamp(
        value.get("readiness_observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    readiness_state = str(value.get("readiness_state") or "")
    receipt_digest = str(value.get("readiness_receipt_sha256") or "")
    expected_scope = {**_EXPECTED_SCOPE, "compose_project": scope.get("compose_project")}
    ready = readiness_state == "ready_for_authorization"
    runtime_commit = str(preflight.get("runtime_commit_sha") or "")
    envelope_commit = str(preflight.get("envelope_commit_sha") or "")
    web_image = str(preflight.get("web_image_digest") or "")
    render_image = str(preflight.get("render_image_digest") or "")
    if not (
        value.get("schema") == readiness.VERIFY_SCHEMA
        and value.get("status") == "verified"
        and readiness_state in {
            "ready_for_authorization",
            "blocked",
            "not_required",
        }
        and updated_at is not None
        and observed_at is not None
        and _SHA256.fullmatch(receipt_digest)
        and dict(scope) == expected_scope
        and str(scope.get("compose_project") or "").strip()
        and all(
            _SHA256.fullmatch(str(source_bindings.get(key) or ""))
            for key in (
                "continuity_receipt_sha256",
                "runtime_observation_sha256",
                "preflight_observation_sha256",
                "preflight_script_sha256",
            )
        )
        and progress.get("current_evidence_verified") is True
        and progress.get("receipt_integrity_verified") is True
        and progress.get("activation_preflight_current") is True
        and (progress.get("activation_preflight_passed") is True) is ready
        and value.get("verification_receipt_persisted") is True
        and (value.get("action_required") is True) is ready
        and value.get("interrupt_operator") is False
        and (value.get("authorization_required") is True) is ready
        and value.get("authorization_recorded") is False
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("deployment_or_restart_performed") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and value.get("delivery_attempted") is False
        and (
            not ready
            or (
                preflight.get("status") == "ready"
                and _COMMIT.fullmatch(runtime_commit)
                and _COMMIT.fullmatch(envelope_commit)
                and _IMAGE.fullmatch(web_image)
                and _IMAGE.fullmatch(render_image)
                and preflight.get("build_performed") is False
                and preflight.get("deployment_or_restart_performed") is False
                and preflight.get("provider_quota_consumed") is False
                and preflight.get("delivery_attempted") is False
            )
        )
    ):
        raise ValueError("scheduler_activation_readiness_not_admissible")
    return {
        "status": "verified",
        "readiness_state": readiness_state,
        "updated_at": updated_at,
        "observed_at": observed_at,
        "blocking_reason": str(value.get("blocking_reason") or ""),
        "readiness_receipt_sha256": receipt_digest,
        "scope": dict(scope),
        "source_bindings": dict(source_bindings),
        "release_evidence": {
            "runtime_commit_sha": runtime_commit,
            "envelope_commit_sha": envelope_commit,
            "web_image_digest": web_image,
            "render_image_digest": render_image,
            "preflight_script_sha256": str(
                source_bindings.get("preflight_script_sha256") or ""
            ),
        },
    }


def _semantic_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope": dict(source.get("scope") or {}),
        "release_evidence": dict(source.get("release_evidence") or {}),
        "decision_options": list(_DECISION_OPTIONS),
        "protected_operations": list(_PROTECTED_OPERATIONS),
    }


def _request_envelope_admissible(
    request: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    generated_at: datetime,
) -> bool:
    binding = request.get("binding")
    request_scope = request.get("scope")
    release_evidence = request.get("release_evidence")
    progress = request.get("progress")
    request_authorization = request.get("authorization")
    binding_updated_at = (
        _parse_timestamp(binding.get("readiness_updated_at"))
        if isinstance(binding, Mapping)
        else None
    )
    expected_root_keys = {
        "schema",
        "request_id",
        "semantic_request_sha256",
        "generated_at",
        "expires_at",
        "status",
        "blocking_reason",
        "next_action",
        "scope",
        "release_evidence",
        "binding",
        "decision_options",
        "protected_operations",
        "authorization",
        "progress",
        "action_required",
        "automatic_execution_allowed",
        "execution_authorized",
        "deployment_or_restart_authorized",
        "deployment_or_restart_performed",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
        "receipt_persisted",
        "secret_values_recorded",
        "integrity",
    }
    semantic_digest = _sha256(_canonical(_semantic_payload(source)))
    return bool(
        set(request) == expected_root_keys
        and request.get("schema") == SCHEMA
        and _REQUEST_ID.fullmatch(str(request.get("request_id") or ""))
        and request.get("request_id") == f"pqsar_{semantic_digest[:24]}"
        and request.get("semantic_request_sha256") == semantic_digest
        and isinstance(request_scope, Mapping)
        and dict(request_scope) == source.get("scope")
        and isinstance(release_evidence, Mapping)
        and dict(release_evidence) == source.get("release_evidence")
        and isinstance(binding, Mapping)
        and set(binding)
        == {
            "readiness_receipt_sha256",
            "readiness_updated_at",
            "continuity_receipt_sha256",
            "runtime_observation_sha256",
            "preflight_observation_sha256",
            "preflight_script_sha256",
        }
        and all(
            _SHA256.fullmatch(str(binding.get(key) or ""))
            for key in (
                "readiness_receipt_sha256",
                "continuity_receipt_sha256",
                "runtime_observation_sha256",
                "preflight_observation_sha256",
                "preflight_script_sha256",
            )
        )
        and binding_updated_at is not None
        and binding_updated_at <= generated_at
        and request.get("status") == "awaiting_explicit_authorization"
        and request.get("blocking_reason")
        == "explicit_scheduler_activation_authorization_required"
        and str(request.get("next_action") or "").strip()
        and request.get("decision_options") == _DECISION_OPTIONS
        and request.get("protected_operations") == _PROTECTED_OPERATIONS
        and request_authorization
        == {
            "required": True,
            "recorded": False,
            "decision": "pending",
            "decided_at": "",
            "decider_identity_recorded": False,
        }
        and progress
        == {
            "activation_readiness_verified": True,
            "activation_preflight_passed": True,
            "authorization_request_staged": True,
            "authorization_recorded": False,
            "current_evidence_verified": True,
        }
        and request.get("action_required") is True
        and request.get("automatic_execution_allowed") is False
        and request.get("execution_authorized") is False
        and request.get("deployment_or_restart_authorized") is False
        and request.get("deployment_or_restart_performed") is False
        and request.get("protected_operation_executed") is False
        and request.get("provider_quota_consumption_allowed") is False
        and request.get("delivery_authorized") is False
        and request.get("delivery_attempted") is False
        and request.get("sent") is False
        and request.get("receipt_persisted") is True
        and request.get("secret_values_recorded") is False
    )


def build_scheduler_activation_authorization_request(
    *,
    activation_readiness: Mapping[str, Any],
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        raise ValueError("scheduler_activation_authorization_ttl_not_admissible")
    source = _readiness_projection(
        activation_readiness,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if source["readiness_state"] != "ready_for_authorization":
        raise ValueError("scheduler_activation_not_ready_for_authorization")
    semantic_payload = _semantic_payload(source)
    semantic_digest = _sha256(_canonical(semantic_payload))
    request: dict[str, Any] = {
        "schema": SCHEMA,
        "request_id": f"pqsar_{semantic_digest[:24]}",
        "semantic_request_sha256": semantic_digest,
        "generated_at": observed_now.isoformat(),
        "expires_at": (
            observed_now + timedelta(seconds=ttl_seconds)
        ).isoformat(),
        "status": "awaiting_explicit_authorization",
        "blocking_reason": "explicit_scheduler_activation_authorization_required",
        "next_action": (
            "explicitly approve, reject, or defer this exact full-release activation scope; "
            "no deployment or restart occurs from this request"
        ),
        "scope": dict(source["scope"]),
        "release_evidence": dict(source["release_evidence"]),
        "binding": {
            "readiness_receipt_sha256": source[
                "readiness_receipt_sha256"
            ],
            "readiness_updated_at": source["updated_at"],
            **dict(source["source_bindings"]),
        },
        "decision_options": list(_DECISION_OPTIONS),
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "authorization": {
            "required": True,
            "recorded": False,
            "decision": "pending",
            "decided_at": "",
            "decider_identity_recorded": False,
        },
        "progress": {
            "activation_readiness_verified": True,
            "activation_preflight_passed": True,
            "authorization_request_staged": True,
            "authorization_recorded": False,
            "current_evidence_verified": True,
        },
        "action_required": True,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "receipt_persisted": True,
        "secret_values_recorded": False,
    }
    request["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(request)),
    }
    return request


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate the request from current verified activation readiness",
        "action_required": False,
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {"current_evidence_verified": False},
    }


def verify_scheduler_activation_authorization_request(
    request: Mapping[str, Any],
    *,
    activation_readiness: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _parse_timestamp(request.get("generated_at"))
    expires_at = _parse_timestamp(request.get("expires_at"))
    if generated_at is None or expires_at is None:
        return _blocked(
            "scheduler_activation_authorization_timestamp_invalid",
            now=observed_now,
        )
    ttl_seconds = (expires_at - generated_at).total_seconds()
    age_seconds = (observed_now - generated_at).total_seconds()
    if not (
        math.isfinite(ttl_seconds)
        and ttl_seconds.is_integer()
        and MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        and math.isfinite(age_seconds)
        and age_seconds >= 0
        and observed_now <= expires_at
    ):
        return _blocked(
            "scheduler_activation_authorization_not_fresh",
            now=observed_now,
        )
    normalized = dict(request)
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
            "scheduler_activation_authorization_integrity_invalid",
            now=observed_now,
        )
    try:
        source = _readiness_projection(
            activation_readiness,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_activation_authorization_source_not_admissible",
            now=observed_now,
        )
    if not (
        source.get("readiness_state") == "ready_for_authorization"
        and _request_envelope_admissible(
            request,
            source=source,
            generated_at=generated_at,
        )
    ):
        return _blocked(
            "scheduler_activation_authorization_source_binding_mismatch",
            now=observed_now,
        )
    request_digest = _sha256(_canonical(request))
    exact_readiness_binding = (
        request.get("binding", {}).get("readiness_receipt_sha256")
        == source.get("readiness_receipt_sha256")
    )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": request_digest,
        "semantic_request_sha256": str(
            request.get("semantic_request_sha256") or ""
        ),
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "blocking_reason": "explicit_scheduler_activation_authorization_required",
        "next_action": str(request.get("next_action") or ""),
        "scope": dict(request.get("scope") or {}),
        "release_evidence": dict(request.get("release_evidence") or {}),
        "binding": dict(request.get("binding") or {}),
        "binding_mode": (
            "exact_readiness_receipt"
            if exact_readiness_binding
            else "stable_scope_current_readiness"
        ),
        "current_readiness_receipt_sha256": str(
            source.get("readiness_receipt_sha256") or ""
        ),
        "decision_options": list(_DECISION_OPTIONS),
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "action_required": True,
        "authorization_required": True,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            **dict(request.get("progress") or {}),
            "request_integrity_verified": True,
        },
    }


def _private_request(path: Path) -> tuple[dict[str, Any], str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("scheduler_activation_authorization_file_not_admissible")
    payload, _raw, digest = load_strict_json_object_snapshot(
        target,
        field="scheduler_activation_authorization_request",
        maximum_bytes=MAX_REQUEST_BYTES,
    )
    return payload, digest


def _suppressed_handoff(
    source: Mapping[str, Any],
    *,
    request_path: Path,
    now: datetime,
) -> dict[str, Any]:
    state = str(source.get("readiness_state") or "blocked")
    not_required = state == "not_required"
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "ready",
        "handoff_state": (
            "authorization_not_required"
            if not_required
            else "authorization_not_ready"
        ),
        "updated_at": now.isoformat(),
        "blocking_reason": str(
            source.get("blocking_reason")
            or (
                "scheduler_activation_not_required"
                if not_required
                else "scheduler_activation_not_ready_for_authorization"
            )
        ),
        "next_action": (
            "continue fresh continuity verification"
            if not_required
            else "resolve activation readiness blockers before staging operator consent"
        ),
        "request_path": str(Path(request_path).absolute()),
        "source_cycle_receipt_sha256": str(
            source.get("readiness_receipt_sha256") or ""
        ),
        "action_required": False,
        "interrupt_operator": False,
        "actions": [],
        "progress": {
            "current_evidence_verified": True,
            "authorization_request_staged": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def _authorization_action(
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    release = dict(verification.get("release_evidence") or {})
    return {
        "lane": "scheduler_activation",
        "reason": "explicit_scheduler_activation_authorization_required",
        "source_generated_at": str(verification.get("updated_at") or ""),
        "safe_next_action": (
            "review and explicitly approve, reject, or defer the exact expiring "
            "full-release activation request; no action is executed automatically"
        ),
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "provider_quota_consumption_allowed": False,
        "request_id": str(verification.get("request_id") or ""),
        "semantic_request_sha256": str(
            verification.get("semantic_request_sha256") or ""
        ),
        "scope_sha256": _sha256(
            _canonical(dict(verification.get("scope") or {}))
        ),
        "runtime_commit_sha": str(release.get("runtime_commit_sha") or ""),
        "envelope_commit_sha": str(release.get("envelope_commit_sha") or ""),
        "web_image_digest": str(release.get("web_image_digest") or ""),
        "render_image_digest": str(release.get("render_image_digest") or ""),
        "preflight_script_sha256": str(
            release.get("preflight_script_sha256") or ""
        ),
    }


def stage_current_scheduler_activation_authorization_handoff(
    *,
    activation_readiness: Mapping[str, Any],
    request_path: Path = DEFAULT_REQUEST_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    refresh_before_seconds: int = DEFAULT_REFRESH_BEFORE_SECONDS,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        source = _readiness_projection(
            activation_readiness,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return {
            **_blocked(
                "scheduler_activation_readiness_not_admissible",
                now=observed_now,
            ),
            "schema": HANDOFF_SCHEMA,
            "handoff_state": "blocked",
            "request_path": str(Path(request_path).absolute()),
            "actions": [],
        }
    if source["readiness_state"] != "ready_for_authorization":
        return _suppressed_handoff(
            source,
            request_path=request_path,
            now=observed_now,
        )
    if (
        isinstance(refresh_before_seconds, bool)
        or not isinstance(refresh_before_seconds, int)
        or not 0 <= refresh_before_seconds < ttl_seconds
    ):
        return {
            **_blocked(
                "scheduler_activation_authorization_refresh_policy_invalid",
                now=observed_now,
            ),
            "schema": HANDOFF_SCHEMA,
            "handoff_state": "blocked",
            "request_path": str(Path(request_path).absolute()),
            "actions": [],
        }
    try:
        request_state = "created"
        target = Path(request_path).absolute()
        target_existed = target.exists()
        request: dict[str, Any] | None = None
        if target_existed:
            candidate, _candidate_digest = _private_request(target)
            candidate_verification = (
                verify_scheduler_activation_authorization_request(
                    candidate,
                    activation_readiness=activation_readiness,
                    now=observed_now,
                    max_age_seconds=max_age_seconds,
                )
            )
            candidate_expires_at = _parse_timestamp(
                candidate_verification.get("expires_at")
            )
            if (
                candidate_verification.get("status") == "verified"
                and candidate_expires_at is not None
                and (candidate_expires_at - observed_now).total_seconds()
                > refresh_before_seconds
            ):
                request = candidate
                request_state = "retained_current"
        if request is None:
            request = build_scheduler_activation_authorization_request(
                activation_readiness=activation_readiness,
                now=observed_now,
                ttl_seconds=ttl_seconds,
                max_age_seconds=max_age_seconds,
            )
            atomic_write_bytes(
                target,
                _canonical(request),
                overwrite=True,
            )
            request_state = "refreshed" if target_existed else "created"
        persisted_request, persisted_digest = _private_request(request_path)
        if persisted_request != request or persisted_digest != _sha256(
            _canonical(request)
        ):
            raise ValueError(
                "scheduler_activation_authorization_persistence_mismatch"
            )
        verification = verify_scheduler_activation_authorization_request(
            request,
            activation_readiness=activation_readiness,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError(
                "scheduler_activation_authorization_verification_failed"
            )
        verification["request_path"] = str(Path(request_path).absolute())
        verification["verification_path"] = str(
            Path(verification_path).absolute()
        )
        verification["verification_receipt_persisted"] = True
        atomic_write_bytes(
            Path(verification_path).absolute(),
            _canonical(verification),
            overwrite=True,
        )
    except (OSError, TypeError, ValueError):
        return {
            **_blocked(
                "scheduler_activation_authorization_staging_failed",
                now=observed_now,
            ),
            "schema": HANDOFF_SCHEMA,
            "handoff_state": "blocked",
            "request_path": str(Path(request_path).absolute()),
            "actions": [],
        }
    action = _authorization_action(verification)
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "action_required",
        "handoff_state": "authorization_request_staged",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "explicit_scheduler_activation_authorization_required",
        "next_action": str(verification.get("next_action") or ""),
        "request_path": str(Path(request_path).absolute()),
        "verification_path": str(Path(verification_path).absolute()),
        "request_id": str(verification.get("request_id") or ""),
        "request_state": request_state,
        "request_sha256": str(verification.get("request_sha256") or ""),
        "semantic_request_sha256": str(
            verification.get("semantic_request_sha256") or ""
        ),
        "expires_at": str(verification.get("expires_at") or ""),
        "scope": dict(verification.get("scope") or {}),
        "release_evidence": dict(verification.get("release_evidence") or {}),
        "decision_options": list(_DECISION_OPTIONS),
        "protected_operations": list(_PROTECTED_OPERATIONS),
        "source_cycle_receipt_sha256": str(
            activation_readiness.get("readiness_receipt_sha256") or ""
        ),
        "action_required": True,
        "interrupt_operator": False,
        "actions": [action],
        "progress": {
            "current_evidence_verified": True,
            "authorization_request_staged": True,
            "action_required_count": 1,
            "novel_action_count": 1,
        },
        "authorization_required": True,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
