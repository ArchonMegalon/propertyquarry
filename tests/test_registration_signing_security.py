from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from app.api.routes import onboarding


def _container(*, signing_secret: str) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            auth=SimpleNamespace(signing_secret=signing_secret),
        )
    )


def _registration_payload() -> dict[str, object]:
    return {
        "email": "release-security@example.test",
        "verification_code": "123456",
        "expires_at": int(time.time()) + 300,
    }


def test_registration_token_is_bound_to_configured_signing_secret() -> None:
    first = _container(signing_secret="first-release-signing-secret")
    second = _container(signing_secret="second-release-signing-secret")
    payload = _registration_payload()

    token = onboarding._sign_registration_payload(
        container=first,
        payload=payload,
    )

    assert onboarding._verify_registration_payload(
        container=first,
        token=token,
    ) == payload
    assert onboarding._verify_registration_payload(
        container=second,
        token=token,
    ) is None


def test_registration_rejects_legacy_predictable_secret() -> None:
    payload = _registration_payload()
    encoded = onboarding._urlsafe_b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        b"register:prod:local-user",
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    forged_token = (
        f"{encoded}.{onboarding._urlsafe_b64encode(signature)}"
    )

    assert onboarding._verify_registration_payload(
        container=_container(
            signing_secret="configured-release-signing-secret"
        ),
        token=forged_token,
    ) is None
