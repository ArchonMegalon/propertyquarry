#!/usr/bin/env python3
"""Verify the fixed, externally signed PropertyQuarry public-launch receipt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping

if __package__:
    from scripts.propertyquarry_global_governance_attestation import (
        GLOBAL_MARKET_GATE_ID,
        TRUST_STORE_ENV,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from scripts import propertyquarry_global_governance_attestation as governance
else:
    from propertyquarry_global_governance_attestation import (
        GLOBAL_MARKET_GATE_ID,
        TRUST_STORE_ENV,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    import propertyquarry_global_governance_attestation as governance


PUBLIC_LAUNCH_RECEIPT_CONTRACT = "propertyquarry.public_launch_authority.v2"
PUBLIC_LAUNCH_RECEIPT_PATH = Path(
    "/run/propertyquarry/release-control/"
    "propertyquarry-public-launch-authority.v2.json"
)
PUBLIC_LAUNCH_TRUST_STORE_PATH = Path(
    "/etc/propertyquarry/release-control/"
    "global-governance-trust-store.v1.json"
)
PUBLIC_LAUNCH_REQUIREMENTS = (
    "google_play_public_launch",
    "paid_billing_safe_handoff",
    "encrypted_off_host_disaster_recovery",
)

_CANONICAL_REPOSITORY = "ArchonMegalon/propertyquarry"
_RECEIPT_FIELDS = frozenset(
    {
        "contract_name",
        "passed",
        "secret_values_recorded",
        "canonical_repository",
        "envelope_head_sha",
        "runtime_commit_sha",
        "release_image_digest",
        "nonce",
        "requirements",
        "attestation",
    }
)
_REQUIREMENT_FIELDS = frozenset(
    {"status", "evidence_ref", "evidence_sha256"}
)
_GIT_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
_IMAGE_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")
_MAX_RECEIPT_BYTES = 128 * 1024


class PublicLaunchAuthorityError(ValueError):
    """Fail-closed public-launch verification error with a stable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass(frozen=True)
class VerifiedPublicLaunchAuthority:
    """Non-secret verified projection safe for launch-room output."""

    receipt_contract: str
    canonical_repository: str
    envelope_head_sha: str
    runtime_commit_sha: str
    release_image_digest: str
    receipt_sha256: str
    receipt_id: str
    issued_at: str
    expires_at: str
    key_id: str
    authority_id: str
    requirements: Mapping[str, Mapping[str, str]]
    attestation_sha256: str
    trust_store_sha256: str
    public_key_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_contract": self.receipt_contract,
            "canonical_repository": self.canonical_repository,
            "envelope_head_sha": self.envelope_head_sha,
            "runtime_commit_sha": self.runtime_commit_sha,
            "release_image_digest": self.release_image_digest,
            "receipt_sha256": self.receipt_sha256,
            "receipt_id": self.receipt_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
            "authority_id": self.authority_id,
            "requirements": {
                name: dict(row) for name, row in self.requirements.items()
            },
            "attestation_sha256": self.attestation_sha256,
            "trust_store_sha256": self.trust_store_sha256,
            "public_key_sha256": self.public_key_sha256,
        }


def _absolute_lexical(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or any(
        part in {"", ".", ".."} for part in expanded.parts[1:]
    ):
        raise PublicLaunchAuthorityError(
            "public_launch_authority_receipt_path_untrusted"
        )
    return Path(os.path.abspath(os.fspath(expanded)))


def _require_binding(value: object, expected: str, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None and value == expected


def _evidence_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise PublicLaunchAuthorityError(
            "public_launch_authority_requirement_invalid"
        )
    return value


def _requirements(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != set(PUBLIC_LAUNCH_REQUIREMENTS):
        raise PublicLaunchAuthorityError(
            "public_launch_authority_requirements_incomplete"
        )
    result: dict[str, dict[str, str]] = {}
    for name in PUBLIC_LAUNCH_REQUIREMENTS:
        raw_row = value.get(name)
        if not isinstance(raw_row, Mapping) or set(raw_row) != _REQUIREMENT_FIELDS:
            raise PublicLaunchAuthorityError(
                "public_launch_authority_requirement_invalid"
            )
        row = dict(raw_row)
        evidence_sha256 = row.get("evidence_sha256")
        if (
            row.get("status") != "pass"
            or not isinstance(evidence_sha256, str)
            or _IMAGE_DIGEST.fullmatch(evidence_sha256) is None
        ):
            raise PublicLaunchAuthorityError(
                "public_launch_authority_requirement_invalid"
            )
        result[name] = {
            "status": "pass",
            "evidence_ref": _evidence_ref(row.get("evidence_ref")),
            "evidence_sha256": evidence_sha256,
        }
    return result


def verify_public_launch_authority(
    receipt_path: Path,
    *,
    expected_envelope_head_sha: str,
    expected_runtime_commit_sha: str,
    expected_release_image_digest: str,
    observed_at: datetime | None = None,
) -> VerifiedPublicLaunchAuthority:
    """Verify the exact-candidate receipt against fixed external trust."""

    path = _absolute_lexical(receipt_path)
    if path != PUBLIC_LAUNCH_RECEIPT_PATH:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_receipt_path_untrusted"
        )
    configured_trust_store = str(os.getenv(TRUST_STORE_ENV) or "").strip()
    if not configured_trust_store:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_trust_store_missing"
        )
    try:
        configured_trust_path = _absolute_lexical(Path(configured_trust_store))
    except PublicLaunchAuthorityError as exc:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_trust_store_untrusted"
        ) from exc
    if configured_trust_path != PUBLIC_LAUNCH_TRUST_STORE_PATH:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_trust_store_untrusted"
        )

    try:
        receipt_snapshot = governance._read_secure_file(
            path,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            allowed_owner_uids=governance._trusted_owner_uids(),
            reason="public_launch_authority_receipt_file_untrusted",
        )
        receipt = governance._strict_json_bytes(
            receipt_snapshot.body,
            reason="public_launch_authority_receipt_json_invalid",
        )
    except GlobalGovernanceAttestationError as exc:
        raise PublicLaunchAuthorityError(exc.reason) from exc

    canonical_receipt = governance._canonical_json_bytes(receipt)
    if receipt_snapshot.body not in {canonical_receipt, canonical_receipt + b"\n"}:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_receipt_json_noncanonical"
        )
    if set(receipt) != _RECEIPT_FIELDS:
        raise PublicLaunchAuthorityError(
            "public_launch_authority_receipt_contract_invalid"
        )
    if (
        receipt.get("contract_name") != PUBLIC_LAUNCH_RECEIPT_CONTRACT
        or receipt.get("passed") is not True
        or receipt.get("secret_values_recorded") is not False
        or receipt.get("canonical_repository") != _CANONICAL_REPOSITORY
        or not _require_binding(
            receipt.get("envelope_head_sha"),
            expected_envelope_head_sha,
            _GIT_SHA,
        )
        or not _require_binding(
            receipt.get("runtime_commit_sha"),
            expected_runtime_commit_sha,
            _GIT_SHA,
        )
        or not _require_binding(
            receipt.get("release_image_digest"),
            expected_release_image_digest,
            _IMAGE_DIGEST,
        )
        or not isinstance(receipt.get("nonce"), str)
        or _NONCE.fullmatch(str(receipt.get("nonce"))) is None
    ):
        raise PublicLaunchAuthorityError(
            "public_launch_authority_receipt_binding_invalid"
        )

    requirements = _requirements(receipt.get("requirements"))
    unsigned_receipt = {
        name: value for name, value in receipt.items() if name != "attestation"
    }
    payload_sha256 = "sha256:" + hashlib.sha256(
        governance._canonical_json_bytes(unsigned_receipt)
    ).hexdigest()
    expected_subject = {
        "gate_id": GLOBAL_MARKET_GATE_ID,
        "receipt_contract": PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        "release_commit_sha": expected_runtime_commit_sha,
        "release_image_digest": expected_release_image_digest,
        "source_digests": {
            name: row["evidence_sha256"]
            for name, row in requirements.items()
        },
        "payload_sha256": payload_sha256,
    }
    now = observed_at or datetime.now(timezone.utc)
    try:
        verified = verify_global_governance_attestation(
            receipt.get("attestation"),
            expected_subject=expected_subject,
            observed_at=now,
        )
    except GlobalGovernanceAttestationError as exc:
        raise PublicLaunchAuthorityError(exc.reason) from exc

    return VerifiedPublicLaunchAuthority(
        receipt_contract=PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        canonical_repository=_CANONICAL_REPOSITORY,
        envelope_head_sha=expected_envelope_head_sha,
        runtime_commit_sha=expected_runtime_commit_sha,
        release_image_digest=expected_release_image_digest,
        receipt_sha256=receipt_snapshot.sha256,
        receipt_id=hashlib.sha256(str(receipt["nonce"]).encode("ascii")).hexdigest(),
        issued_at=verified.issued_at,
        expires_at=verified.expires_at,
        key_id=verified.key_id,
        authority_id=verified.authority_id,
        requirements=requirements,
        attestation_sha256=verified.attestation_sha256,
        trust_store_sha256=verified.trust_store_sha256,
        public_key_sha256=verified.public_key_sha256,
    )
