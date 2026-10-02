#!/usr/bin/env python3
"""Stage and verify an exact PropertyQuarry config plan without applying it."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_authorization_decision as authorization_decision
from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_runtime_configuration_plan.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_runtime_configuration_plan_verification.v1"
HANDOFF_SCHEMA = "propertyquarry.ooda_runtime_configuration_handoff.v1"
DEFAULT_PLAN_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-plan.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-plan-verification.json"
)
MAX_PLAN_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
COMPOSE_PATH = Path("docker-compose.property.yml")
ISOLATION_PATH = Path("scripts/propertyquarry_runtime_isolation_v2.py")
DEPLOYMENT_AUDIT_PATH = Path("scripts/propertyquarry_local_deployment_receipt.py")
LIVE_SMOKE_PATH = Path("scripts/propertyquarry_live_mobile_surface_smoke.py")
CURRENT_PORT_EXPRESSION = (
    "${EA_HOST_BIND:-127.0.0.1}:${EA_HOST_PORT:-8090}:8090"
)
PROPOSED_PORT_EXPRESSION = (
    "${EA_HOST_BIND:-127.0.0.1}:${EA_HOST_PORT:-8097}:8090"
)
SUPPORTING_CONTRACTS = (
    (
        "runtime_isolation",
        ISOLATION_PATH,
        'API_HOST_PORT_KEY = "EA_HOST_PORT"',
        'root_values.get(API_HOST_PORT_KEY) != "8097"',
    ),
    (
        "deployment_audit",
        DEPLOYMENT_AUDIT_PATH,
        'DEFAULT_LOCAL_ORIGIN: Final = "http://127.0.0.1:8097"',
        "",
    ),
    (
        "live_mobile_smoke",
        LIVE_SMOKE_PATH,
        '_env("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097")',
        "",
    ),
)
VALIDATION_STEPS = [
    "docker compose config must resolve propertyquarry-api host bind to 127.0.0.1:8097",
    "runtime isolation inputs must retain EA_HOST_BIND=127.0.0.1 and EA_HOST_PORT=8097",
    "rerun the approved evaluate-only OODA chain after any source edit",
]
LOCAL_ENVIRONMENT_VALIDATION_STEPS = [
    "reinspect the private target and candidate under one exclusive local apply lock",
    "add exactly the requested missing keys without overwriting any existing environment value",
    "persist a private pre-apply rollback snapshot before atomic target replacement",
    "verify required-key presence without recording or hashing environment values",
    "rerun the approved evaluate-only OODA chain without deploying or restarting",
]


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
        "next_action": "regenerate the plan from a fresh request and current source files",
        "progress": {
            "current_evidence_verified": False,
            "configuration_plan_verified": False,
            "authorization_decision_recorded": False,
        },
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _source_posture_admissible(
    scope: Mapping[str, Any],
    source_posture: object,
) -> bool:
    if not isinstance(source_posture, Mapping):
        return False
    posture_payload = dict(source_posture)
    posture_fingerprint = posture_payload.pop("fingerprint_sha256", None)
    if not (
        source_posture.get("status") == "ready"
        and source_posture.get("secret_values_recorded") is False
        and authorization_request._SHA256.fullmatch(
            str(posture_fingerprint or "")
        )
        and posture_fingerprint == _sha256(_canonical(posture_payload))
    ):
        return False
    target = source_posture.get("target")
    kind = authorization_request._scope_kind(scope)
    if kind == "live_probe_origin":
        supporting = source_posture.get("supporting_contracts")
        if not (
            source_posture.get("root") == "repository"
            and isinstance(target, Mapping)
            and set(target)
            == {
                "path",
                "selector",
                "sha256",
                "bytes",
                "current_expression",
                "proposed_expression",
                "current_expression_count",
                "proposed_expression_count",
                "change_required",
            }
            and target.get("path") == COMPOSE_PATH.as_posix()
            and target.get("selector")
            == "services.propertyquarry-api.ports[0]"
            and authorization_request._SHA256.fullmatch(
                str(target.get("sha256") or "")
            )
            and isinstance(target.get("bytes"), int)
            and int(target.get("bytes") or 0) > 0
            and target.get("current_expression") == CURRENT_PORT_EXPRESSION
            and target.get("proposed_expression") == PROPOSED_PORT_EXPRESSION
            and (
                target.get("current_expression_count"),
                target.get("proposed_expression_count"),
            )
            in {(1, 0), (0, 1)}
            and target.get("change_required")
            is (target.get("current_expression_count") == 1)
            and isinstance(supporting, list)
            and len(supporting) == len(SUPPORTING_CONTRACTS)
        ):
            return False
        return all(
            isinstance(row, Mapping)
            and set(row)
            == {
                "id",
                "path",
                "sha256",
                "bytes",
                "expected_host_port",
                "status",
            }
            and row.get("id") == contract_id
            and row.get("path") == path.as_posix()
            and authorization_request._SHA256.fullmatch(
                str(row.get("sha256") or "")
            )
            and isinstance(row.get("bytes"), int)
            and int(row.get("bytes") or 0) > 0
            and row.get("expected_host_port") == 8097
            and row.get("status") == "aligned"
            for row, (contract_id, path, _first, _second) in zip(
                supporting,
                SUPPORTING_CONTRACTS,
                strict=True,
            )
        )
    if kind == "local_environment_candidate_merge":
        candidate = source_posture.get("candidate")
        review_keys = list(scope.get("candidate_review_keys") or [])
        return bool(
            source_posture.get("root") == "private_environment_layers"
            and source_posture.get("environment_values_recorded") is False
            and source_posture.get("environment_values_hashed") is False
            and source_posture.get("recovery_preview_sha256")
            == scope.get("recovery_preview_sha256")
            and isinstance(target, Mapping)
            and set(target)
            == {
                "path",
                "file_mode",
                "missing_keys",
                "missing_key_count",
                "configuration_merge_policy",
                "existing_environment_values_overwrite_allowed",
                "change_required",
            }
            and target.get("path")
            == runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix()
            and target.get("file_mode") == 0o600
            and target.get("missing_keys") == review_keys
            and target.get("missing_key_count") == len(review_keys)
            and target.get("configuration_merge_policy")
            == "add_missing_keys_only"
            and target.get("existing_environment_values_overwrite_allowed")
            is False
            and target.get("change_required") is bool(review_keys)
            and isinstance(candidate, Mapping)
            and set(candidate)
            == {
                "path",
                "schema",
                "file_mode",
                "status",
                "review_keys",
                "review_key_count",
                "candidate_matches_current_derivation",
                "candidate_secret_policy_verified",
                "candidate_values_recorded",
                "candidate_values_hashed",
            }
            and candidate.get("path") == scope.get("candidate_path")
            and candidate.get("schema") == scope.get("candidate_policy_schema")
            and candidate.get("file_mode") == 0o600
            and candidate.get("status") == "ready_for_merge_review"
            and candidate.get("review_keys") == review_keys
            and candidate.get("review_key_count") == len(review_keys)
            and candidate.get("candidate_matches_current_derivation") is True
            and candidate.get("candidate_secret_policy_verified") is True
            and candidate.get("candidate_values_recorded") is False
            and candidate.get("candidate_values_hashed") is False
        )
    return False


def _change_contract(
    scope: Mapping[str, Any],
    source_posture: Mapping[str, Any],
) -> dict[str, Any]:
    target = dict(source_posture.get("target") or {})
    if authorization_request._scope_kind(scope) == "live_probe_origin":
        change_required = target.get("change_required") is True
        return {
            "operation": "replace_exact_text",
            "path": target.get("path"),
            "selector": target.get("selector"),
            "expected_before_sha256": target.get("sha256"),
            "current_expression": CURRENT_PORT_EXPRESSION,
            "proposed_expression": PROPOSED_PORT_EXPRESSION,
            "change_required": change_required,
            "replacement_count": 1 if change_required else 0,
            "rollback_expression": CURRENT_PORT_EXPRESSION,
        }
    candidate = dict(source_posture.get("candidate") or {})
    review_keys = list(scope.get("candidate_review_keys") or [])
    return {
        "operation": "merge_dotenv_add_missing_keys",
        "target_path": target.get("path"),
        "candidate_path": candidate.get("path"),
        "candidate_policy_schema": candidate.get("schema"),
        "review_keys": review_keys,
        "review_key_count": len(review_keys),
        "configuration_merge_policy": "add_missing_keys_only",
        "existing_environment_values_overwrite_allowed": False,
        "change_required": target.get("change_required") is True,
        "rollback_policy": "restore_private_pre_apply_snapshot",
        "environment_values_recorded": False,
        "environment_values_hashed": False,
    }


def _validation_steps(scope: Mapping[str, Any]) -> list[str]:
    return list(
        LOCAL_ENVIRONMENT_VALIDATION_STEPS
        if authorization_request._scope_kind(scope)
        == "local_environment_candidate_merge"
        else VALIDATION_STEPS
    )


def _plan_next_action(scope: Mapping[str, Any], decision: str) -> str:
    local_merge = authorization_request._scope_kind(scope) == (
        "local_environment_candidate_merge"
    )
    if decision == "pending":
        return "record approve_exact_scope, reject, or defer for this exact plan"
    if decision == "approve_exact_scope":
        return (
            "manually apply only the exact add-missing-keys dotenv merge, then "
            "regenerate all bound evidence; do not deploy or restart"
            if local_merge
            else "manually apply only the exact one-line replacement, then regenerate all bound evidence; "
            "do not deploy or restart"
        )
    if decision == "reject":
        return (
            "leave the deployment environment unchanged"
            if local_merge
            else "leave the Compose host-port fallback unchanged"
        )
    return "leave the source unchanged and regenerate fresh evidence when ready"


def _plan_envelope_admissible(plan: Mapping[str, Any]) -> bool:
    normalized = dict(plan)
    integrity = normalized.pop("integrity", None)
    generated_at = _parse_timestamp(plan.get("generated_at"))
    expires_at = _parse_timestamp(plan.get("expires_at"))
    binding = plan.get("binding")
    scope = plan.get("scope")
    source_posture = plan.get("source_posture")
    change = plan.get("change")
    authorization = plan.get("authorization")
    expected_root_keys = {
        "schema",
        "plan_id",
        "generated_at",
        "expires_at",
        "status",
        "next_action",
        "binding",
        "scope",
        "source_posture",
        "change",
        "validation",
        "authorization",
        "manual_apply_authorized",
        "automatic_apply_allowed",
        "apply_performed",
        "execution_authorized",
        "execution_performed",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "secret_values_recorded",
        "integrity",
    }
    if not (
        set(plan) == expected_root_keys
        and isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and authorization_request._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
        and generated_at is not None
        and expires_at is not None
        and generated_at <= expires_at
        and plan.get("schema") == SCHEMA
        and isinstance(binding, dict)
        and set(binding)
        == {
            "request_id",
            "request_sha256",
            "review_packet_sha256",
            "proposal_sha256",
            "source_fingerprint_sha256",
        }
        and authorization_request._REQUEST_ID.fullmatch(
            str(binding.get("request_id") or "")
        )
        and all(
            authorization_request._SHA256.fullmatch(str(binding.get(key) or ""))
            for key in (
                "request_sha256",
                "review_packet_sha256",
                "proposal_sha256",
                "source_fingerprint_sha256",
            )
        )
        and isinstance(scope, dict)
        and bool(authorization_request._scope_kind(scope))
        and _source_posture_admissible(scope, source_posture)
        and source_posture.get("fingerprint_sha256")
        == binding.get("source_fingerprint_sha256")
        and plan.get("validation") == _validation_steps(scope)
        and isinstance(change, dict)
        and isinstance(authorization, dict)
        and set(authorization)
        == {
            "required",
            "recorded",
            "decision",
            "exact_scope_authorized",
        }
        and authorization.get("required") is True
        and isinstance(authorization.get("recorded"), bool)
        and isinstance(authorization.get("exact_scope_authorized"), bool)
        and plan.get("automatic_apply_allowed") is False
        and plan.get("apply_performed") is False
        and plan.get("execution_authorized") is False
        and plan.get("execution_performed") is False
        and plan.get("deployment_or_restart_authorized") is False
        and plan.get("protected_operation_executed") is False
        and plan.get("provider_quota_consumption_allowed") is False
        and plan.get("delivery_authorized") is False
        and plan.get("secret_values_recorded") is False
    ):
        return False
    expected_change = _change_contract(scope, source_posture)
    if change != expected_change:
        return False
    change_required = expected_change.get("change_required") is True
    recorded = authorization.get("recorded") is True
    decision = str(authorization.get("decision") or "")
    exact_scope_authorized = authorization.get("exact_scope_authorized") is True
    if not (
        decision in {"pending", "approve_exact_scope", "reject", "defer"}
        and recorded is (decision != "pending")
        and exact_scope_authorized is (decision == "approve_exact_scope")
    ):
        return False
    expected_status = {
        "pending": "review_ready",
        "approve_exact_scope": "exact_scope_authorized",
        "reject": "rejected",
        "defer": "deferred",
    }[decision]
    expected_next_action = _plan_next_action(scope, decision)
    decision_status = "verified" if recorded else "pending"
    expected_plan_id = "pqcp_" + _sha256(
        _canonical(
            {
                "binding": binding,
                "scope": scope,
                "decision_status": decision_status,
                "decision_value": decision,
            }
        )
    )[:24]
    return bool(
        plan.get("plan_id") == expected_plan_id
        and plan.get("status") == expected_status
        and plan.get("next_action") == expected_next_action
        and plan.get("manual_apply_authorized")
        is (exact_scope_authorized and change_required)
    )


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _source_snapshot(path: Path, *, root: Path) -> tuple[str, bytes, str]:
    expected_root = root.resolve(strict=True)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("configuration_source_path_invalid")
    target = expected_root / path
    parent_before = target.parent.lstat()
    if (
        not stat.S_ISDIR(parent_before.st_mode)
        or stat.S_ISLNK(parent_before.st_mode)
        or parent_before.st_uid not in {0, os.geteuid()}
        or parent_before.st_mode & stat.S_IWOTH
        or parent_before.st_mode & stat.S_IWGRP
        and parent_before.st_gid != os.getegid()
    ):
        raise ValueError("configuration_source_parent_not_admissible")
    descriptor = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid not in {0, os.geteuid()}
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= MAX_SOURCE_BYTES
        ):
            raise ValueError("configuration_source_file_not_admissible")
        raw = bytearray()
        while len(raw) < opened.st_size:
            chunk = os.read(descriptor, min(65536, opened.st_size - len(raw)))
            if not chunk:
                raise ValueError("configuration_source_short_read")
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named = target.lstat()
        parent_after = target.parent.lstat()
    finally:
        os.close(descriptor)
    if (
        _file_identity(opened) != _file_identity(after)
        or _file_identity(opened) != _file_identity(named)
        or _directory_identity(parent_before) != _directory_identity(parent_after)
    ):
        raise ValueError("configuration_source_changed_during_read")
    raw_bytes = bytes(raw)
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("configuration_source_utf8_invalid") from exc
    return text, raw_bytes, _sha256(raw_bytes)


def _inspect_legacy_configuration_sources(*, root: Path = ROOT) -> dict[str, Any]:
    compose_text, compose_raw, compose_sha256 = _source_snapshot(
        COMPOSE_PATH,
        root=root,
    )
    current_count = compose_text.count(CURRENT_PORT_EXPRESSION)
    proposed_count = compose_text.count(PROPOSED_PORT_EXPRESSION)
    if (current_count, proposed_count) not in {(1, 0), (0, 1)}:
        raise ValueError("compose_host_port_contract_not_admissible")
    supporting: list[dict[str, Any]] = []
    for contract_id, path, first, second in SUPPORTING_CONTRACTS:
        text, raw, digest = _source_snapshot(path, root=root)
        required_fragments = [value for value in (first, second) if value]
        if any(text.count(fragment) != 1 for fragment in required_fragments):
            raise ValueError(f"{contract_id}_port_contract_not_admissible")
        supporting.append(
            {
                "id": contract_id,
                "path": path.as_posix(),
                "sha256": digest,
                "bytes": len(raw),
                "expected_host_port": 8097,
                "status": "aligned",
            }
        )
    posture: dict[str, Any] = {
        "status": "ready",
        "root": "repository",
        "target": {
            "path": COMPOSE_PATH.as_posix(),
            "selector": "services.propertyquarry-api.ports[0]",
            "sha256": compose_sha256,
            "bytes": len(compose_raw),
            "current_expression": CURRENT_PORT_EXPRESSION,
            "proposed_expression": PROPOSED_PORT_EXPRESSION,
            "current_expression_count": current_count,
            "proposed_expression_count": proposed_count,
            "change_required": current_count == 1,
        },
        "supporting_contracts": supporting,
        "secret_values_recorded": False,
    }
    posture["fingerprint_sha256"] = _sha256(_canonical(posture))
    return posture


def _inspect_local_environment_merge_sources(
    scope: Mapping[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    if authorization_request._scope_kind(scope) != (
        "local_environment_candidate_merge"
    ):
        raise ValueError("local_environment_merge_scope_not_admissible")
    environment = runtime_review.inspect_deployment_environment(root=root)
    intake = environment.get("intake_plan")
    candidate = (
        intake.get("local_runtime_binding_candidate")
        if isinstance(intake, Mapping)
        else None
    )
    review_keys = list(scope.get("candidate_review_keys") or [])
    layers = list(environment.get("environment_layers") or [])
    target_layer = layers[0] if layers and isinstance(layers[0], Mapping) else None
    if not (
        environment.get("source")
        == "private_dotenv_layer_presence_inspection"
        and environment.get("status") == "incomplete"
        and environment.get("file_status") == "admissible"
        and environment.get("inspection_complete") is True
        and environment.get("path")
        == runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix()
        and environment.get("missing_required_keys") == review_keys
        and environment.get("missing_required_key_count") == len(review_keys)
        and environment.get("unresolved_required_key_count") == 0
        and environment.get("environment_values_recorded") is False
        and environment.get("environment_values_hashed") is False
        and environment.get("secret_values_recorded") is False
        and isinstance(intake, Mapping)
        and intake.get("status") == "local_merge_review_required"
        and intake.get("local_merge_review_required") is True
        and intake.get("local_merge_review_keys") == review_keys
        and intake.get("local_merge_review_key_count") == len(review_keys)
        and intake.get("locally_pending_key_count") == 0
        and intake.get("configuration_apply_authorized") is False
        and intake.get("deployment_or_restart_authorized") is False
        and isinstance(candidate, Mapping)
        and candidate.get("status") == "ready_for_merge_review"
        and candidate.get("candidate_ready_for_merge_review") is True
        and candidate.get("candidate_matches_current_derivation") is True
        and candidate.get("candidate_secret_policy_verified") is True
        and candidate.get("path") == scope.get("candidate_path")
        and candidate.get("schema") == scope.get("candidate_policy_schema")
        and candidate.get("required_keys") == review_keys
        and candidate.get("required_key_count") == len(review_keys)
        and candidate.get("candidate_values_hashed") is False
        and candidate.get("candidate_values_recorded_in_receipt") is False
        and candidate.get("secret_values_recorded") is False
        and isinstance(target_layer, Mapping)
        and target_layer.get("path")
        == runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix()
        and target_layer.get("file_status") == "admissible"
        and target_layer.get("inspection_complete") is True
        and target_layer.get("environment_values_hashed") is False
        and target_layer.get("environment_values_recorded") is False
        and target_layer.get("secret_values_recorded") is False
    ):
        raise ValueError("local_environment_merge_sources_not_admissible")
    posture: dict[str, Any] = {
        "status": "ready",
        "root": "private_environment_layers",
        "target": {
            "path": runtime_review.DEFAULT_DEPLOYMENT_ENV_PATH.as_posix(),
            "file_mode": target_layer.get("file_mode"),
            "missing_keys": review_keys,
            "missing_key_count": len(review_keys),
            "configuration_merge_policy": "add_missing_keys_only",
            "existing_environment_values_overwrite_allowed": False,
            "change_required": bool(review_keys),
        },
        "candidate": {
            "path": str(candidate.get("path") or ""),
            "schema": str(candidate.get("schema") or ""),
            "file_mode": candidate.get("file_mode"),
            "status": "ready_for_merge_review",
            "review_keys": review_keys,
            "review_key_count": len(review_keys),
            "candidate_matches_current_derivation": True,
            "candidate_secret_policy_verified": True,
            "candidate_values_recorded": False,
            "candidate_values_hashed": False,
        },
        "recovery_preview_sha256": str(
            scope.get("recovery_preview_sha256") or ""
        ),
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    posture["fingerprint_sha256"] = _sha256(_canonical(posture))
    return posture


def inspect_configuration_sources(
    *,
    root: Path = ROOT,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if authorization_request._scope_kind(scope) == (
        "local_environment_candidate_merge"
    ):
        return _inspect_local_environment_merge_sources(
            dict(scope or {}),
            root=root,
        )
    return _inspect_legacy_configuration_sources(root=root)


def build_configuration_plan(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    request_verification: Mapping[str, Any],
    decision_verification: Mapping[str, Any],
    source_posture: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _now(now)
    request_expires_at = _parse_timestamp(request.get("expires_at"))
    scope = request.get("scope")
    target = source_posture.get("target")
    decision_status = str(decision_verification.get("status") or "")
    decision_value = str(decision_verification.get("decision") or "pending")
    if not (
        request_verification.get("status") == "verified"
        and request_verification.get("progress", {}).get("current_evidence_verified")
        is True
        and request_verification.get("authorization_recorded") is False
        and request_verification.get("execution_authorized") is False
        and request_verification.get("deployment_or_restart_authorized") is False
        and request_verification.get("provider_quota_consumption_allowed") is False
        and request_verification.get("delivery_authorized") is False
        and request_sha256 == _sha256(_canonical(request))
        and request_expires_at is not None
        and generated_at <= request_expires_at
        and isinstance(scope, dict)
        and bool(authorization_request._scope_kind(scope))
        and _source_posture_admissible(scope, source_posture)
        and isinstance(target, Mapping)
        and decision_status in {"pending", "verified"}
        and decision_verification.get("progress", {}).get(
            "current_evidence_verified"
        )
        is True
        and decision_verification.get("request_id") == request.get("request_id")
        and decision_verification.get("request_sha256") == request_sha256
        and decision_verification.get("automatic_execution_allowed") is False
        and decision_verification.get("execution_authorized") is False
        and decision_verification.get("execution_performed") is False
        and decision_verification.get("deployment_or_restart_authorized") is False
        and decision_verification.get("provider_quota_consumption_allowed") is False
        and decision_verification.get("delivery_authorized") is False
    ):
        raise ValueError("configuration_plan_input_not_admissible")
    decision_recorded = decision_status == "verified"
    exact_scope_authorized = (
        decision_recorded
        and decision_value == "approve_exact_scope"
        and decision_verification.get("exact_scope_authorized") is True
    )
    change = _change_contract(scope, source_posture)
    change_required = change.get("change_required") is True
    binding = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": request_sha256,
        "review_packet_sha256": str(
            request.get("binding", {}).get("review_packet_sha256") or ""
        ),
        "proposal_sha256": str(
            request.get("binding", {}).get("proposal_sha256") or ""
        ),
        "source_fingerprint_sha256": str(
            source_posture.get("fingerprint_sha256") or ""
        ),
    }
    plan_digest = _sha256(
        _canonical(
            {
                "binding": binding,
                "scope": scope,
                "decision_status": decision_status,
                "decision_value": decision_value,
            }
        )
    )
    if decision_value == "reject":
        status = "rejected"
    elif decision_value == "defer":
        status = "deferred"
    elif exact_scope_authorized:
        status = "exact_scope_authorized"
    else:
        status = "review_ready"
    next_action = _plan_next_action(scope, decision_value)
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "plan_id": f"pqcp_{plan_digest[:24]}",
        "generated_at": generated_at.isoformat(),
        "expires_at": request_expires_at.isoformat(),
        "status": status,
        "next_action": next_action,
        "binding": binding,
        "scope": dict(scope),
        "source_posture": dict(source_posture),
        "change": change,
        "validation": _validation_steps(scope),
        "authorization": {
            "required": True,
            "recorded": decision_recorded,
            "decision": decision_value,
            "exact_scope_authorized": exact_scope_authorized,
        },
        "manual_apply_authorized": exact_scope_authorized and change_required,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "secret_values_recorded": False,
    }
    plan["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(plan)),
    }
    return plan


def verify_configuration_plan(
    plan: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_verification: Mapping[str, Any],
    decision_verification: Mapping[str, Any],
    source_posture: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized = dict(plan)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and authorization_request._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
    ):
        return _blocked("configuration_plan_integrity_invalid", now=observed_now)
    generated_at = _parse_timestamp(plan.get("generated_at"))
    expires_at = _parse_timestamp(plan.get("expires_at"))
    if generated_at is None or expires_at is None:
        return _blocked("configuration_plan_timestamp_invalid", now=observed_now)
    age_seconds = (observed_now - generated_at).total_seconds()
    if not (
        math.isfinite(age_seconds)
        and age_seconds >= -30.0
        and generated_at <= expires_at
        and observed_now <= expires_at
    ):
        return _blocked("configuration_plan_not_fresh", now=observed_now)
    try:
        expected = build_configuration_plan(
            request=request,
            request_sha256=_sha256(_canonical(request)),
            request_verification=request_verification,
            decision_verification=decision_verification,
            source_posture=source_posture,
            now=generated_at,
        )
    except (TypeError, ValueError):
        return _blocked("configuration_plan_inputs_not_admissible", now=observed_now)
    if not (
        dict(plan) == expected
        and plan.get("automatic_apply_allowed") is False
        and plan.get("apply_performed") is False
        and plan.get("execution_authorized") is False
        and plan.get("execution_performed") is False
        and plan.get("deployment_or_restart_authorized") is False
        and plan.get("protected_operation_executed") is False
        and plan.get("provider_quota_consumption_allowed") is False
        and plan.get("delivery_authorized") is False
        and plan.get("secret_values_recorded") is False
    ):
        return _blocked("configuration_plan_contract_not_admissible", now=observed_now)
    authorization = dict(plan.get("authorization") or {})
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(plan.get("next_action") or ""),
        "progress": {
            "current_evidence_verified": False,
            "configuration_plan_verified": True,
            "authorization_decision_recorded": authorization.get("recorded") is True,
        },
        "plan_id": str(plan.get("plan_id") or ""),
        "plan_sha256": _sha256(_canonical(plan)),
        "request_id": str(plan.get("binding", {}).get("request_id") or ""),
        "request_sha256": str(
            plan.get("binding", {}).get("request_sha256") or ""
        ),
        "expires_at": str(plan.get("expires_at") or ""),
        "plan_status": str(plan.get("status") or ""),
        "change": dict(plan.get("change") or {}),
        "authorization_required": True,
        "authorization_recorded": authorization.get("recorded") is True,
        "exact_scope_authorized": authorization.get("exact_scope_authorized") is True,
        "manual_apply_authorized": plan.get("manual_apply_authorized") is True,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _current_inputs(
    *,
    request_path: Path,
    packet_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    decision_dir: Path,
    project: str,
    root: Path,
    now: datetime,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    request, request_sha256, _packet = authorization_decision._current_inputs(
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        project=project,
        now=now,
    )
    request_verification = authorization_request.verify_current_authorization_request(
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        project=project,
        now=now,
    )
    decision_verification = authorization_decision.verify_current_authorization_decision(
        decision_dir=decision_dir,
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        project=project,
        now=now,
    )
    source_posture = inspect_configuration_sources(
        root=root,
        scope=request.get("scope")
        if isinstance(request.get("scope"), Mapping)
        else None,
    )
    return (
        request,
        request_sha256,
        request_verification,
        decision_verification,
        source_posture,
    )


def materialize_current_configuration_plan(
    *,
    plan_path: Path = DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    request, request_sha256, request_verification, decision_verification, sources = (
        _current_inputs(
            request_path=request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            decision_dir=decision_dir,
            project=project,
            root=root,
            now=observed_now,
        )
    )
    plan = build_configuration_plan(
        request=request,
        request_sha256=request_sha256,
        request_verification=request_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        now=observed_now,
    )
    atomic_write_bytes(
        runtime_review._rooted(plan_path),
        _canonical(plan),
        overwrite=True,
    )
    return plan


def verify_current_configuration_plan(
    *,
    plan_path: Path = DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        plan, _raw, _digest = authorization_request._private_object(
            plan_path,
            field="runtime_configuration_plan",
            maximum_bytes=MAX_PLAN_BYTES,
        )
        request, request_sha256, request_verification, decision_verification, sources = (
            _current_inputs(
                request_path=request_path,
                packet_path=packet_path,
                cycle_receipt_path=cycle_receipt_path,
                signal_dir=signal_dir,
                live_mobile_receipt_path=live_mobile_receipt_path,
                release_manifest_path=release_manifest_path,
                decision_dir=decision_dir,
                project=project,
                root=root,
                now=observed_now,
            )
        )
    except Exception:
        return _blocked("configuration_plan_current_evidence_unavailable", now=observed_now)
    verification = verify_configuration_plan(
        plan,
        request=request,
        request_verification=request_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        now=observed_now,
    )
    if verification.get("status") != "verified":
        return verification
    if verification.get("request_sha256") != request_sha256:
        return _blocked("configuration_plan_current_binding_mismatch", now=observed_now)
    verification["progress"]["current_evidence_verified"] = True
    return verification


def _configuration_handoff_blocked(
    reason: str,
    *,
    plan_path: Path,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "blocked",
        "updated_at": now.isoformat(),
        "blocking_reason": reason,
        "next_action": "repair the private plan or refresh its current authorization and source evidence",
        "plan_path": str(plan_path),
        "plan_state": "unavailable",
        "plan_state_updated": False,
        "current_evidence_verified": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_apply_authorized": False,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _ready_configuration_handoff(
    verification: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    plan_state: str,
    refresh_reason: str,
    previous_plan_sha256: str,
    plan_state_updated: bool,
    now: datetime,
) -> dict[str, Any]:
    if not (
        verification.get("status") == "verified"
        and verification.get("progress", {}).get("current_evidence_verified") is True
        and verification.get("automatic_apply_allowed") is False
        and verification.get("apply_performed") is False
        and verification.get("execution_authorized") is False
        and verification.get("execution_performed") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("protected_operation_executed") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
        and _plan_envelope_admissible(plan)
    ):
        return _configuration_handoff_blocked(
            "configuration_handoff_verification_not_admissible",
            plan_path=plan_path,
            now=now,
        )
    authorization = dict(plan.get("authorization") or {})
    return {
        "schema": HANDOFF_SCHEMA,
        "status": "ready",
        "updated_at": now.isoformat(),
        "blocking_reason": "",
        "next_action": str(verification.get("next_action") or ""),
        "plan_path": str(plan_path),
        "plan_state": plan_state,
        "plan_refresh_reason": refresh_reason,
        "plan_state_updated": plan_state_updated,
        "previous_plan_sha256": previous_plan_sha256,
        "plan_id": str(verification.get("plan_id") or ""),
        "plan_sha256": str(verification.get("plan_sha256") or ""),
        "request_id": str(verification.get("request_id") or ""),
        "request_sha256": str(verification.get("request_sha256") or ""),
        "expires_at": str(verification.get("expires_at") or ""),
        "plan_status": str(verification.get("plan_status") or ""),
        "authorization_decision": str(authorization.get("decision") or "pending"),
        "change": dict(verification.get("change") or {}),
        "current_evidence_verified": True,
        "authorization_required": True,
        "authorization_recorded": verification.get("authorization_recorded") is True,
        "exact_scope_authorized": verification.get("exact_scope_authorized") is True,
        "manual_apply_authorized": verification.get("manual_apply_authorized") is True,
        "automatic_apply_allowed": False,
        "apply_performed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def stage_current_configuration_handoff(
    *,
    plan_path: Path = DEFAULT_PLAN_PATH,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    decision_dir: Path = authorization_decision.DEFAULT_DECISION_DIR,
    project: str = "property",
    root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Keep a current non-applying plan staged without changing source or runtime."""

    observed_now = _now(now)
    try:
        target = runtime_review._rooted(plan_path)
    except (OSError, ValueError):
        return _configuration_handoff_blocked(
            "configuration_handoff_plan_path_not_admissible",
            plan_path=plan_path,
            now=observed_now,
        )
    try:
        request, request_sha256, request_verification, decision_verification, sources = (
            _current_inputs(
                request_path=request_path,
                packet_path=packet_path,
                cycle_receipt_path=cycle_receipt_path,
                signal_dir=signal_dir,
                live_mobile_receipt_path=live_mobile_receipt_path,
                release_manifest_path=release_manifest_path,
                decision_dir=decision_dir,
                project=project,
                root=root,
                now=observed_now,
            )
        )
        expected = build_configuration_plan(
            request=request,
            request_sha256=request_sha256,
            request_verification=request_verification,
            decision_verification=decision_verification,
            source_posture=sources,
            now=observed_now,
        )
    except Exception:
        return _configuration_handoff_blocked(
            "configuration_handoff_current_evidence_unavailable",
            plan_path=target,
            now=observed_now,
        )

    previous_plan_sha256 = ""
    refresh_reason = "missing"
    try:
        existing, _raw, previous_plan_sha256 = authorization_request._private_object(
            target,
            field="runtime_configuration_plan",
            maximum_bytes=MAX_PLAN_BYTES,
        )
    except FileNotFoundError:
        existing = None
    except Exception:
        return _configuration_handoff_blocked(
            "configuration_handoff_existing_plan_not_admissible",
            plan_path=target,
            now=observed_now,
        )
    if existing is not None:
        verification = verify_configuration_plan(
            existing,
            request=request,
            request_verification=request_verification,
            decision_verification=decision_verification,
            source_posture=sources,
            now=observed_now,
        )
        if (
            verification.get("status") == "verified"
            and verification.get("request_sha256") == request_sha256
        ):
            verification["progress"]["current_evidence_verified"] = True
            return _ready_configuration_handoff(
                verification,
                existing,
                plan_path=target,
                plan_state="reused",
                refresh_reason="",
                previous_plan_sha256="",
                plan_state_updated=False,
                now=observed_now,
            )
        if not _plan_envelope_admissible(existing):
            return _configuration_handoff_blocked(
                "configuration_handoff_existing_plan_not_admissible",
                plan_path=target,
                now=observed_now,
            )
        refresh_reason = (
            "expired"
            if verification.get("blocking_reason") == "configuration_plan_not_fresh"
            else "superseded"
        )

    try:
        atomic_write_bytes(target, _canonical(expected), overwrite=True)
        persisted, _persisted_raw, persisted_sha256 = (
            authorization_request._private_object(
                target,
                field="runtime_configuration_plan",
                maximum_bytes=MAX_PLAN_BYTES,
            )
        )
    except Exception:
        return _configuration_handoff_blocked(
            "configuration_handoff_plan_write_failed",
            plan_path=target,
            now=observed_now,
        )
    verification = verify_configuration_plan(
        persisted,
        request=request,
        request_verification=request_verification,
        decision_verification=decision_verification,
        source_posture=sources,
        now=observed_now,
    )
    if not (
        persisted == expected
        and verification.get("status") == "verified"
        and verification.get("request_sha256") == request_sha256
        and verification.get("plan_sha256") == persisted_sha256
    ):
        return _configuration_handoff_blocked(
            "configuration_handoff_plan_write_not_verified",
            plan_path=target,
            now=observed_now,
        )
    verification["progress"]["current_evidence_verified"] = True
    return _ready_configuration_handoff(
        verification,
        persisted,
        plan_path=target,
        plan_state="refreshed",
        refresh_reason=refresh_reason,
        previous_plan_sha256=previous_plan_sha256,
        plan_state_updated=True,
        now=observed_now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage or verify an exact PropertyQuarry configuration plan. "
            "This command never edits source, deploys, restarts, calls providers, or sends."
        )
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument(
        "--verification-write",
        type=Path,
        default=DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--request",
        type=Path,
        default=authorization_request.DEFAULT_REQUEST_PATH,
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
        "--decision-dir",
        type=Path,
        default=authorization_decision.DEFAULT_DECISION_DIR,
    )
    parser.add_argument("--project", default="property")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.verify_current:
            result = verify_current_configuration_plan(
                plan_path=args.plan,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                decision_dir=args.decision_dir,
                project=args.project,
                root=args.root,
            )
        else:
            plan = materialize_current_configuration_plan(
                plan_path=args.plan,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                decision_dir=args.decision_dir,
                project=args.project,
                root=args.root,
            )
            result = verify_current_configuration_plan(
                plan_path=args.plan,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                decision_dir=args.decision_dir,
                project=args.project,
                root=args.root,
            )
            result["plan_generated_at"] = str(plan.get("generated_at") or "")
    except Exception:
        result = _blocked("configuration_plan_materialization_failed", now=None)
    result["plan_path"] = str(args.plan)
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
