#!/usr/bin/env python3
"""Record and verify explicit scheduler activation authorization decisions."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_scheduler_activation_authorization as authorization
from scripts import propertyquarry_ooda_scheduler_activation_readiness as readiness
from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_scheduler_activation_authorization_decision.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_authorization_decision_verification.v1"
)
DEFAULT_DECISION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-authorization-decisions"
)
DEFAULT_READINESS_PATH = readiness.DEFAULT_VERIFICATION_PATH
MAX_DECISION_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DECISION_ID = re.compile(r"pqsad_[0-9a-f]{24}\Z")
_DECIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_DECISIONS = {"approve_exact_scope", "reject", "defer"}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return authorization._canonical(value)


def _sha256(value: bytes) -> str:
    return authorization._sha256(value)


def _timestamp(value: object) -> datetime | None:
    return authorization._parse_timestamp(value)


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "record a fresh explicit decision against the current exact request",
        "action_required": False,
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_deployment_authorized": False,
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


def _private_object(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
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
        maximum_bytes=maximum_bytes,
    )
    return payload, digest


def build_scheduler_activation_authorization_decision(
    *,
    request: Mapping[str, Any],
    activation_readiness: Mapping[str, Any],
    decision: str,
    decider_id: str,
    now: datetime | None = None,
    _source_validation_now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    source_validation_now = (
        _now(_source_validation_now)
        if _source_validation_now is not None
        else observed_now
    )
    normalized_decision = str(decision or "").strip()
    normalized_decider = str(decider_id or "").strip()
    if normalized_decision not in _DECISIONS:
        raise ValueError("scheduler_activation_decision_not_admissible")
    if _DECIDER_ID.fullmatch(normalized_decider) is None:
        raise ValueError("scheduler_activation_decider_not_admissible")
    request_verification = (
        authorization.verify_scheduler_activation_authorization_request(
            request,
            activation_readiness=activation_readiness,
            now=source_validation_now,
        )
    )
    if request_verification.get("status") != "verified":
        raise ValueError("scheduler_activation_request_not_current")
    expires_at = _timestamp(request_verification.get("expires_at"))
    if expires_at is None or observed_now > expires_at:
        raise ValueError("scheduler_activation_request_not_current")
    approved = normalized_decision == "approve_exact_scope"
    identity_payload = {
        "request_id": request_verification["request_id"],
        "request_sha256": request_verification["request_sha256"],
        "decision": normalized_decision,
        "decider_id": normalized_decider,
        "recorded_at": observed_now.isoformat(),
    }
    identity_digest = _sha256(_canonical(identity_payload))
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "decision_id": f"pqsad_{identity_digest[:24]}",
        "recorded_at": observed_now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": "recorded",
        "decision": normalized_decision,
        "decider_id": normalized_decider,
        "request_id": str(request_verification.get("request_id") or ""),
        "request_sha256": str(
            request_verification.get("request_sha256") or ""
        ),
        "semantic_request_sha256": str(
            request_verification.get("semantic_request_sha256") or ""
        ),
        "readiness_receipt_sha256": str(
            request_verification.get("binding", {}).get(
                "readiness_receipt_sha256"
            )
            or ""
        ),
        "scope": dict(request_verification.get("scope") or {}),
        "release_evidence": dict(
            request_verification.get("release_evidence") or {}
        ),
        "protected_operations": list(
            request_verification.get("protected_operations") or []
        ),
        "action_required": False,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": approved,
        "manual_deployment_authorized": approved,
        "automatic_execution_allowed": False,
        "execution_authorized": approved,
        "deployment_or_restart_authorized": approved,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            "current_evidence_verified": True,
            "authorization_request_verified": True,
            "authorization_decision_recorded": True,
            "exact_scope_authorized": approved,
            "execution_performed": False,
        },
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def verify_scheduler_activation_authorization_decision(
    receipt: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    activation_readiness: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    recorded_at = _timestamp(receipt.get("recorded_at"))
    expires_at = _timestamp(receipt.get("expires_at"))
    if recorded_at is None or expires_at is None:
        return _blocked(
            "scheduler_activation_decision_timestamp_invalid",
            now=observed_now,
        )
    age = (observed_now - recorded_at).total_seconds()
    if not (
        math.isfinite(age)
        and age >= 0
        and observed_now <= expires_at
    ):
        return _blocked(
            "scheduler_activation_decision_not_fresh",
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
            "scheduler_activation_decision_integrity_invalid",
            now=observed_now,
        )
    try:
        expected = build_scheduler_activation_authorization_decision(
            request=request,
            activation_readiness=activation_readiness,
            decision=str(receipt.get("decision") or ""),
            decider_id=str(receipt.get("decider_id") or ""),
            now=recorded_at,
            _source_validation_now=observed_now,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_activation_decision_source_not_admissible",
            now=observed_now,
        )
    if dict(receipt) != expected:
        return _blocked(
            "scheduler_activation_decision_source_binding_mismatch",
            now=observed_now,
        )
    approved = receipt.get("decision") == "approve_exact_scope"
    if not (
        _DECISION_ID.fullmatch(str(receipt.get("decision_id") or ""))
        and (receipt.get("exact_scope_authorized") is True) == approved
        and (receipt.get("manual_deployment_authorized") is True) == approved
        and (receipt.get("execution_authorized") is True) == approved
        and (
            receipt.get("deployment_or_restart_authorized") is True
        )
        == approved
        and receipt.get("deployment_or_restart_performed") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
    ):
        return _blocked(
            "scheduler_activation_decision_authority_not_admissible",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "decision_id": str(receipt.get("decision_id") or ""),
        "decision_sha256": _sha256(_canonical(receipt)),
        "decision": str(receipt.get("decision") or ""),
        "decider_id": str(receipt.get("decider_id") or ""),
        "recorded_at": recorded_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "request_id": str(receipt.get("request_id") or ""),
        "request_sha256": str(receipt.get("request_sha256") or ""),
        "semantic_request_sha256": str(
            receipt.get("semantic_request_sha256") or ""
        ),
        "readiness_receipt_sha256": str(
            receipt.get("readiness_receipt_sha256") or ""
        ),
        "scope": dict(receipt.get("scope") or {}),
        "release_evidence": dict(receipt.get("release_evidence") or {}),
        "protected_operations": list(
            receipt.get("protected_operations") or []
        ),
        "action_required": False,
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": approved,
        "manual_deployment_authorized": approved,
        "automatic_execution_allowed": False,
        "execution_authorized": approved,
        "deployment_or_restart_authorized": approved,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "decision_integrity_verified": True,
        },
    }


def _ensure_private_directory(path: Path) -> Path:
    target = Path(path).absolute()
    if not target.exists():
        target.mkdir(parents=True, mode=0o700)
    metadata = target.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("scheduler_activation_decision_dir_not_admissible")
    return target


def _pending(
    request_verification: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "pending",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_scheduler_activation_decision_required",
        "next_action": "approve, reject, or defer the current exact activation request",
        "request_id": str(request_verification.get("request_id") or ""),
        "request_sha256": str(
            request_verification.get("request_sha256") or ""
        ),
        "semantic_request_sha256": str(
            request_verification.get("semantic_request_sha256") or ""
        ),
        "expires_at": str(request_verification.get("expires_at") or ""),
        "scope": dict(request_verification.get("scope") or {}),
        "release_evidence": dict(
            request_verification.get("release_evidence") or {}
        ),
        "action_required": True,
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "manual_deployment_authorized": False,
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
            "current_evidence_verified": True,
            "authorization_request_verified": True,
            "authorization_decision_recorded": False,
        },
    }


def verify_current_scheduler_activation_authorization_decision(
    *,
    request_path: Path = authorization.DEFAULT_REQUEST_PATH,
    readiness_path: Path = DEFAULT_READINESS_PATH,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        request, _request_digest = _private_object(
            request_path,
            field="scheduler_activation_authorization_request",
            maximum_bytes=authorization.MAX_REQUEST_BYTES,
        )
        activation_readiness, _readiness_digest = _private_object(
            readiness_path,
            field="scheduler_activation_readiness_verification",
            maximum_bytes=MAX_DECISION_BYTES,
        )
        request_verification = (
            authorization.verify_scheduler_activation_authorization_request(
                request,
                activation_readiness=activation_readiness,
                now=observed_now,
            )
        )
        if request_verification.get("status") != "verified":
            raise ValueError("scheduler_activation_request_not_current")
    except (OSError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_decision_current_evidence_unavailable",
            now=observed_now,
        )
    target_dir = Path(decision_dir).absolute()
    if not target_dir.exists():
        return _pending(request_verification, now=observed_now)
    try:
        metadata = target_dir.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("scheduler_activation_decision_dir_not_admissible")
        candidates = sorted(target_dir.iterdir(), key=lambda path: path.name)
        if len(candidates) > 256:
            raise ValueError("scheduler_activation_decision_cardinality_not_admissible")
        current: list[dict[str, Any]] = []
        for path in candidates:
            candidate, _candidate_digest = _private_object(
                path,
                field="scheduler_activation_authorization_decision",
                maximum_bytes=MAX_DECISION_BYTES,
            )
            if candidate.get("request_id") != request_verification.get(
                "request_id"
            ):
                continue
            if candidate.get("request_sha256") != request_verification.get(
                "request_sha256"
            ):
                continue
            verification = verify_scheduler_activation_authorization_decision(
                candidate,
                request=request,
                activation_readiness=activation_readiness,
                now=observed_now,
            )
            if verification.get("status") != "verified":
                raise ValueError(
                    "scheduler_activation_current_decision_not_admissible"
                )
            verification["decision_path"] = str(path)
            verification["decision_receipt_persisted"] = True
            current.append(verification)
    except (OSError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_current_decision_not_admissible",
            now=observed_now,
        )
    if not current:
        return _pending(request_verification, now=observed_now)
    if len(current) != 1:
        return _blocked(
            "scheduler_activation_current_decision_ambiguous",
            now=observed_now,
        )
    return current[0]


def record_current_scheduler_activation_authorization_decision(
    *,
    decision: str,
    decider_id: str,
    expected_request_id: str,
    expected_request_sha256: str,
    request_path: Path = authorization.DEFAULT_REQUEST_PATH,
    readiness_path: Path = DEFAULT_READINESS_PATH,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        request, request_digest = _private_object(
            request_path,
            field="scheduler_activation_authorization_request",
            maximum_bytes=authorization.MAX_REQUEST_BYTES,
        )
        activation_readiness, _readiness_digest = _private_object(
            readiness_path,
            field="scheduler_activation_readiness_verification",
            maximum_bytes=MAX_DECISION_BYTES,
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_decision_current_evidence_unavailable",
            now=observed_now,
        )
    if not (
        authorization._REQUEST_ID.fullmatch(str(expected_request_id or ""))
        and _SHA256.fullmatch(str(expected_request_sha256 or ""))
        and request.get("request_id") == expected_request_id
        and request_digest == expected_request_sha256
    ):
        return _blocked(
            "scheduler_activation_decision_expected_request_mismatch",
            now=observed_now,
        )
    existing = verify_current_scheduler_activation_authorization_decision(
        request_path=request_path,
        readiness_path=readiness_path,
        decision_dir=decision_dir,
        now=observed_now,
    )
    if existing.get("status") == "verified":
        return _blocked(
            "scheduler_activation_decision_already_recorded",
            now=observed_now,
        )
    if existing.get("status") != "pending":
        return _blocked(
            str(
                existing.get("blocking_reason")
                or "scheduler_activation_current_decision_not_admissible"
            ),
            now=observed_now,
        )
    try:
        receipt = build_scheduler_activation_authorization_decision(
            request=request,
            activation_readiness=activation_readiness,
            decision=decision,
            decider_id=decider_id,
            now=observed_now,
        )
        target_dir = _ensure_private_directory(decision_dir)
        target = target_dir / (
            f"{receipt['request_id']}--{receipt['decision_id']}.json"
        )
        atomic_write_bytes(target, _canonical(receipt), overwrite=False)
        persisted, persisted_digest = _private_object(
            target,
            field="scheduler_activation_authorization_decision",
            maximum_bytes=MAX_DECISION_BYTES,
        )
        if persisted != receipt or persisted_digest != _sha256(
            _canonical(receipt)
        ):
            raise ValueError("scheduler_activation_decision_persistence_mismatch")
        verification = verify_scheduler_activation_authorization_decision(
            receipt,
            request=request,
            activation_readiness=activation_readiness,
            now=observed_now,
        )
        if verification.get("status") != "verified":
            raise ValueError("scheduler_activation_decision_verification_failed")
        verification["decision_path"] = str(target)
        verification["decision_receipt_persisted"] = True
        return verification
    except (OSError, TypeError, ValueError):
        return _blocked(
            "scheduler_activation_decision_recording_failed",
            now=observed_now,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record one explicit PropertyQuarry scheduler activation decision."
    )
    parser.add_argument("--decision", required=True, choices=sorted(_DECISIONS))
    parser.add_argument("--decider-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--request", type=Path, default=authorization.DEFAULT_REQUEST_PATH)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS_PATH)
    parser.add_argument("--decision-dir", type=Path, default=DEFAULT_DECISION_DIR)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = record_current_scheduler_activation_authorization_decision(
        decision=args.decision,
        decider_id=args.decider_id,
        expected_request_id=args.request_id,
        expected_request_sha256=args.request_sha256,
        request_path=args.request,
        readiness_path=args.readiness,
        decision_dir=args.decision_dir,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
