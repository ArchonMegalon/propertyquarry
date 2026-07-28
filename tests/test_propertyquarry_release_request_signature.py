from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.propertyquarry_release_request_signature import (
    REQUEST_SIGNATURE_DOMAIN,
    REQUEST_SIGNATURE_PREFIX,
    RequestSignatureError,
    request_signature_key_id,
    request_signature_message,
    sign_request,
    verifier_for,
    verify_request_signature,
)


def _key(byte: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([byte]) * 32)


def test_fixed_native_cross_language_signature_vector() -> None:
    private_key = _key(0x41)
    public_key = private_key.public_key()
    message = request_signature_message(b"ab", b"c")
    signature = sign_request(private_key, b"ab", b"c")

    assert message.startswith(REQUEST_SIGNATURE_DOMAIN)
    assert hashlib.sha256(message).hexdigest() == (
        "05789b6993da7a0fa924a22059bd3b8814db996e16138224417f1b4e9746952c"
    )
    assert request_signature_key_id(public_key) == (
        "sha256:21981d07157626519dc4de7c1c09043877f8e66afd3554169bf9496df8419f50"
    )
    assert signature == (
        "ed25519-v2/"
        "sha256:21981d07157626519dc4de7c1c09043877f8e66afd3554169bf9496df8419f50/"
        "x1iXfjTGO6nxlwyEqTUfkekRJXaVnlc8DQ8bREEIybmxMmk73127nwzHi8U6MOv"
        "HFdVcQhl_FhH664kHRK0GCA"
    )
    assert verify_request_signature(public_key, b"ab", b"c", signature) is True
    assert verifier_for(public_key)(b"ab", b"c", signature) is True


def test_framing_distinguishes_component_boundaries() -> None:
    assert request_signature_message(b"ab", b"c") != request_signature_message(
        b"a", b"bc"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace(REQUEST_SIGNATURE_PREFIX, "ed25519-v1", 1),
        lambda value: value.replace("sha256:", "sha512:", 1),
        lambda value: value + "=",
        lambda value: value + "/extra",
        lambda value: value[:-1] + ("A" if value[-1] != "A" else "B"),
    ],
)
def test_verifier_rejects_profile_and_signature_substitution(
    mutate,
) -> None:
    private_key = _key(0x41)
    value = sign_request(private_key, b"payload", b"envelope")
    with pytest.raises(RequestSignatureError):
        verify_request_signature(
            private_key.public_key(),
            b"payload",
            b"envelope",
            mutate(value),
        )


def test_wrong_authority_and_component_substitution_fail() -> None:
    private_key = _key(0x41)
    value = sign_request(private_key, b"payload", b"envelope")
    for public_key, payload, envelope in (
        (_key(0x42).public_key(), b"payload", b"envelope"),
        (private_key.public_key(), b"different", b"envelope"),
        (private_key.public_key(), b"payload", b"different"),
    ):
        with pytest.raises(RequestSignatureError):
            verify_request_signature(public_key, payload, envelope, value)


def test_signer_and_verifier_reject_wrong_types_or_empty_components() -> None:
    private_key = _key(0x41)
    with pytest.raises(RequestSignatureError):
        sign_request(private_key, b"", b"envelope")
    with pytest.raises(RequestSignatureError):
        sign_request(private_key, b"payload", b"")
    with pytest.raises(RequestSignatureError):
        sign_request(object(), b"payload", b"envelope")  # type: ignore[arg-type]
    with pytest.raises(RequestSignatureError):
        verify_request_signature(  # type: ignore[arg-type]
            object(),
            b"payload",
            b"envelope",
            "",
        )


def test_encoding_is_unpadded_raw_64_byte_base64url() -> None:
    value = sign_request(_key(0x41), b"payload", b"envelope")
    encoded = value.rsplit("/", 1)[1]
    assert "=" not in encoded
    assert len(base64.urlsafe_b64decode(encoded + "==")) == 64
