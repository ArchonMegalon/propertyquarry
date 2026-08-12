from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from propertyquarry_global_governance_test_support import (
    install_test_authority,
    signed_attestation,
)
from scripts import propertyquarry_public_launch_authority as public_authority
from scripts.propertyquarry_global_governance_attestation import (
    GLOBAL_MARKET_GATE_ID,
)
from scripts.propertyquarry_public_launch_authority import (
    PUBLIC_LAUNCH_RECEIPT_CONTRACT,
    PublicLaunchAuthorityError,
    verify_public_launch_authority,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
ENVELOPE_SHA = "b" * 40
RUNTIME_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + ("c" * 64)
EVIDENCE_DIGESTS = {
    "google_play_public_launch": "sha256:" + ("d" * 64),
    "paid_billing_safe_handoff": "sha256:" + ("e" * 64),
    "encrypted_off_host_disaster_recovery": "sha256:" + ("f" * 64),
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unsigned_receipt() -> dict[str, object]:
    return {
        "contract_name": PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        "passed": True,
        "secret_values_recorded": False,
        "canonical_repository": "ArchonMegalon/propertyquarry",
        "envelope_head_sha": ENVELOPE_SHA,
        "runtime_commit_sha": RUNTIME_SHA,
        "release_image_digest": IMAGE_DIGEST,
        "nonce": "1" * 32,
        "requirements": {
            "google_play_public_launch": {
                "status": "pass",
                "evidence_ref": "play-console:at-production-access",
                "evidence_sha256": EVIDENCE_DIGESTS[
                    "google_play_public_launch"
                ],
            },
            "paid_billing_safe_handoff": {
                "status": "pass",
                "evidence_ref": "billing:no-second-login-canary",
                "evidence_sha256": EVIDENCE_DIGESTS[
                    "paid_billing_safe_handoff"
                ],
            },
            "encrypted_off_host_disaster_recovery": {
                "status": "pass",
                "evidence_ref": "dr:encrypted-off-host-restore-drill",
                "evidence_sha256": EVIDENCE_DIGESTS[
                    "encrypted_off_host_disaster_recovery"
                ],
            },
        },
    }


def _signed_receipt() -> dict[str, object]:
    unsigned = _unsigned_receipt()
    payload_sha256 = "sha256:" + hashlib.sha256(
        _canonical_bytes(unsigned)
    ).hexdigest()
    return {
        **unsigned,
        "attestation": signed_attestation(
            gate_id=GLOBAL_MARKET_GATE_ID,
            receipt_contract=PUBLIC_LAUNCH_RECEIPT_CONTRACT,
            release_commit_sha=RUNTIME_SHA,
            release_image_digest=IMAGE_DIGEST,
            source_digests=EVIDENCE_DIGESTS,
            payload_sha256=payload_sha256,
            issued_at=NOW - timedelta(minutes=2),
        ),
    }


def _install(
    tmp_path: Path,
    monkeypatch,
    receipt: dict[str, object] | None = None,
) -> Path:
    trust_path = install_test_authority(tmp_path, monkeypatch)
    receipt_path = tmp_path / "public-launch-authority.v2.json"
    receipt_path.write_bytes(_canonical_bytes(receipt or _signed_receipt()) + b"\n")
    receipt_path.chmod(0o600)
    monkeypatch.setattr(
        public_authority,
        "PUBLIC_LAUNCH_RECEIPT_PATH",
        receipt_path,
    )
    monkeypatch.setattr(
        public_authority,
        "PUBLIC_LAUNCH_TRUST_STORE_PATH",
        trust_path,
    )
    return receipt_path


def _verify(receipt_path: Path):
    return verify_public_launch_authority(
        receipt_path,
        expected_envelope_head_sha=ENVELOPE_SHA,
        expected_runtime_commit_sha=RUNTIME_SHA,
        expected_release_image_digest=IMAGE_DIGEST,
        observed_at=NOW,
    )


def test_signed_public_launch_receipt_binds_every_external_requirement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt_path = _install(tmp_path, monkeypatch)

    verified = _verify(receipt_path)

    assert verified.canonical_repository == "ArchonMegalon/propertyquarry"
    assert verified.envelope_head_sha == ENVELOPE_SHA
    assert verified.runtime_commit_sha == RUNTIME_SHA
    assert verified.release_image_digest == IMAGE_DIGEST
    assert set(verified.requirements) == set(EVIDENCE_DIGESTS)
    assert all(
        row["status"] == "pass" for row in verified.requirements.values()
    )
    assert len(verified.receipt_sha256) == 64
    assert len(verified.receipt_id) == 64


def test_envelope_head_is_inside_the_signed_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt = _signed_receipt()
    receipt["envelope_head_sha"] = "9" * 40
    receipt_path = _install(tmp_path, monkeypatch, receipt)

    with pytest.raises(
        PublicLaunchAuthorityError,
        match="global_governance_attestation_subject_mismatch",
    ):
        verify_public_launch_authority(
            receipt_path,
            expected_envelope_head_sha="9" * 40,
            expected_runtime_commit_sha=RUNTIME_SHA,
            expected_release_image_digest=IMAGE_DIGEST,
            observed_at=NOW,
        )


def test_forged_signature_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt = _signed_receipt()
    attestation = dict(receipt["attestation"])
    signature = bytearray(base64.b64decode(attestation["signature_base64"]))
    signature[-1] ^= 1
    attestation["signature_base64"] = base64.b64encode(signature).decode("ascii")
    receipt["attestation"] = attestation
    receipt_path = _install(tmp_path, monkeypatch, receipt)

    with pytest.raises(
        PublicLaunchAuthorityError,
        match="global_governance_attestation_signature_invalid",
    ):
        _verify(receipt_path)


def test_missing_requirement_and_noncanonical_json_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt = _signed_receipt()
    del receipt["requirements"]["paid_billing_safe_handoff"]
    receipt_path = _install(tmp_path, monkeypatch, receipt)

    with pytest.raises(
        PublicLaunchAuthorityError,
        match="public_launch_authority_requirements_incomplete",
    ):
        _verify(receipt_path)

    receipt = _signed_receipt()
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    receipt_path.chmod(0o600)
    with pytest.raises(
        PublicLaunchAuthorityError,
        match="public_launch_authority_receipt_json_noncanonical",
    ):
        _verify(receipt_path)


def test_caller_selected_receipt_or_trust_store_cannot_authorize(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt_path = _install(tmp_path, monkeypatch)
    alternate = tmp_path / "alternate.json"
    alternate.write_bytes(receipt_path.read_bytes())
    alternate.chmod(0o600)

    with pytest.raises(
        PublicLaunchAuthorityError,
        match="public_launch_authority_receipt_path_untrusted",
    ):
        _verify(alternate)

    monkeypatch.setattr(
        public_authority,
        "PUBLIC_LAUNCH_TRUST_STORE_PATH",
        tmp_path / "different-trust-store.json",
    )
    with pytest.raises(
        PublicLaunchAuthorityError,
        match="public_launch_authority_trust_store_untrusted",
    ):
        _verify(receipt_path)
