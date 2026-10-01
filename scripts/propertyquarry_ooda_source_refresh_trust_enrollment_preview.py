#!/usr/bin/env python3
"""Stage an exact producer-trust registry preview without enrolling trust."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


SCHEMA = "propertyquarry.ooda_source_refresh_trust_enrollment_preview.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_enrollment_preview_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-preview.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-enrollment-preview-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = trust_intake.DEFAULT_MAX_AGE_SECONDS
MAX_RECEIPT_BYTES = 1024 * 1024


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return trust_intake._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return trust_intake._sha256(value)


def _timestamp(value: object) -> datetime | None:
    return trust_intake._timestamp(value)


def _with_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    normalized.pop("integrity", None)
    normalized["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(normalized)),
    }
    return normalized


def _integrity_verified(value: Mapping[str, Any]) -> bool:
    normalized = dict(value)
    integrity = normalized.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and integrity.get("algorithm") == "sha256"
        and trust_intake._SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _safety_fields() -> dict[str, bool]:
    return {
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "preview_staged": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
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


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "preview_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the exact intake, decision, registry, and preview binding; "
            "do not enroll trust or modify the registry"
        ),
        "preview_id": "",
        "candidate_review_id": "",
        "decision_id": "",
        "current_trust_registry_sha256": "",
        "proposed_trust_registry_sha256": "",
        "progress": {
            "current_evidence_verified": False,
            "intake_binding_verified": False,
            "decision_binding_verified": False,
            "trust_registry_binding_verified": False,
            "preview_integrity_verified": False,
        },
        **_safety_fields(),
    }


def _candidate_entries(
    intake: Mapping[str, Any],
    *,
    current_registry_sha256: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    fingerprints: set[str] = set()
    for raw in list(intake.get("candidates") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("trust_enrollment_preview_candidate_not_admissible")
        producer_id = str(raw.get("producer_id") or "")
        key_id = str(raw.get("key_id") or "")
        public_key = str(raw.get("public_key") or "")
        public_key_sha256 = str(raw.get("public_key_sha256") or "")
        lanes = list(raw.get("lanes") or [])
        decoded = claims._decode_base64url(
            public_key,
            expected_bytes=32,
            label="trust_enrollment_preview_public_key",
        )
        identity = (producer_id, key_id)
        if not (
            claims._IDENTIFIER.fullmatch(producer_id)
            and claims._IDENTIFIER.fullmatch(key_id)
            and identity not in identities
            and public_key_sha256 not in fingerprints
            and raw.get("algorithm") == "Ed25519"
            and public_key_sha256 == _sha256(decoded)
            and raw.get("requested_status") == "ACTIVE"
            and raw.get("trust_registry_sha256") == current_registry_sha256
            and lanes == sorted(set(lanes))
            and bool(lanes)
            and all(
                str(lane) in trust_intake.source_refresh._LANE_ARTIFACTS
                for lane in lanes
            )
            and raw.get("proof_of_possession_verified") is True
            and raw.get("candidate_confers_authority") is False
            and raw.get("trust_enrollment_authorized") is False
            and raw.get("private_key_material_recorded") is False
        ):
            raise ValueError("trust_enrollment_preview_candidate_not_admissible")
        identities.add(identity)
        fingerprints.add(public_key_sha256)
        entries.append(
            {
                "producer_id": producer_id,
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key": public_key,
                "public_key_sha256": public_key_sha256,
                "lanes": sorted(str(lane) for lane in lanes),
                "status": "ACTIVE",
            }
        )
    return sorted(entries, key=lambda row: (row["producer_id"], row["key_id"]))


def _decision_binding_is_admissible(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    require_confirm: bool,
) -> bool:
    status = str(decision.get("status") or "")
    decision_value = str(decision.get("decision") or "")
    expected_scope = trust_decision._candidate_scope(intake)
    common = bool(
        trust_intake._review_is_admissible(intake)
        and status in {"pending", "verified"}
        and decision.get("candidate_review_id")
        == intake.get("candidate_review_id")
        and decision.get("trust_intake_verification_sha256")
        == intake.get("verification_receipt_sha256")
        and list(decision.get("candidate_scope") or []) == expected_scope
        and decision.get("trust_enrollment_authorized") is False
        and decision.get("trust_registry_modified") is False
        and decision.get("decision_confers_trust") is False
        and decision.get("private_key_material_requested") is False
        and decision.get("private_key_material_recorded") is False
        and decision.get("provider_quota_consumption_allowed") is False
        and decision.get("delivery_authorized") is False
        and decision.get("protected_operation_executed") is False
        and (
            status != "verified"
            or trust_decision._DECISION_ID.fullmatch(
                str(decision.get("decision_id") or "")
            )
            and trust_intake._SHA256.fullmatch(
                str(decision.get("decision_sha256") or "")
            )
        )
    )
    if not common:
        return False
    if not require_confirm:
        return bool(
            status == "pending"
            or status == "verified"
            and decision_value in {"reject_candidate", "defer"}
        )
    return bool(
        status == "verified"
        and decision_value == "confirm_identity_verified"
        and decision.get("review_state") == "identity_verified"
        and decision.get("identity_verification_asserted") is True
        and decision.get("trust_enrollment_preview_authorized") is True
        and trust_decision._DECISION_ID.fullmatch(
            str(decision.get("decision_id") or "")
        )
        and trust_intake._SHA256.fullmatch(
            str(decision.get("decision_sha256") or "")
        )
    )


def _base_binding(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    trust_registry: Mapping[str, Any],
) -> dict[str, Any]:
    scope = trust_decision._candidate_scope(intake)
    return {
        "candidate_review_id": str(intake.get("candidate_review_id") or ""),
        "intake_receipt_sha256": str(
            intake.get("intake_receipt_sha256") or ""
        ),
        "trust_intake_verification_sha256": str(
            intake.get("verification_receipt_sha256") or ""
        ),
        "decision_id": str(decision.get("decision_id") or ""),
        "decision_sha256": str(decision.get("decision_sha256") or ""),
        "decision": str(decision.get("decision") or ""),
        "candidate_scope_sha256": _sha256(
            _canonical({"candidates": scope})
        ),
        "current_trust_registry_sha256": str(
            trust_registry.get("trust_registry_sha256") or ""
        ),
    }


def _admissible_expiry(intake: Mapping[str, Any], *, now: datetime) -> datetime:
    expires_at = _timestamp(intake.get("expires_at"))
    if expires_at is None or expires_at < now:
        raise ValueError("trust_enrollment_preview_intake_not_fresh")
    return expires_at


def build_enrollment_preview(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    trust_registry: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _now(now)
    expires_at = _admissible_expiry(intake, now=generated_at)
    current_digest = str(trust_registry.get("trust_registry_sha256") or "")
    source_binding = intake.get("source_binding")
    if not (
        isinstance(source_binding, Mapping)
        and trust_intake._SHA256.fullmatch(current_digest)
        and source_binding.get("trust_registry_sha256") == current_digest
        and trust_registry.get("status") in {"UNCONFIGURED", "ACTIVE"}
        and isinstance(trust_registry.get("rotation_epoch"), int)
        and not isinstance(trust_registry.get("rotation_epoch"), bool)
        and isinstance(trust_registry.get("producers"), list)
    ):
        raise ValueError("trust_enrollment_preview_registry_binding_invalid")
    candidates = list(intake.get("candidates") or [])
    if not candidates:
        if not (
            decision.get("status") == "not_required"
            and intake.get("status") == "verified"
            and intake.get("action_required") is False
            and intake.get("public_key_candidate_recorded") is False
        ):
            raise ValueError("trust_enrollment_preview_no_candidate_invalid")
        return _with_integrity(
            {
                "schema": SCHEMA,
                "status": "not_required",
                "preview_id": "",
                "generated_at": generated_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "blocking_reason": "",
                "next_action": "await a verified producer public-key candidate",
                "binding": _base_binding(intake, decision, trust_registry),
                "current_registry": {
                    "status": str(trust_registry.get("status") or ""),
                    "rotation_epoch": int(
                        trust_registry.get("rotation_epoch") or 0
                    ),
                    "producer_count": len(
                        list(trust_registry.get("producers") or [])
                    ),
                },
                "proposed_registry": {},
                "proposed_trust_registry_sha256": "",
                "registry_diff": {},
                "authorization_scope": {},
                **_safety_fields(),
            }
        )
    if not _decision_binding_is_admissible(
        intake, decision, require_confirm=False
    ) and not _decision_binding_is_admissible(
        intake, decision, require_confirm=True
    ):
        raise ValueError("trust_enrollment_preview_decision_binding_invalid")
    if decision.get("status") == "pending":
        preview_state = "awaiting_decision"
        next_action = str(decision.get("next_action") or "")
    elif decision.get("decision") in {"reject_candidate", "defer"}:
        preview_state = "not_required"
        next_action = str(decision.get("next_action") or "")
    else:
        preview_state = "preview_staged"
        next_action = (
            "review and separately authorize or reject this exact registry "
            "preview; no trust enrollment or registry write has occurred"
        )
    binding = _base_binding(intake, decision, trust_registry)
    if preview_state != "preview_staged":
        return _with_integrity(
            {
                "schema": SCHEMA,
                "status": preview_state,
                "preview_id": "",
                "generated_at": generated_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "blocking_reason": "",
                "next_action": next_action,
                "binding": binding,
                "current_registry": {
                    "status": str(trust_registry.get("status") or ""),
                    "rotation_epoch": int(
                        trust_registry.get("rotation_epoch") or 0
                    ),
                    "producer_count": len(
                        list(trust_registry.get("producers") or [])
                    ),
                },
                "proposed_registry": {},
                "proposed_trust_registry_sha256": "",
                "registry_diff": {},
                "authorization_scope": {},
                **_safety_fields(),
            }
        )
    additions = _candidate_entries(
        intake,
        current_registry_sha256=current_digest,
    )
    current_producers = [
        dict(row) for row in list(trust_registry.get("producers") or [])
    ]
    current_identities = {
        (str(row.get("producer_id") or ""), str(row.get("key_id") or ""))
        for row in current_producers
    }
    current_fingerprints = {
        str(row.get("public_key_sha256") or "") for row in current_producers
    }
    if any(
        (row["producer_id"], row["key_id"]) in current_identities
        or row["public_key_sha256"] in current_fingerprints
        for row in additions
    ):
        raise ValueError("trust_enrollment_preview_candidate_already_registered")
    rotation_epoch = int(trust_registry.get("rotation_epoch") or 0)
    if rotation_epoch >= 2**31 - 1 or len(current_producers) + len(additions) > 32:
        raise ValueError("trust_enrollment_preview_registry_capacity_invalid")
    proposed_registry = _with_integrity(
        {
            "schema": claims.TRUST_SCHEMA,
            "status": "ACTIVE",
            "rotation_epoch": rotation_epoch + 1,
            "producers": sorted(
                current_producers + additions,
                key=lambda row: (
                    str(row.get("producer_id") or ""),
                    str(row.get("key_id") or ""),
                ),
            ),
        }
    )
    proposed_digest = _sha256(_canonical(proposed_registry))
    preview_semantic = {
        "binding": binding,
        "proposed_trust_registry_sha256": proposed_digest,
    }
    preview_id = f"pqtrustpreview_{_sha256(_canonical(preview_semantic))[:24]}"
    safety = _safety_fields()
    safety.update(
        {
            "action_required": True,
            "interrupt_operator": True,
            "operator_review_required": True,
            "preview_staged": True,
            "identity_verification_asserted": True,
            "trust_enrollment_preview_authorized": True,
        }
    )
    return _with_integrity(
        {
            "schema": SCHEMA,
            "status": "preview_staged",
            "preview_id": preview_id,
            "generated_at": generated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "blocking_reason": "",
            "next_action": next_action,
            "binding": binding,
            "current_registry": {
                "status": str(trust_registry.get("status") or ""),
                "rotation_epoch": rotation_epoch,
                "producer_count": len(current_producers),
            },
            "proposed_registry": proposed_registry,
            "proposed_trust_registry_sha256": proposed_digest,
            "registry_diff": {
                "status": {
                    "from": str(trust_registry.get("status") or ""),
                    "to": "ACTIVE",
                },
                "rotation_epoch": {
                    "from": rotation_epoch,
                    "to": rotation_epoch + 1,
                },
                "additions": additions,
                "removals": [],
            },
            "authorization_scope": {
                "operation": "replace_trust_registry_with_exact_preview",
                "preview_id": preview_id,
                "expected_current_trust_registry_sha256": current_digest,
                "proposed_trust_registry_sha256": proposed_digest,
                "decision_options": [
                    "authorize_exact_preview",
                    "reject",
                    "defer",
                ],
                "authorization_recorded": False,
            },
            **safety,
        }
    )


def verify_enrollment_preview(
    receipt: Mapping[str, Any],
    *,
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    trust_registry: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    generated_at = _timestamp(receipt.get("generated_at"))
    expires_at = _timestamp(receipt.get("expires_at"))
    if not (
        receipt.get("schema") == SCHEMA
        and _integrity_verified(receipt)
        and generated_at is not None
        and expires_at is not None
        and generated_at <= observed_now <= expires_at
        and math.isfinite((observed_now - generated_at).total_seconds())
        and -30.0
        <= (observed_now - generated_at).total_seconds()
        <= max_age_seconds
    ):
        return _blocked("trust_enrollment_preview_not_fresh", now=observed_now)
    try:
        expected = build_enrollment_preview(
            intake,
            decision,
            trust_registry,
            now=generated_at,
        )
    except (OSError, TypeError, ValueError):
        return _blocked(
            "trust_enrollment_preview_source_not_admissible",
            now=observed_now,
        )
    status = str(receipt.get("status") or "")
    staged = status == "preview_staged"
    if not (
        dict(receipt) == expected
        and status in {"not_required", "awaiting_decision", "preview_staged"}
        and receipt.get("action_required") is staged
        and receipt.get("interrupt_operator") is staged
        and receipt.get("operator_review_required") is staged
        and receipt.get("preview_staged") is staged
        and receipt.get("identity_verification_asserted") is staged
        and receipt.get("trust_enrollment_preview_authorized") is staged
        and receipt.get("trust_enrollment_authorized") is False
        and receipt.get("trust_registry_modified") is False
        and receipt.get("decision_confers_trust") is False
        and receipt.get("private_key_material_requested") is False
        and receipt.get("private_key_material_recorded") is False
        and receipt.get("producer_dispatch_authorized") is False
        and receipt.get("producer_refresh_authorized") is False
        and receipt.get("automatic_execution_allowed") is False
        and receipt.get("execution_authorized") is False
        and receipt.get("deployment_or_restart_authorized") is False
        and receipt.get("protected_operation_executed") is False
        and receipt.get("provider_quota_consumption_allowed") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("delivery_attempted") is False
        and receipt.get("sent") is False
        and receipt.get("secret_values_recorded") is False
    ):
        return _blocked(
            "trust_enrollment_preview_contract_not_admissible",
            now=observed_now,
        )
    binding = dict(receipt.get("binding") or {})
    return _with_integrity(
        {
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "preview_state": status,
            "updated_at": observed_now.isoformat(),
            "generated_at": str(receipt.get("generated_at") or ""),
            "expires_at": str(receipt.get("expires_at") or ""),
            "blocking_reason": "",
            "next_action": str(receipt.get("next_action") or ""),
            "preview_id": str(receipt.get("preview_id") or ""),
            "candidate_review_id": str(
                binding.get("candidate_review_id") or ""
            ),
            "decision_id": str(binding.get("decision_id") or ""),
            "decision": str(binding.get("decision") or ""),
            "current_trust_registry_sha256": str(
                binding.get("current_trust_registry_sha256") or ""
            ),
            "proposed_trust_registry_sha256": str(
                receipt.get("proposed_trust_registry_sha256") or ""
            ),
            "current_registry": dict(receipt.get("current_registry") or {}),
            "proposed_registry": dict(receipt.get("proposed_registry") or {}),
            "registry_diff": dict(receipt.get("registry_diff") or {}),
            "authorization_scope": dict(
                receipt.get("authorization_scope") or {}
            ),
            "progress": {
                "current_evidence_verified": True,
                "intake_binding_verified": True,
                "decision_binding_verified": True,
                "trust_registry_binding_verified": True,
                "preview_integrity_verified": True,
            },
            **_safety_fields(),
            "action_required": staged,
            "interrupt_operator": staged,
            "operator_review_required": staged,
            "preview_staged": staged,
            "identity_verification_asserted": staged,
            "trust_enrollment_preview_authorized": staged,
        }
    )


def _persist_verification(
    verification: Mapping[str, Any],
    *,
    receipt_path: Path,
    verification_path: Path,
) -> dict[str, Any]:
    receipt_target = Path(receipt_path).absolute()
    verification_target = Path(verification_path).absolute()
    receipt, receipt_raw, receipt_digest = trust_intake._private_snapshot(
        receipt_target,
        field="source_refresh_trust_enrollment_preview",
    )
    if not _integrity_verified(receipt):
        raise ValueError("trust_enrollment_preview_persistence_mismatch")
    payload = _with_integrity(
        {
            key: value
            for key, value in verification.items()
            if key != "integrity"
        }
        | {
            "preview_receipt_sha256": receipt_digest,
            "progress": {
                **dict(verification.get("progress") or {}),
                "preview_receipt_persisted": True,
                "verification_receipt_persisted": True,
            },
        }
    )
    atomic_write_bytes(
        verification_target,
        _canonical(payload),
        overwrite=True,
    )
    persisted, verification_raw, verification_digest = (
        trust_intake._private_snapshot(
            verification_target,
            field="source_refresh_trust_enrollment_preview_verification",
        )
    )
    if persisted != payload or not _integrity_verified(persisted):
        raise ValueError("trust_enrollment_preview_verification_persistence_mismatch")
    result = dict(payload)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(receipt_target),
            "verification_path": str(verification_target),
            "preview_receipt_sha256": receipt_digest,
            "preview_receipt_bytes": len(receipt_raw),
            "verification_receipt_sha256": verification_digest,
            "verification_receipt_bytes": len(verification_raw),
            "preview_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def materialize_enrollment_preview(
    intake: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        trust_registry = claims.load_producer_trust_registry(
            trust_registry_path
        )
        receipt = build_enrollment_preview(
            intake,
            decision,
            trust_registry,
            now=observed_now,
        )
        atomic_write_bytes(
            Path(receipt_path).absolute(),
            _canonical(receipt),
            overwrite=True,
        )
        persisted, _raw, _digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_enrollment_preview",
        )
        if persisted != receipt:
            raise ValueError("trust_enrollment_preview_persistence_mismatch")
        verification = verify_enrollment_preview(
            persisted,
            intake=intake,
            decision=decision,
            trust_registry=trust_registry,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if verification.get("status") != "verified":
            raise ValueError("trust_enrollment_preview_verification_failed")
        return _persist_verification(
            verification,
            receipt_path=receipt_path,
            verification_path=verification_path,
        )
    except Exception:
        return _blocked(
            "trust_enrollment_preview_evidence_or_persistence_not_admissible",
            now=observed_now,
        )


def inspect_current_enrollment_preview(
    *,
    decision_dir: Path = trust_decision.DEFAULT_DECISION_DIR,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    try:
        intake = trust_intake.inspect_trust_intake_bundle(
            claim_verification_path=claim_verification_path,
            trust_registry_path=trust_registry_path,
            candidate_dir=candidate_dir,
            receipt_path=intake_receipt_path,
            verification_path=intake_verification_path,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        decision = trust_decision.verify_candidate_review_decision_for_report(
            intake,
            decision_dir=decision_dir,
            now=observed_now,
        )
        trust_registry = claims.load_producer_trust_registry(
            trust_registry_path
        )
        receipt, _receipt_raw, receipt_digest = trust_intake._private_snapshot(
            receipt_path,
            field="source_refresh_trust_enrollment_preview",
        )
        persisted, _verification_raw, _verification_digest = (
            trust_intake._private_snapshot(
                verification_path,
                field="source_refresh_trust_enrollment_preview_verification",
            )
        )
        current = verify_enrollment_preview(
            receipt,
            intake=intake,
            decision=decision,
            trust_registry=trust_registry,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        expected_progress = {
            **dict(current.get("progress") or {}),
            "preview_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
        current_without_variable = {
            key: value
            for key, value in current.items()
            if key not in {"integrity", "progress", "updated_at"}
        }
        persisted_without_variable = {
            key: value
            for key, value in persisted.items()
            if key
            not in {
                "integrity",
                "progress",
                "updated_at",
                "preview_receipt_sha256",
            }
        }
        if not (
            current.get("status") == "verified"
            and persisted.get("schema") == VERIFY_SCHEMA
            and persisted.get("status") == "verified"
            and _integrity_verified(persisted)
            and persisted.get("preview_receipt_sha256") == receipt_digest
            and persisted_without_variable == current_without_variable
            and dict(persisted.get("progress") or {}) == expected_progress
        ):
            raise ValueError("trust_enrollment_preview_bundle_not_current")
    except Exception:
        return _blocked(
            "trust_enrollment_preview_bundle_not_admissible",
            now=observed_now,
        )
    result = dict(persisted)
    result.pop("integrity", None)
    result.update(
        {
            "receipt_path": str(Path(receipt_path).absolute()),
            "verification_path": str(Path(verification_path).absolute()),
            "preview_receipt_sha256": receipt_digest,
            "preview_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a digest-bound trust-registry enrollment preview. This "
            "command never writes the trust registry or enrolls trust."
        )
    )
    parser.add_argument("--decision-dir", type=Path, default=trust_decision.DEFAULT_DECISION_DIR)
    parser.add_argument("--claim-verification", type=Path, default=claims.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--trust-registry", type=Path, default=claims.DEFAULT_TRUST_REGISTRY_PATH)
    parser.add_argument("--candidate-dir", type=Path, default=trust_intake.DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--intake-receipt", type=Path, default=trust_intake.DEFAULT_RECEIPT_PATH)
    parser.add_argument("--intake-verification", type=Path, default=trust_intake.DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION_PATH)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    result = inspect_current_enrollment_preview(
        decision_dir=args.decision_dir,
        claim_verification_path=args.claim_verification,
        trust_registry_path=args.trust_registry,
        candidate_dir=args.candidate_dir,
        intake_receipt_path=args.intake_receipt,
        intake_verification_path=args.intake_verification,
        receipt_path=args.receipt,
        verification_path=args.verification,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
