from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from app.services import propertyquarry_registration_identity as identity


def test_registration_token_is_bound_to_configured_signing_secret() -> None:
    issued = identity.issue_registration_challenge(
        email="release-security@example.test",
        return_to="/app/search",
        secret="first-release-signing-secret",
        now=1_900_000_000,
    )

    with pytest.raises(
        identity.RegistrationChallengeError,
        match="registration_verification_code_invalid",
    ):
        identity.verify_registration_challenge(
            token=issued.token,
            verification_code=issued.verification_code,
            secret="second-release-signing-secret",
            now=1_900_000_001,
        )

    verified = identity.verify_registration_challenge(
        token=issued.token,
        verification_code=issued.verification_code,
        secret="first-release-signing-secret",
        now=1_900_000_002,
    )
    assert verified.email == "release-security@example.test"
    assert verified.grant.startswith("pqrg2_")


def test_registration_rejects_legacy_predictable_secret() -> None:
    payload = {
        "email": "release-security@example.test",
        "verification_code": "123456",
        "expires_at": 1_900_000_300,
    }
    encoded = (
        base64.urlsafe_b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(
        b"register:prod:local-user",
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_signature = (
        base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )
    forged_token = f"{encoded}.{encoded_signature}"

    with pytest.raises(
        identity.RegistrationChallengeError,
        match="registration_verification_invalid",
    ):
        identity.verify_registration_challenge(
            token=forged_token,
            verification_code="123456",
            secret="configured-release-signing-secret",
            now=1_900_000_001,
        )
