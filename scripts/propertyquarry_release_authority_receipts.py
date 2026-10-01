#!/usr/bin/env python3
"""Non-authoritative envelope ABI for native signed authority receipts.

This module mirrors the native canonical JSON, digest, Ed25519 framing, and
receipt-envelope formats. Its verifier checks only canonical envelope bytes,
the payload digest, and the Ed25519 signature. Payload dictionaries are opaque:
this module does not validate their nested schema, authenticate an authority,
evaluate release policy, validate freshness or replay, prove durable storage,
or authorize any release effect.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Final, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


AUTHORITATIVE: Final = False
PRODUCTION_READY: Final = False
PERFORMS_EFFECTS: Final = False
VERIFICATION_SCOPE: Final = "canonical-envelope-payload-digest-ed25519-only"
VERIFIES_ENVELOPE_SIGNATURE_ONLY: Final = True
VALIDATES_PAYLOAD_SCHEMA: Final = False
VALIDATES_RELEASE_POLICY: Final = False
VALIDATES_FRESHNESS: Final = False
VALIDATES_REPLAY: Final = False
VALIDATES_DURABLE_STORAGE: Final = False
MAX_AUTHORITY_RECEIPT_BYTES: Final = 128 * 1024
MAX_CANONICAL_JSON_DEPTH: Final = 32
_MIN_INT64: Final = -(1 << 63)
_MAX_INT64: Final = (1 << 63) - 1
_COMMON_BINDING_KEYS: Final = frozenset(
    {
        "request",
        "release",
        "gold_operations_verification",
        "lifecycle",
    }
)
_STORAGE_KEYS: Final = frozenset(
    {
        "cas_generation",
        "previous_sha256",
        "persisted_ack_sha256",
        "fsynced_ack_sha256",
    }
)
_ENVELOPE_KEYS: Final = frozenset(
    {
        "schema",
        "producer",
        "key_id",
        "payload",
        "payload_sha256",
        "signature",
    }
)


@dataclass(frozen=True)
class AuthorityReceiptProfile:
    """One built-in receipt-envelope profile and its signature domain."""

    schema: str
    producer: str
    signature_domain: bytes


LAUNCH_AUTHORITY_DECISION: Final = AuthorityReceiptProfile(
    schema="propertyquarry.release-control.launch-authority-decision.v2",
    producer="propertyquarry-resource-mediator",
    signature_domain=(
        b"propertyquarry.release-control."
        b"launch-authority-decision.ed25519.v2\0"
    ),
)
EVIDENCE_STORE_ACKNOWLEDGEMENT: Final = AuthorityReceiptProfile(
    schema=(
        "propertyquarry.release-control."
        "evidence-store-acknowledgement.v2"
    ),
    producer="propertyquarry-evidence-authority",
    signature_domain=(
        b"propertyquarry.release-control."
        b"evidence-store-acknowledgement.ed25519.v2\0"
    ),
)


@dataclass(frozen=True)
class SignedAuthorityReceipt:
    """Canonical envelope bytes and digests, conveying no payload authority."""

    profile: AuthorityReceiptProfile
    raw: bytes
    payload_sha256: str
    receipt_sha256: str


class AuthorityReceiptABIError(ValueError):
    """A deterministic reference-ABI failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise AuthorityReceiptABIError(code)


def _is_builtin_profile(value: object) -> bool:
    return type(value) is AuthorityReceiptProfile and (
        value is LAUNCH_AUTHORITY_DECISION
        or value is EVIDENCE_STORE_ACKNOWLEDGEMENT
    )


def _canonical_string(value: str) -> bytes:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _reject("canonical-json-invalid")
    return json.dumps(value, ensure_ascii=True).encode("ascii")


def _append_canonical(
    destination: bytearray,
    value: object,
    depth: int,
) -> None:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        _reject("canonical-json-invalid")
    if value is None:
        destination.extend(b"null")
        return
    if type(value) is bool:
        destination.extend(b"true" if value else b"false")
        return
    if type(value) is int:
        if value < _MIN_INT64 or value > _MAX_INT64:
            _reject("canonical-json-invalid")
        destination.extend(str(value).encode("ascii"))
        return
    if type(value) is str:
        destination.extend(_canonical_string(value))
        return
    if type(value) is list:
        destination.append(ord("["))
        for index, item in enumerate(value):
            if index:
                destination.append(ord(","))
            _append_canonical(destination, item, depth + 1)
        destination.append(ord("]"))
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            _reject("canonical-json-invalid")
        destination.append(ord("{"))
        for index, key in enumerate(sorted(value)):
            if index:
                destination.append(ord(","))
            destination.extend(_canonical_string(key))
            destination.append(ord(":"))
            _append_canonical(destination, value[key], depth + 1)
        destination.append(ord("}"))
        return
    _reject("canonical-json-invalid")


def canonical_json(value: object) -> bytes:
    """Return native-compatible, newline-free canonical JSON bytes."""

    destination = bytearray()
    _append_canonical(destination, value, 0)
    return bytes(destination)


def sha256_digest(value: bytes) -> str:
    """Return the native ``sha256:<lower-hex>`` digest form."""

    if type(value) is not bytes:
        _reject("digest-input-invalid")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def authority_receipt_key_id(public_key: Ed25519PublicKey) -> str:
    """Derive the native key ID from DER SubjectPublicKeyInfo."""

    if not isinstance(public_key, Ed25519PublicKey):
        _reject("public-key-invalid")
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return sha256_digest(der)


def authority_receipt_signature_message(
    profile: AuthorityReceiptProfile,
    canonical_unsigned: bytes,
) -> bytes:
    """Return the profile domain plus uint64-framed unsigned envelope."""

    if (
        not _is_builtin_profile(profile)
        or type(canonical_unsigned) is not bytes
        or not canonical_unsigned
    ):
        _reject("signature-message-invalid")
    return (
        profile.signature_domain
        + len(canonical_unsigned).to_bytes(8, byteorder="big", signed=False)
        + canonical_unsigned
    )


def common_binding_digest(common_bindings: dict[str, object]) -> str:
    """Digest four opaque common binding objects used by both envelopes."""

    if (
        type(common_bindings) is not dict
        or set(common_bindings) != _COMMON_BINDING_KEYS
    ):
        _reject("common-bindings-invalid")
    return sha256_digest(canonical_json(common_bindings))


def build_launch_authority_payload(
    common_bindings: dict[str, object],
    *,
    issued_at: int,
    expires_at: int,
) -> dict[str, object]:
    """Assemble the top-level launch shape without validating nested schema."""

    common_copy = _canonical_object_copy(
        common_bindings,
        "common-bindings-invalid",
    )
    binding_sha256 = common_binding_digest(common_copy)
    _validate_positive_time_window(issued_at, expires_at)
    return {
        **common_copy,
        "decision": "allow",
        "binding_sha256": binding_sha256,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def build_evidence_store_payload(
    common_bindings: dict[str, object],
    *,
    launch_receipt: SignedAuthorityReceipt,
    storage: dict[str, object],
    issued_at: int,
    expires_at: int,
) -> dict[str, object]:
    """Assemble an opaque acknowledgement shape bound to exact launch bytes.

    The acknowledgement text is serialized data, not proof of persistence.
    Nested common or storage semantics are deliberately not validated here.
    """

    common_copy = _canonical_object_copy(
        common_bindings,
        "common-bindings-invalid",
    )
    binding_sha256 = common_binding_digest(common_copy)
    _validate_positive_time_window(issued_at, expires_at)
    if not _has_exact_launch_digest_bindings(launch_receipt):
        _reject("launch-receipt-invalid")
    if type(storage) is not dict or set(storage) != _STORAGE_KEYS:
        _reject("storage-bindings-invalid")
    storage_copy = _canonical_object_copy(storage)
    return {
        **common_copy,
        "acknowledgement": "persisted-and-fsynced",
        "binding_sha256": binding_sha256,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "launch_decision_payload_sha256": (
            launch_receipt.payload_sha256
        ),
        "launch_decision_receipt_sha256": (
            launch_receipt.receipt_sha256
        ),
        "storage": storage_copy,
    }


def _validate_positive_time_window(issued_at: int, expires_at: int) -> None:
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or issued_at < 1
        or expires_at <= issued_at
        or expires_at > _MAX_INT64
    ):
        _reject("receipt-time-window-invalid")


def _unsigned_envelope(
    profile: AuthorityReceiptProfile,
    key_id: str,
    payload: dict[str, object],
) -> tuple[dict[str, object], bytes, str]:
    if not _is_builtin_profile(profile) or type(payload) is not dict:
        _reject("receipt-profile-invalid")
    canonical_payload = canonical_json(payload)
    payload_sha256 = sha256_digest(canonical_payload)
    unsigned: dict[str, object] = {
        "schema": profile.schema,
        "producer": profile.producer,
        "key_id": key_id,
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    return unsigned, canonical_json(unsigned), payload_sha256


def sign_authority_receipt(
    private_key: Ed25519PrivateKey,
    profile: AuthorityReceiptProfile,
    payload: dict[str, object],
) -> SignedAuthorityReceipt:
    """Sign one reference receipt; the result carries no authority."""

    if not isinstance(private_key, Ed25519PrivateKey):
        _reject("private-key-invalid")
    key_id = authority_receipt_key_id(private_key.public_key())
    unsigned, canonical_unsigned, payload_sha256 = _unsigned_envelope(
        profile,
        key_id,
        payload,
    )
    message = authority_receipt_signature_message(
        profile,
        canonical_unsigned,
    )
    signature = base64.urlsafe_b64encode(
        private_key.sign(message)
    ).rstrip(b"=").decode("ascii")
    signed = {**unsigned, "signature": signature}
    raw = canonical_json(signed)
    if len(raw) > MAX_AUTHORITY_RECEIPT_BYTES:
        _reject("receipt-size-invalid")
    return SignedAuthorityReceipt(
        profile=profile,
        raw=raw,
        payload_sha256=payload_sha256,
        receipt_sha256=sha256_digest(raw),
    )


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("receipt-json-invalid")
        result[key] = value
    return result


def _reject_noninteger(_: str) -> NoReturn:
    _reject("receipt-json-invalid")


def _parse_bounded_int(value: str) -> int:
    if type(value) is not str:
        _reject("receipt-json-invalid")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    limit = "9223372036854775808" if negative else "9223372036854775807"
    if (
        not digits
        or not digits.isascii()
        or not digits.isdigit()
        or len(digits) > len(limit)
        or (len(digits) == len(limit) and digits > limit)
    ):
        _reject("receipt-json-invalid")
    try:
        return int(value, 10)
    except (TypeError, ValueError, OverflowError):
        _reject("receipt-json-invalid")


def _canonical_object_copy(
    value: object,
    invalid_code: str = "canonical-json-invalid",
) -> dict[str, object]:
    if type(value) is not dict:
        _reject(invalid_code)
    snapshot = canonical_json(value)
    return _decode_canonical_receipt(snapshot)


def _decode_canonical_receipt(raw: bytes) -> dict[str, object]:
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_AUTHORITY_RECEIPT_BYTES
        or raw.startswith(b"\xef\xbb\xbf")
    ):
        _reject("receipt-size-or-encoding-invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            parse_int=_parse_bounded_int,
            parse_float=_reject_noninteger,
            parse_constant=_reject_noninteger,
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except AuthorityReceiptABIError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        _reject("receipt-json-invalid")
    if type(value) is not dict or canonical_json(value) != raw:
        _reject("receipt-not-canonical")
    return value


def _has_exact_launch_digest_bindings(value: object) -> bool:
    if (
        type(value) is not SignedAuthorityReceipt
        or not _is_builtin_profile(value.profile)
        or value.profile is not LAUNCH_AUTHORITY_DECISION
        or sha256_digest(value.raw) != value.receipt_sha256
    ):
        return False
    try:
        outer = _decode_canonical_receipt(value.raw)
        payload = outer.get("payload")
        if (
            set(outer) != _ENVELOPE_KEYS
            or outer.get("schema") != LAUNCH_AUTHORITY_DECISION.schema
            or outer.get("producer")
            != LAUNCH_AUTHORITY_DECISION.producer
            or type(payload) is not dict
        ):
            return False
        exact_payload_sha256 = sha256_digest(canonical_json(payload))
        return (
            outer.get("payload_sha256") == exact_payload_sha256
            and value.payload_sha256 == exact_payload_sha256
        )
    except AuthorityReceiptABIError:
        return False


def verify_authority_receipt(
    public_key: Ed25519PublicKey,
    profile: AuthorityReceiptProfile,
    raw: bytes,
) -> SignedAuthorityReceipt:
    """Verify only canonical envelope, payload digest, and Ed25519 signature.

    Success does not validate the payload schema or any policy, freshness,
    replay, authority, or storage assertion carried by that opaque payload.
    """

    if not isinstance(public_key, Ed25519PublicKey):
        _reject("public-key-invalid")
    if not _is_builtin_profile(profile):
        _reject("receipt-profile-invalid")
    outer = _decode_canonical_receipt(raw)
    expected_key_id = authority_receipt_key_id(public_key)
    if (
        set(outer) != _ENVELOPE_KEYS
        or outer.get("schema") != profile.schema
        or outer.get("producer") != profile.producer
        or outer.get("key_id") != expected_key_id
        or type(outer.get("payload")) is not dict
        or type(outer.get("payload_sha256")) is not str
        or type(outer.get("signature")) is not str
    ):
        _reject("receipt-envelope-invalid")
    payload = outer["payload"]
    assert type(payload) is dict
    payload_sha256 = sha256_digest(canonical_json(payload))
    if outer["payload_sha256"] != payload_sha256:
        _reject("receipt-payload-digest-invalid")
    unsigned = {
        "schema": outer["schema"],
        "producer": outer["producer"],
        "key_id": outer["key_id"],
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    encoded = outer["signature"]
    assert type(encoded) is str
    if "=" in encoded:
        _reject("receipt-signature-encoding-invalid")
    try:
        signature = base64.b64decode(
            encoded + "==",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error):
        _reject("receipt-signature-encoding-invalid")
    if (
        len(signature) != 64
        or base64.urlsafe_b64encode(signature)
        .rstrip(b"=")
        .decode("ascii")
        != encoded
    ):
        _reject("receipt-signature-encoding-invalid")
    message = authority_receipt_signature_message(
        profile,
        canonical_json(unsigned),
    )
    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        _reject("receipt-signature-invalid")
    return SignedAuthorityReceipt(
        profile=profile,
        raw=raw,
        payload_sha256=payload_sha256,
        receipt_sha256=sha256_digest(raw),
    )
