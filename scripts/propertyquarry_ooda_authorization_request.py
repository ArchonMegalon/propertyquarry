#!/usr/bin/env python3
"""Stage and verify one non-executing PropertyQuarry authorization request."""

from __future__ import annotations

import argparse
import json
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

from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_runtime_authorization_request.v3"
PREVIOUS_SCHEMA = "propertyquarry.ooda_runtime_authorization_request.v2"
LEGACY_SCHEMA = "propertyquarry.ooda_runtime_authorization_request.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_runtime_authorization_request_verification.v1"
HANDOFF_SCHEMA = "propertyquarry.ooda_runtime_authorization_handoff.v1"
DEFAULT_REQUEST_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-request.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-request-verification.json"
)
DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 1800
DEFAULT_REFRESH_BEFORE_SECONDS = 300
MAX_REQUEST_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"pqar_[0-9a-f]{24}\Z")
_LEGACY_REVIEW_PACKET_SCHEMA = re.compile(
    r"propertyquarry\.ooda_runtime_review_packet\.v[1-5]\Z"
)
_EXCLUDED_OPERATIONS = [
    "deployment_or_restart",
    "provider_account_change",
    "provider_quota_consumption",
    "external_delivery",
]
_LEGACY_PROBE_SCOPE_KEYS = {
    "operation",
    "change_id",
    "current_value",
    "proposed_value",
    "public_host",
    "public_origin",
    "compose_project",
}
_LOCAL_ENVIRONMENT_MERGE_SCOPE_KEYS = {
    "operation",
    "change_id",
    "recovery_preview_sha256",
    "candidate_path",
    "candidate_policy_schema",
    "candidate_review_keys",
    "candidate_review_key_count",
    "configuration_merge_policy",
    "existing_environment_values_overwrite_allowed",
    "rollback_required",
    "compose_project",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return runtime_review._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return runtime_review._sha256(value)


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate the authorization request from a fresh verified runtime review packet",
        "progress": {
            "current_evidence_verified": False,
            "authorization_request_verified": False,
        },
        "authorization_required": True,
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _private_object(path: Path, *, field: str, maximum_bytes: int) -> tuple[dict[str, Any], bytes, str]:
    target = runtime_review._rooted(path)
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{field}_file_not_admissible")
    return load_strict_json_object_snapshot(
        target,
        field=field,
        maximum_bytes=maximum_bytes,
    )


def _request_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _scope_kind(scope: object) -> str:
    if not isinstance(scope, Mapping):
        return ""
    if set(scope) == _LEGACY_PROBE_SCOPE_KEYS:
        if (
            scope.get("operation") == "runtime_configuration_change"
            and scope.get("change_id") == "live_probe_origin"
            and all(str(scope.get(key) or "").strip() for key in scope)
            and scope.get("current_value") != scope.get("proposed_value")
        ):
            return "live_probe_origin"
        return ""
    if set(scope) == _LOCAL_ENVIRONMENT_MERGE_SCOPE_KEYS:
        review_keys = list(scope.get("candidate_review_keys") or [])
        if (
            scope.get("operation") == "runtime_configuration_change"
            and scope.get("change_id")
            == "local_environment_candidate_merge"
            and _SHA256.fullmatch(
                str(scope.get("recovery_preview_sha256") or "")
            )
            and scope.get("candidate_path")
            == runtime_review.DEFAULT_DEPLOYMENT_ENV_LOCAL_RUNTIME_CANDIDATE_PATH.as_posix()
            and scope.get("candidate_policy_schema")
            == "propertyquarry.deployment_environment_local_runtime_candidate.v2"
            and review_keys == sorted(runtime_review._LOCAL_CANDIDATE_KEYS)
            and scope.get("candidate_review_key_count") == len(review_keys)
            and scope.get("configuration_merge_policy")
            == "add_missing_keys_only"
            and scope.get("existing_environment_values_overwrite_allowed")
            is False
            and scope.get("rollback_required") is True
            and str(scope.get("compose_project") or "").strip()
        ):
            return "local_environment_candidate_merge"
    return ""


def _request_identifier(
    binding: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> str:
    """Return the legacy semantic identifier used by v1/v2 requests."""

    identity = (
        {"scope": dict(scope)}
        if _scope_kind(scope) == "local_environment_candidate_merge"
        else {"binding": dict(binding), "scope": dict(scope)}
    )
    return "pqar_" + _sha256(_canonical(identity))[:24]


def _request_instance_identifier(
    binding: Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    generated_at: str,
) -> str:
    """Bind a v3 request ID to one bounded consent opportunity."""

    if _request_timestamp(generated_at) is None:
        raise ValueError("authorization_request_timestamp_invalid")
    identity = {
        "semantic_request_id": _request_identifier(binding, scope),
        "generated_at": generated_at,
    }
    return "pqar_" + _sha256(_canonical(identity))[:24]


def _request_envelope_admissible(request: Mapping[str, Any]) -> bool:
    normalized = dict(request)
    integrity = normalized.pop("integrity", None)
    binding = request.get("binding")
    scope = request.get("scope")
    generated_at = _request_timestamp(request.get("generated_at"))
    expires_at = _request_timestamp(request.get("expires_at"))
    if generated_at is None or expires_at is None:
        return False
    ttl_seconds = (expires_at - generated_at).total_seconds()
    expected_root_keys = {
        "schema",
        "request_id",
        "generated_at",
        "expires_at",
        "status",
        "blocking_reason",
        "next_action",
        "progress",
        "binding",
        "scope",
        "decision_options",
        "excluded_operations",
        "authorization",
        "automatic_execution_allowed",
        "execution_authorized",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "secret_values_recorded",
        "integrity",
    }
    expected_binding_keys = {
        "review_packet_sha256",
        "review_packet_schema",
        "review_packet_generated_at",
        "proposal_sha256",
        "action_sha256",
        "release_head_sha",
        "release_worktree_fingerprint_sha256",
    }
    if not (
        set(request) == expected_root_keys
        and isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
        and request.get("schema") in {SCHEMA, PREVIOUS_SCHEMA, LEGACY_SCHEMA}
        and not (
            request.get("schema") == LEGACY_SCHEMA
            and _scope_kind(scope) != "live_probe_origin"
        )
        and isinstance(binding, dict)
        and set(binding) == expected_binding_keys
        and all(
            _SHA256.fullmatch(str(binding.get(key) or ""))
            for key in (
                "review_packet_sha256",
                "proposal_sha256",
                "action_sha256",
                "release_worktree_fingerprint_sha256",
            )
        )
        and (
            binding.get("review_packet_schema") == runtime_review.SCHEMA
            or (
                request.get("schema") == LEGACY_SCHEMA
                and _LEGACY_REVIEW_PACKET_SCHEMA.fullmatch(
                    str(binding.get("review_packet_schema") or "")
                )
            )
        )
        and _request_timestamp(binding.get("review_packet_generated_at")) is not None
        and re.fullmatch(r"[0-9a-f]{40}", str(binding.get("release_head_sha") or ""))
        and isinstance(scope, dict)
        and bool(_scope_kind(scope))
        and math.isfinite(ttl_seconds)
        and ttl_seconds.is_integer()
        and MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        and request.get("request_id")
        == (
            _request_instance_identifier(
                binding,
                scope,
                generated_at=str(request.get("generated_at") or ""),
            )
            if request.get("schema") == SCHEMA
            else _request_identifier(binding, scope)
        )
        and _REQUEST_ID.fullmatch(str(request.get("request_id") or ""))
        and request.get("status") == "awaiting_explicit_authorization"
        and request.get("blocking_reason") == "explicit_operator_decision_required"
        and str(request.get("next_action") or "").strip()
        and request.get("decision_options")
        == ["approve_exact_scope", "reject", "defer"]
        and request.get("excluded_operations") == _EXCLUDED_OPERATIONS
        and request.get("authorization")
        == {
            "required": True,
            "recorded": False,
            "decision": "pending",
            "decided_at": "",
            "decider_identity_recorded": False,
        }
        and request.get("automatic_execution_allowed") is False
        and request.get("execution_authorized") is False
        and request.get("deployment_or_restart_authorized") is False
        and request.get("protected_operation_executed") is False
        and request.get("provider_quota_consumption_allowed") is False
        and request.get("delivery_authorized") is False
        and request.get("secret_values_recorded") is False
    ):
        return False
    progress = request.get("progress")
    return isinstance(progress, dict) and progress == {
        "review_packet_verified": True,
        "authorization_request_staged": True,
        "authorization_recorded": False,
        "release_worktree_clean": progress.get("release_worktree_clean") is True,
    }


def _stable_request_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(value)
    if projected.get("schema") in {PREVIOUS_SCHEMA, LEGACY_SCHEMA}:
        projected["schema"] = SCHEMA
    projected.pop("integrity", None)
    projected.pop("request_id", None)
    binding = dict(projected.get("binding") or {})
    binding.pop("review_packet_sha256", None)
    binding.pop("review_packet_generated_at", None)
    if _scope_kind(projected.get("scope")) == "local_environment_candidate_merge":
        binding.pop("proposal_sha256", None)
        binding.pop("action_sha256", None)
    projected["binding"] = binding
    return projected


def _review_packet_current(
    *,
    packet_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    deployment_env_path: Path,
    project: str,
    now: datetime,
    max_age_seconds: float,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    verification = runtime_review.verify_current_review_packet(
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        deployment_env_path=deployment_env_path,
        project=project,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    if not (
        verification.get("status") == "verified"
        and verification.get("progress", {}).get("current_evidence_verified") is True
        and verification.get("execution_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        raise ValueError("runtime_review_not_current")
    packet, _raw, digest = _private_object(
        packet_path,
        field="runtime_review_packet",
        maximum_bytes=runtime_review.MAX_PACKET_BYTES,
    )
    if digest != verification.get("packet_sha256"):
        raise ValueError("runtime_review_binding_mismatch")
    return packet, digest, verification


def build_authorization_request(
    *,
    review_packet: Mapping[str, Any],
    review_packet_sha256: str,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    observed_at = _now(now)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        raise ValueError("authorization_request_ttl_invalid")
    review_verification = runtime_review.verify_review_packet(
        review_packet,
        now=observed_at,
        max_age_seconds=runtime_review.DEFAULT_MAX_AGE_SECONDS,
    )
    proposal = review_packet.get("configuration_proposal")
    release = review_packet.get("release_posture")
    if not (
        review_verification.get("status") == "verified"
        and _SHA256.fullmatch(str(review_packet_sha256 or ""))
        and review_packet_sha256 == _sha256(_canonical(review_packet))
        and isinstance(proposal, dict)
        and proposal.get("status") == "review_required"
        and proposal.get("authorization_required") is True
        and proposal.get("execution_authorized") is False
        and proposal.get("provider_quota_consumption_allowed") is False
        and proposal.get("secret_values_recorded") is False
        and isinstance(release, dict)
        and release.get("changed_paths_recorded") is False
    ):
        raise ValueError("runtime_review_not_admissible_for_authorization_request")
    consent_gate = review_packet.get("consent_gate")
    consent_scope = (
        consent_gate.get("authorization_scope")
        if isinstance(consent_gate, Mapping)
        else None
    )
    expected_consent_gate = runtime_review._runtime_consent_gate(proposal)
    local_merge_ready = bool(
        consent_gate == expected_consent_gate
        and isinstance(consent_scope, Mapping)
        and consent_scope.get("operation")
        == "add_missing_local_environment_candidate"
        and consent_scope.get("protected_operations")
        == ["runtime_configuration_change"]
        and consent_gate.get("configuration_merge_authorized") is False
        and consent_gate.get("deployment_or_restart_authorized") is False
        and consent_gate.get("execution_authorized") is False
    )
    legacy_probe_ready = bool(
        proposal.get("probe_target_change_proposed") is True
        and proposal.get("probe_host_change_proposed") is False
    )
    if local_merge_ready:
        scope = {
            "operation": "runtime_configuration_change",
            "change_id": "local_environment_candidate_merge",
            "recovery_preview_sha256": str(
                consent_scope.get("recovery_preview_sha256") or ""
            ),
            "candidate_path": str(consent_scope.get("candidate_path") or ""),
            "candidate_policy_schema": str(
                consent_scope.get("candidate_policy_schema") or ""
            ),
            "candidate_review_keys": list(
                consent_scope.get("candidate_review_keys") or []
            ),
            "candidate_review_key_count": consent_scope.get(
                "candidate_review_key_count"
            ),
            "configuration_merge_policy": str(
                consent_scope.get("configuration_merge_policy") or ""
            ),
            "existing_environment_values_overwrite_allowed": consent_scope.get(
                "existing_environment_values_overwrite_allowed"
            ),
            "rollback_required": consent_scope.get("rollback_required"),
            "compose_project": str(proposal.get("compose_project") or ""),
        }
    elif legacy_probe_ready:
        scope = {
            "operation": "runtime_configuration_change",
            "change_id": "live_probe_origin",
            "current_value": str(proposal.get("current_probe_origin") or ""),
            "proposed_value": str(proposal.get("proposed_probe_origin") or ""),
            "public_host": str(proposal.get("proposed_probe_host") or ""),
            "public_origin": str(proposal.get("release_public_origin") or ""),
            "compose_project": str(proposal.get("compose_project") or ""),
        }
    else:
        raise ValueError("authorization_request_scope_invalid")
    if not _scope_kind(scope):
        raise ValueError("authorization_request_scope_invalid")
    binding = {
        "review_packet_sha256": review_packet_sha256,
        "review_packet_schema": str(review_packet.get("schema") or ""),
        "review_packet_generated_at": str(review_packet.get("generated_at") or ""),
        "proposal_sha256": _sha256(_canonical(proposal)),
        "action_sha256": str(review_packet.get("action_sha256") or ""),
        "release_head_sha": str(release.get("head_sha") or ""),
        "release_worktree_fingerprint_sha256": str(
            release.get("worktree_fingerprint_sha256") or ""
        ),
    }
    generated_at = observed_at.isoformat()
    expires_at = (observed_at + timedelta(seconds=ttl_seconds)).isoformat()
    request_id = _request_instance_identifier(
        binding,
        scope,
        generated_at=generated_at,
    )
    request: dict[str, Any] = {
        "schema": SCHEMA,
        "request_id": request_id,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "status": "awaiting_explicit_authorization",
        "blocking_reason": "explicit_operator_decision_required",
        "next_action": (
            "explicitly approve, reject, or defer only this exact add-missing-keys configuration merge; "
            "deployment or restart remains separately excluded"
            if _scope_kind(scope) == "local_environment_candidate_merge"
            else "explicitly approve, reject, or defer only this exact probe-target configuration intent; "
            "deployment or restart requires a separate fresh authorization after clean release evidence"
        ),
        "progress": {
            "review_packet_verified": True,
            "authorization_request_staged": True,
            "authorization_recorded": False,
            "release_worktree_clean": release.get("worktree_clean") is True,
        },
        "binding": binding,
        "scope": scope,
        "decision_options": ["approve_exact_scope", "reject", "defer"],
        "excluded_operations": list(_EXCLUDED_OPERATIONS),
        "authorization": {
            "required": True,
            "recorded": False,
            "decision": "pending",
            "decided_at": "",
            "decider_identity_recorded": False,
        },
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "secret_values_recorded": False,
    }
    request["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(request)),
    }
    return request


def verify_authorization_request(
    request: Mapping[str, Any],
    *,
    review_packet: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized = dict(request)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(str(integrity.get("canonical_payload_sha256") or ""))
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
    ):
        return _blocked("authorization_request_integrity_invalid", now=observed_now)
    try:
        generated_at = datetime.fromisoformat(
            str(request.get("generated_at") or "").replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            str(request.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return _blocked("authorization_request_timestamp_invalid", now=observed_now)
    if generated_at.tzinfo is None or expires_at.tzinfo is None:
        return _blocked("authorization_request_timestamp_invalid", now=observed_now)
    generated_at = generated_at.astimezone(timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)
    ttl_seconds = (expires_at - generated_at).total_seconds()
    age_seconds = (observed_now - generated_at).total_seconds()
    if not (
        math.isfinite(ttl_seconds)
        and ttl_seconds.is_integer()
        and MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        and math.isfinite(age_seconds)
        and age_seconds >= -30.0
        and observed_now <= expires_at
    ):
        return _blocked("authorization_request_not_fresh", now=observed_now)
    try:
        expected = build_authorization_request(
            review_packet=review_packet,
            review_packet_sha256=_sha256(_canonical(review_packet)),
            now=observed_now,
            ttl_seconds=int(ttl_seconds),
        )
        expected["generated_at"] = generated_at.isoformat()
        expected["expires_at"] = expires_at.isoformat()
    except (TypeError, ValueError):
        return _blocked("authorization_request_review_not_admissible", now=observed_now)
    if not (
        _request_envelope_admissible(request)
        and _stable_request_projection(request)
        == _stable_request_projection(expected)
    ):
        return _blocked("authorization_request_contract_not_admissible", now=observed_now)
    current_review_packet_sha256 = _sha256(_canonical(review_packet))
    exact_packet_binding = (
        request.get("binding", {}).get("review_packet_sha256")
        == current_review_packet_sha256
    )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "explicit_operator_decision_required",
        "next_action": str(request.get("next_action") or ""),
        "progress": {
            "current_evidence_verified": False,
            "authorization_request_verified": True,
            "authorization_recorded": False,
            "release_worktree_clean": request.get("progress", {}).get(
                "release_worktree_clean"
            )
            is True,
        },
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": _sha256(_canonical(request)),
        "expires_at": str(request.get("expires_at") or ""),
        "scope": dict(request.get("scope") or {}),
        "binding_mode": (
            "exact_packet" if exact_packet_binding else "stable_scope_current_evidence"
        ),
        "review_packet_binding_current": exact_packet_binding,
        "current_review_packet_sha256": current_review_packet_sha256,
        "authorization_required": True,
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def materialize_current_authorization_request(
    *,
    request_path: Path = DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    project: str = "property",
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    packet, digest, _verification = _review_packet_current(
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        deployment_env_path=deployment_env_path,
        project=project,
        now=observed_now,
        max_age_seconds=runtime_review.DEFAULT_MAX_AGE_SECONDS,
    )
    request = build_authorization_request(
        review_packet=packet,
        review_packet_sha256=digest,
        now=observed_now,
        ttl_seconds=ttl_seconds,
    )
    atomic_write_bytes(
        runtime_review._rooted(request_path),
        _canonical(request),
        overwrite=True,
    )
    return request


def verify_current_authorization_request(
    *,
    request_path: Path = DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    project: str = "property",
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        request, _raw, _digest = _private_object(
            request_path,
            field="runtime_authorization_request",
            maximum_bytes=MAX_REQUEST_BYTES,
        )
        packet, packet_digest, _verification = _review_packet_current(
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            deployment_env_path=deployment_env_path,
            project=project,
            now=observed_now,
            max_age_seconds=runtime_review.DEFAULT_MAX_AGE_SECONDS,
        )
    except Exception:
        return _blocked("authorization_request_current_evidence_unavailable", now=observed_now)
    verification = verify_authorization_request(
        request,
        review_packet=packet,
        now=observed_now,
    )
    if verification.get("status") != "verified":
        return verification
    if verification.get("current_review_packet_sha256") != packet_digest:
        return _blocked("authorization_request_current_binding_mismatch", now=observed_now)
    verification["progress"]["current_evidence_verified"] = True
    return verification


def _handoff_blocked(
    reason: str,
    *,
    request_path: Path,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": "repair or refresh the current evidence before requesting an explicit operator decision",
        "request_path": str(request_path),
        "request_state": "unavailable",
        "request_state_updated": False,
        "current_evidence_verified": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _ready_handoff(
    verification: Mapping[str, Any],
    *,
    request_path: Path,
    request_state: str,
    refresh_reason: str,
    previous_request_sha256: str,
    request_state_updated: bool,
    now: datetime,
) -> dict[str, Any]:
    expires_at = _request_timestamp(verification.get("expires_at"))
    if not (
        verification.get("status") == "verified"
        and expires_at is not None
        and expires_at >= now
        and verification.get("authorization_recorded") is False
        and verification.get("execution_authorized") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        return _handoff_blocked(
            "authorization_handoff_verification_not_admissible",
            request_path=request_path,
            now=now,
        )
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "ready",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_operator_decision_required",
        "next_action": str(verification.get("next_action") or ""),
        "request_path": str(request_path),
        "request_state": request_state,
        "request_refresh_reason": refresh_reason,
        "request_state_updated": request_state_updated,
        "previous_request_sha256": previous_request_sha256,
        "request_id": str(verification.get("request_id") or ""),
        "request_sha256": str(verification.get("request_sha256") or ""),
        "expires_at": expires_at.isoformat(),
        "remaining_seconds": max(0, int((expires_at - now).total_seconds())),
        "scope": dict(verification.get("scope") or {}),
        "decision_options": ["approve_exact_scope", "reject", "defer"],
        "binding_mode": str(verification.get("binding_mode") or ""),
        "current_review_packet_sha256": str(
            verification.get("current_review_packet_sha256") or ""
        ),
        "current_evidence_verified": True,
        "authorization_required": True,
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def stage_current_authorization_handoff(
    *,
    request_path: Path = DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    deployment_env_path: Path = runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    project: str = "property",
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    refresh_before_seconds: int = DEFAULT_REFRESH_BEFORE_SECONDS,
) -> dict[str, Any]:
    """Keep one exact-scope decision handoff usable without granting authority."""

    observed_now = _now(now)
    target = runtime_review._rooted(request_path)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        or isinstance(refresh_before_seconds, bool)
        or not isinstance(refresh_before_seconds, int)
        or not 0 <= refresh_before_seconds < ttl_seconds
    ):
        return _handoff_blocked(
            "authorization_handoff_refresh_policy_invalid",
            request_path=target,
            now=observed_now,
        )
    try:
        packet, packet_digest, _review_verification = _review_packet_current(
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            deployment_env_path=deployment_env_path,
            project=project,
            now=observed_now,
            max_age_seconds=runtime_review.DEFAULT_MAX_AGE_SECONDS,
        )
    except Exception:
        return _handoff_blocked(
            "authorization_handoff_current_evidence_unavailable",
            request_path=target,
            now=observed_now,
        )
    prior_request_sha256 = ""
    refresh_reason = "missing"
    try:
        prior_request, _raw, prior_request_sha256 = _private_object(
            target,
            field="runtime_authorization_request",
            maximum_bytes=MAX_REQUEST_BYTES,
        )
    except FileNotFoundError:
        prior_request = None
    except Exception:
        return _handoff_blocked(
            "authorization_handoff_existing_request_not_admissible",
            request_path=target,
            now=observed_now,
        )
    if prior_request is not None:
        verification = verify_authorization_request(
            prior_request,
            review_packet=packet,
            now=observed_now,
        )
        expires_at = _request_timestamp(prior_request.get("expires_at"))
        remaining_seconds = (
            int((expires_at - observed_now).total_seconds())
            if expires_at is not None
            else -1
        )
        if (
            verification.get("status") == "verified"
            and verification.get("current_review_packet_sha256") == packet_digest
            and remaining_seconds >= 0
        ):
            verification["progress"]["current_evidence_verified"] = True
            return _ready_handoff(
                verification,
                request_path=target,
                request_state="reused",
                refresh_reason="",
                previous_request_sha256="",
                request_state_updated=False,
                now=observed_now,
            )
        if not _request_envelope_admissible(prior_request):
            return _handoff_blocked(
                "authorization_handoff_existing_request_not_admissible",
                request_path=target,
                now=observed_now,
            )
        refresh_reason = (
            "near_expiry"
            if verification.get("status") == "verified"
            else "expired"
            if verification.get("blocking_reason") == "authorization_request_not_fresh"
            else "superseded"
        )

    try:
        request = build_authorization_request(
            review_packet=packet,
            review_packet_sha256=packet_digest,
            now=observed_now,
            ttl_seconds=ttl_seconds,
        )
        atomic_write_bytes(target, _canonical(request), overwrite=True)
        persisted, _persisted_raw, persisted_sha256 = _private_object(
            target,
            field="runtime_authorization_request",
            maximum_bytes=MAX_REQUEST_BYTES,
        )
    except Exception:
        return _handoff_blocked(
            "authorization_handoff_request_write_failed",
            request_path=target,
            now=observed_now,
        )
    verification = verify_authorization_request(
        persisted,
        review_packet=packet,
        now=observed_now,
    )
    if not (
        persisted == request
        and verification.get("status") == "verified"
        and verification.get("request_sha256") == persisted_sha256
        and verification.get("current_review_packet_sha256") == packet_digest
        and verification.get("review_packet_binding_current") is True
    ):
        return _handoff_blocked(
            "authorization_handoff_request_write_not_verified",
            request_path=target,
            now=observed_now,
        )
    verification["progress"]["current_evidence_verified"] = True
    return _ready_handoff(
        verification,
        request_path=target,
        request_state="refreshed",
        refresh_reason=refresh_reason,
        previous_request_sha256=prior_request_sha256,
        request_state_updated=True,
        now=observed_now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage or verify a non-executing exact-scope PropertyQuarry authorization request."
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST_PATH)
    parser.add_argument(
        "--verification-write",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument("--packet", type=Path, default=runtime_review.DEFAULT_PACKET_PATH)
    parser.add_argument("--cycle-receipt", type=Path, default=runtime_review.DEFAULT_CYCLE_RECEIPT)
    parser.add_argument("--signal-dir", type=Path, default=runtime_review.DEFAULT_SIGNAL_DIR)
    parser.add_argument(
        "--live-mobile-receipt",
        type=Path,
        default=runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    )
    parser.add_argument(
        "--release-manifest",
        type=Path,
        default=runtime_review.DEFAULT_RELEASE_MANIFEST,
    )
    parser.add_argument(
        "--deployment-env",
        type=Path,
        default=runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH,
    )
    parser.add_argument("--project", default="property")
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.verify_current:
            result = verify_current_authorization_request(
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                project=args.project,
            )
        else:
            request = materialize_current_authorization_request(
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                project=args.project,
                ttl_seconds=args.ttl_seconds,
            )
            result = verify_current_authorization_request(
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                deployment_env_path=args.deployment_env,
                project=args.project,
            )
            result["request_generated_at"] = str(request.get("generated_at") or "")
    except Exception:
        result = _blocked("authorization_request_materialization_failed", now=None)
    result["request_path"] = str(args.request)
    result["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            runtime_review._rooted(args.verification_write),
            _canonical(result),
            overwrite=True,
        )
    except Exception:
        result["verification_receipt_persisted"] = False
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
