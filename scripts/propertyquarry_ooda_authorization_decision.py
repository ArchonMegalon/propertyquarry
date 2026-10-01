#!/usr/bin/env python3
"""Record and verify one explicit, non-executing PropertyQuarry decision."""

from __future__ import annotations

import argparse
import json
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

from scripts import propertyquarry_ooda_authorization_request as authorization_request
from scripts import propertyquarry_ooda_runtime_review as runtime_review
from scripts.propertyquarry_secure_file_io import OutputExistsError, atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_runtime_authorization_decision.v1"
VERIFY_SCHEMA = "propertyquarry.ooda_runtime_authorization_decision_verification.v1"
DEFAULT_DECISION_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-decisions"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-decision-verification.json"
)
MAX_DECISION_BYTES = 256 * 1024
DECISIONS = ("approve_exact_scope", "reject", "defer")
_DECISION_ID = re.compile(r"pqad_[0-9a-f]{24}\Z")
_DECIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


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
        "next_action": "verify a fresh exact-scope request before recording an operator decision",
        "progress": {
            "current_evidence_verified": False,
            "authorization_request_verified": False,
            "authorization_decision_recorded": False,
        },
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _pending(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "pending",
        "updated_at": now.isoformat(),
        "blocking_reason": "explicit_operator_decision_required",
        "next_action": "record approve_exact_scope, reject, or defer for this exact request",
        "progress": {
            "current_evidence_verified": True,
            "authorization_request_verified": True,
            "authorization_decision_recorded": False,
        },
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": request_sha256,
        "expires_at": str(request.get("expires_at") or ""),
        "scope": dict(request.get("scope") or {}),
        "decision_options": list(DECISIONS),
        "authorization_required": True,
        "authorization_recorded": False,
        "exact_scope_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _private_object(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], bytes, str]:
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


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decision_path(
    decision_dir: Path,
    request_id: str,
    request_sha256: str,
) -> Path:
    if authorization_request._REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("authorization_request_id_invalid")
    if authorization_request._SHA256.fullmatch(request_sha256) is None:
        raise ValueError("authorization_request_sha256_invalid")
    return (
        runtime_review._rooted(decision_dir)
        / f"{request_id}.{request_sha256}.json"
    )


def _current_inputs(
    *,
    request_path: Path,
    packet_path: Path,
    cycle_receipt_path: Path,
    signal_dir: Path,
    live_mobile_receipt_path: Path,
    release_manifest_path: Path,
    project: str,
    now: datetime,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    verification = authorization_request.verify_current_authorization_request(
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        project=project,
        now=now,
    )
    if not (
        verification.get("status") == "verified"
        and verification.get("progress", {}).get("current_evidence_verified") is True
        and verification.get("authorization_recorded") is False
        and verification.get("execution_authorized") is False
        and verification.get("deployment_or_restart_authorized") is False
        and verification.get("provider_quota_consumption_allowed") is False
        and verification.get("delivery_authorized") is False
    ):
        raise ValueError("authorization_request_not_current")
    request, _raw, request_sha256 = _private_object(
        request_path,
        field="runtime_authorization_request",
        maximum_bytes=authorization_request.MAX_REQUEST_BYTES,
    )
    if request_sha256 != verification.get("request_sha256"):
        raise ValueError("authorization_request_binding_mismatch")
    packet, _packet_raw, _packet_sha256 = _private_object(
        packet_path,
        field="runtime_review_packet",
        maximum_bytes=runtime_review.MAX_PACKET_BYTES,
    )
    return request, request_sha256, packet


def build_authorization_decision(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    review_packet: Mapping[str, Any],
    decision: str,
    decider_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    recorded_at = _now(now)
    normalized_decision = str(decision or "").strip()
    normalized_decider = str(decider_id or "").strip()
    request_verification = authorization_request.verify_authorization_request(
        request,
        review_packet=review_packet,
        now=recorded_at,
    )
    if not (
        request_verification.get("status") == "verified"
        and request_sha256 == _sha256(_canonical(request))
        and normalized_decision in DECISIONS
        and _DECIDER_ID.fullmatch(normalized_decider) is not None
        and request.get("excluded_operations")
        == authorization_request._EXCLUDED_OPERATIONS
    ):
        raise ValueError("authorization_decision_input_not_admissible")
    request_expires_at = _parse_timestamp(request.get("expires_at"))
    if request_expires_at is None or recorded_at > request_expires_at:
        raise ValueError("authorization_request_not_fresh")
    scope = dict(request.get("scope") or {})
    binding = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": request_sha256,
        "request_schema": str(request.get("schema") or ""),
        "request_generated_at": str(request.get("generated_at") or ""),
        "request_expires_at": str(request.get("expires_at") or ""),
        "review_packet_sha256": str(
            request.get("binding", {}).get("review_packet_sha256") or ""
        ),
        "proposal_sha256": str(
            request.get("binding", {}).get("proposal_sha256") or ""
        ),
        "action_sha256": str(
            request.get("binding", {}).get("action_sha256") or ""
        ),
        "scope_sha256": _sha256(_canonical(scope)),
    }
    exact_scope_authorized = normalized_decision == "approve_exact_scope"
    decision_digest = _sha256(
        _canonical(
            {
                "binding": binding,
                "decision": normalized_decision,
                "decider_id": normalized_decider,
                "recorded_at": recorded_at.isoformat(),
            }
        )
    )
    status = {
        "approve_exact_scope": "exact_scope_authorized",
        "reject": "rejected",
        "defer": "deferred",
    }[normalized_decision]
    next_action = {
        "approve_exact_scope": (
            "stage only the exact runtime configuration change for review; do not deploy or restart, "
            "and verify this receipt immediately before the change"
        ),
        "reject": "leave the runtime configuration unchanged and close this request",
        "defer": "leave the runtime configuration unchanged and generate a fresh request when ready",
    }[normalized_decision]
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "decision_id": f"pqad_{decision_digest[:24]}",
        "recorded_at": recorded_at.isoformat(),
        "expires_at": request_expires_at.isoformat(),
        "status": status,
        "next_action": next_action,
        "binding": binding,
        "scope": scope,
        "decision": {
            "value": normalized_decision,
            "decider_id": normalized_decider,
            "explicit_input_recorded": True,
            "source": "local_operator_cli",
            "cryptographic_identity_verified": False,
        },
        "excluded_operations": list(authorization_request._EXCLUDED_OPERATIONS),
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": exact_scope_authorized,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
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


def verify_authorization_decision(
    receipt: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    review_packet: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and authorization_request._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256") == _sha256(_canonical(normalized))
    ):
        return _blocked("authorization_decision_integrity_invalid", now=observed_now)
    recorded_at = _parse_timestamp(receipt.get("recorded_at"))
    expires_at = _parse_timestamp(receipt.get("expires_at"))
    if recorded_at is None or expires_at is None:
        return _blocked("authorization_decision_timestamp_invalid", now=observed_now)
    if recorded_at > expires_at or observed_now < recorded_at or observed_now > expires_at:
        return _blocked("authorization_decision_not_fresh", now=observed_now)
    decision_row = receipt.get("decision")
    if not isinstance(decision_row, dict):
        return _blocked("authorization_decision_contract_not_admissible", now=observed_now)
    try:
        expected = build_authorization_decision(
            request=request,
            request_sha256=_sha256(_canonical(request)),
            review_packet=review_packet,
            decision=str(decision_row.get("value") or ""),
            decider_id=str(decision_row.get("decider_id") or ""),
            now=recorded_at,
        )
    except (TypeError, ValueError):
        return _blocked("authorization_decision_request_not_admissible", now=observed_now)
    if not (
        dict(receipt) == expected
        and _DECISION_ID.fullmatch(str(receipt.get("decision_id") or "")) is not None
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("execution_performed") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("secret_values_recorded") is False
    ):
        return _blocked("authorization_decision_contract_not_admissible", now=observed_now)
    decision_value = str(decision_row.get("value") or "")
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": str(receipt.get("next_action") or ""),
        "progress": {
            "current_evidence_verified": False,
            "authorization_request_verified": True,
            "authorization_decision_recorded": True,
        },
        "request_id": str(receipt.get("binding", {}).get("request_id") or ""),
        "request_sha256": str(
            receipt.get("binding", {}).get("request_sha256") or ""
        ),
        "decision_id": str(receipt.get("decision_id") or ""),
        "decision": decision_value,
        "decider_id": str(decision_row.get("decider_id") or ""),
        "recorded_at": str(receipt.get("recorded_at") or ""),
        "expires_at": str(receipt.get("expires_at") or ""),
        "scope": dict(receipt.get("scope") or {}),
        "authorization_required": True,
        "authorization_recorded": True,
        "exact_scope_authorized": decision_value == "approve_exact_scope",
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def materialize_current_authorization_decision(
    *,
    decision: str,
    decider_id: str,
    expected_request_id: str,
    expected_request_sha256: str,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    project: str = "property",
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    observed_now = _now(now)
    request, request_sha256, packet = _current_inputs(
        request_path=request_path,
        packet_path=packet_path,
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=signal_dir,
        live_mobile_receipt_path=live_mobile_receipt_path,
        release_manifest_path=release_manifest_path,
        project=project,
        now=observed_now,
    )
    request_id = str(request.get("request_id") or "")
    if not (
        expected_request_id == request_id
        and expected_request_sha256 == request_sha256
    ):
        raise ValueError("operator_request_binding_mismatch")
    receipt = build_authorization_decision(
        request=request,
        request_sha256=request_sha256,
        review_packet=packet,
        decision=decision,
        decider_id=decider_id,
        now=observed_now,
    )
    decision_path = _decision_path(decision_dir, request_id, request_sha256)
    try:
        atomic_write_bytes(decision_path, _canonical(receipt), overwrite=False)
    except OutputExistsError:
        existing, _raw, _digest = _private_object(
            decision_path,
            field="runtime_authorization_decision",
            maximum_bytes=MAX_DECISION_BYTES,
        )
        existing_decision = existing.get("decision")
        existing_verification = verify_authorization_decision(
            existing,
            request=request,
            review_packet=packet,
            now=observed_now,
        )
        if not (
            existing_verification.get("status") == "verified"
            and existing.get("binding", {}).get("request_sha256") == request_sha256
            and isinstance(existing_decision, dict)
            and existing_decision.get("value") == str(decision or "").strip()
            and existing_decision.get("decider_id") == str(decider_id or "").strip()
        ):
            raise ValueError("authorization_decision_already_recorded")
        receipt = existing
    return receipt, decision_path


def verify_current_authorization_decision(
    *,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    request_path: Path = authorization_request.DEFAULT_REQUEST_PATH,
    packet_path: Path = runtime_review.DEFAULT_PACKET_PATH,
    cycle_receipt_path: Path = runtime_review.DEFAULT_CYCLE_RECEIPT,
    signal_dir: Path = runtime_review.DEFAULT_SIGNAL_DIR,
    live_mobile_receipt_path: Path = runtime_review.DEFAULT_LIVE_MOBILE_RECEIPT,
    release_manifest_path: Path = runtime_review.DEFAULT_RELEASE_MANIFEST,
    project: str = "property",
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        request, request_sha256, packet = _current_inputs(
            request_path=request_path,
            packet_path=packet_path,
            cycle_receipt_path=cycle_receipt_path,
            signal_dir=signal_dir,
            live_mobile_receipt_path=live_mobile_receipt_path,
            release_manifest_path=release_manifest_path,
            project=project,
            now=observed_now,
        )
        decision_path = _decision_path(
            decision_dir,
            str(request.get("request_id") or ""),
            request_sha256,
        )
        try:
            receipt, _raw, decision_sha256 = _private_object(
                decision_path,
                field="runtime_authorization_decision",
                maximum_bytes=MAX_DECISION_BYTES,
            )
        except FileNotFoundError:
            pending = _pending(
                request=request,
                request_sha256=request_sha256,
                now=observed_now,
            )
            pending["decision_path"] = str(decision_path)
            return pending
    except Exception:
        return _blocked("authorization_decision_current_evidence_unavailable", now=observed_now)
    verification = verify_authorization_decision(
        receipt,
        request=request,
        review_packet=packet,
        now=observed_now,
    )
    if verification.get("status") != "verified":
        return verification
    if verification.get("request_sha256") != request_sha256:
        return _blocked("authorization_decision_current_binding_mismatch", now=observed_now)
    verification["progress"]["current_evidence_verified"] = True
    verification["decision_path"] = str(decision_path)
    verification["decision_sha256"] = decision_sha256
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify one explicit PropertyQuarry authorization decision. "
            "This command never changes runtime configuration or deploys anything."
        )
    )
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--decision", choices=DECISIONS)
    parser.add_argument("--decider-id")
    parser.add_argument("--request-id")
    parser.add_argument("--request-sha256")
    parser.add_argument("--decision-dir", type=Path, default=DEFAULT_DECISION_DIR)
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
    parser.add_argument("--project", default="property")
    args = parser.parse_args(argv)
    decision_path: Path | None = None
    try:
        if args.verify_current:
            result = verify_current_authorization_decision(
                decision_dir=args.decision_dir,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                project=args.project,
            )
        else:
            if not all(
                str(value or "").strip()
                for value in (
                    args.decision,
                    args.decider_id,
                    args.request_id,
                    args.request_sha256,
                )
            ):
                raise ValueError("explicit_decision_arguments_required")
            _receipt, decision_path = materialize_current_authorization_decision(
                decision=str(args.decision),
                decider_id=str(args.decider_id),
                expected_request_id=str(args.request_id),
                expected_request_sha256=str(args.request_sha256),
                decision_dir=args.decision_dir,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                project=args.project,
            )
            result = verify_current_authorization_decision(
                decision_dir=args.decision_dir,
                request_path=args.request,
                packet_path=args.packet,
                cycle_receipt_path=args.cycle_receipt,
                signal_dir=args.signal_dir,
                live_mobile_receipt_path=args.live_mobile_receipt,
                release_manifest_path=args.release_manifest,
                project=args.project,
            )
    except Exception:
        result = _blocked("authorization_decision_materialization_failed", now=None)
    if decision_path is not None:
        result["decision_path"] = str(decision_path)
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
    return 0 if result.get("status") in {"verified", "pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
