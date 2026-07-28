#!/usr/bin/env python3
"""Production-compatible PropertyQuarry release-request signature profile.

This module implements only the deterministic Ed25519 request-signature
profile consumed by the installed native broker. It does not validate OIDC,
issue an authority decision, persist replay state, or authorize a release.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Callable, Final, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


REQUEST_SIGNATURE_DOMAIN: Final = (
    b"propertyquarry.release-request-signature.v2\0"
)
REQUEST_SIGNATURE_PREFIX: Final = "ed25519-v2"
MAX_SIGNED_COMPONENT_BYTES: Final = 1_048_576
SIGNATURE_TEXT_BYTES: Final = 169
_KEY_ID_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RequestSignatureError(ValueError):
    """A deterministic request-signature profile failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise RequestSignatureError(code)


def _exact_bytes(value: object, code: str) -> bytes:
    if (
        type(value) is not bytes
        or not value
        or len(value) > MAX_SIGNED_COMPONENT_BYTES
    ):
        _reject(code)
    return value


def request_signature_message(
    signature_payload: bytes,
    canonical_envelope: bytes,
) -> bytes:
    """Return the exact native-compatible domain/length-framed message."""

    payload = _exact_bytes(signature_payload, "signature-payload-invalid")
    envelope = _exact_bytes(canonical_envelope, "canonical-envelope-invalid")
    return (
        REQUEST_SIGNATURE_DOMAIN
        + len(payload).to_bytes(8, byteorder="big", signed=False)
        + payload
        + len(envelope).to_bytes(8, byteorder="big", signed=False)
        + envelope
    )


def request_signature_key_id(public_key: Ed25519PublicKey) -> str:
    """Return the SHA-256 digest of canonical DER SubjectPublicKeyInfo."""

    if not isinstance(public_key, Ed25519PublicKey):
        _reject("public-key-invalid")
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "sha256:" + hashlib.sha256(der).hexdigest()


def sign_request(
    private_key: Ed25519PrivateKey,
    signature_payload: bytes,
    canonical_envelope: bytes,
) -> str:
    """Sign and format one exact request signature string."""

    if not isinstance(private_key, Ed25519PrivateKey):
        _reject("private-key-invalid")
    message = request_signature_message(signature_payload, canonical_envelope)
    signature = private_key.sign(message)
    key_id = request_signature_key_id(private_key.public_key())
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    result = f"{REQUEST_SIGNATURE_PREFIX}/{key_id}/{encoded}"
    if len(result.encode("ascii")) != SIGNATURE_TEXT_BYTES:
        _reject("signature-encoding-invalid")
    return result


def verify_request_signature(
    public_key: Ed25519PublicKey,
    signature_payload: bytes,
    canonical_envelope: bytes,
    request_signature: str,
) -> bool:
    """Verify the exact profile or raise a deterministic profile error."""

    if not isinstance(public_key, Ed25519PublicKey):
        _reject("public-key-invalid")
    if type(request_signature) is not str:
        _reject("signature-profile-invalid")
    try:
        raw_text = request_signature.encode("ascii")
    except UnicodeEncodeError:
        _reject("signature-profile-invalid")
    if len(raw_text) != SIGNATURE_TEXT_BYTES:
        _reject("signature-profile-invalid")
    profile, separator, remainder = request_signature.partition("/")
    key_id, second_separator, encoded = remainder.partition("/")
    if (
        separator != "/"
        or second_separator != "/"
        or "/" in encoded
        or profile != REQUEST_SIGNATURE_PREFIX
        or _KEY_ID_RE.fullmatch(key_id) is None
        or key_id != request_signature_key_id(public_key)
    ):
        _reject("signature-profile-invalid")
    if "=" in encoded:
        _reject("signature-encoding-invalid")
    try:
        signature = base64.b64decode(
            encoded + "==",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error):
        _reject("signature-encoding-invalid")
    if (
        len(signature) != 64
        or base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        != encoded
    ):
        _reject("signature-encoding-invalid")
    message = request_signature_message(signature_payload, canonical_envelope)
    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        _reject("signature-verification-failed")
    return True


def verifier_for(
    public_key: Ed25519PublicKey,
) -> Callable[[bytes, bytes, str], bool]:
    """Return the callback shape used by the request-authority model."""

    if not isinstance(public_key, Ed25519PublicKey):
        _reject("public-key-invalid")

    def verify(
        signature_payload: bytes,
        canonical_envelope: bytes,
        request_signature: str,
    ) -> bool:
        return verify_request_signature(
            public_key,
            signature_payload,
            canonical_envelope,
            request_signature,
        )

    return verify
