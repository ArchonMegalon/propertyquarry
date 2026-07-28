from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts import propertyquarry_release_authority_receipts as receipts
from scripts.propertyquarry_release_authority_receipts import (
    AUTHORITATIVE,
    EVIDENCE_STORE_ACKNOWLEDGEMENT,
    LAUNCH_AUTHORITY_DECISION,
    MAX_AUTHORITY_RECEIPT_BYTES,
    PERFORMS_EFFECTS,
    PRODUCTION_READY,
    VALIDATES_DURABLE_STORAGE,
    VALIDATES_FRESHNESS,
    VALIDATES_PAYLOAD_SCHEMA,
    VALIDATES_RELEASE_POLICY,
    VALIDATES_REPLAY,
    VERIFICATION_SCOPE,
    VERIFIES_ENVELOPE_SIGNATURE_ONLY,
    AuthorityReceiptProfile,
    AuthorityReceiptABIError,
    SignedAuthorityReceipt,
    authority_receipt_key_id,
    authority_receipt_signature_message,
    build_evidence_store_payload,
    build_launch_authority_payload,
    canonical_json,
    common_binding_digest,
    sha256_digest,
    sign_authority_receipt,
    verify_authority_receipt,
)


def _key(byte: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([byte]) * 32)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _common_bindings() -> dict[str, object]:
    return {
        "request": {
            "identity": {
                "audience": "propertyquarry-release-control-v2",
                "repository": "example/property",
                "ref": "refs/heads/main",
                "candidate_sha": "a" * 40,
                "workflow_ref": (
                    "example/property/.github/workflows/"
                    "release.yml@refs/heads/main"
                ),
                "workflow_sha": "b" * 40,
                "run_id": "12345",
                "run_attempt": 2,
                "job": "propertyquarry-release-v2",
                "environment": "production",
            },
            "operation": "release-run",
            "request_id": "request-123",
            "request_sha256": _digest("1"),
            "envelope_sha256": _digest("2"),
            "root_policy_sha256": _digest("3"),
        },
        "release": {
            "commit_sha": "a" * 40,
            "image_digest": _digest("4"),
        },
        "gold_operations_verification": {
            "schema": (
                "propertyquarry.flagship-operations-"
                "evidence-verification.v1"
            ),
            "payload_sha256": _digest("5"),
            "deployment_id": "deployment-123",
            "challenge_nonce": "challenge-123",
            "challenge_sha256": _digest("6"),
            "policy_sha256": _digest("7"),
            "replica_ids": ["replica-a", "replica-b"],
            "window": {"start_unix": 1000, "end_unix": 22600},
            "raw_receipt_hashes": {
                "dashboard_render": _digest("8"),
                "structured_log_query": _digest("9"),
                "distributed_trace_query": _digest("0"),
            },
            "result": "verified",
            "cross_link_sha256": _digest("a"),
        },
        "lifecycle": {
            "lifecycle_id": "lifecycle-123",
            "lifecycle_sha256": _digest("b"),
            "fence_token_sha256": _digest("c"),
            "fence_epoch": 7,
        },
    }


def _receipt_pair() -> tuple[SignedAuthorityReceipt, SignedAuthorityReceipt]:
    common = _common_bindings()
    resource_key = _key(0x51)
    evidence_key = _key(0x62)
    launch_payload = build_launch_authority_payload(
        common,
        issued_at=23000,
        expires_at=23500,
    )
    launch = sign_authority_receipt(
        resource_key,
        LAUNCH_AUTHORITY_DECISION,
        launch_payload,
    )
    evidence_payload = build_evidence_store_payload(
        common,
        launch_receipt=launch,
        storage={
            "cas_generation": 11,
            "previous_sha256": _digest("d"),
            "persisted_ack_sha256": _digest("e"),
            "fsynced_ack_sha256": _digest("f"),
        },
        issued_at=23001,
        expires_at=23400,
    )
    evidence = sign_authority_receipt(
        evidence_key,
        EVIDENCE_STORE_ACKNOWLEDGEMENT,
        evidence_payload,
    )
    return launch, evidence


def _unsigned_and_message(
    receipt: SignedAuthorityReceipt,
) -> tuple[bytes, bytes, str]:
    outer = json.loads(receipt.raw)
    signature = outer.pop("signature")
    unsigned = canonical_json(outer)
    message = authority_receipt_signature_message(
        receipt.profile,
        unsigned,
    )
    return unsigned, message, signature


def test_helper_is_explicitly_non_authoritative() -> None:
    assert AUTHORITATIVE is False
    assert PRODUCTION_READY is False
    assert PERFORMS_EFFECTS is False
    assert VERIFICATION_SCOPE == (
        "canonical-envelope-payload-digest-ed25519-only"
    )
    assert VERIFIES_ENVELOPE_SIGNATURE_ONLY is True
    assert VALIDATES_PAYLOAD_SCHEMA is False
    assert VALIDATES_RELEASE_POLICY is False
    assert VALIDATES_FRESHNESS is False
    assert VALIDATES_REPLAY is False
    assert VALIDATES_DURABLE_STORAGE is False


def test_fixed_python_go_cross_language_receipt_vectors() -> None:
    launch, evidence = _receipt_pair()
    launch_unsigned, launch_message, launch_signature = (
        _unsigned_and_message(launch)
    )
    evidence_unsigned, evidence_message, evidence_signature = (
        _unsigned_and_message(evidence)
    )

    assert common_binding_digest(_common_bindings()) == (
        "sha256:cc91112dbeeddba60f21e9a4e8f70b343de29377063a9da8c688ad6c669ea4d1"
    )
    assert authority_receipt_key_id(_key(0x51).public_key()) == (
        "sha256:83ab31e2208ae4a4ecab44dab0e52db23dee6be569017054ef087af1ec505b09"
    )
    assert authority_receipt_key_id(_key(0x62).public_key()) == (
        "sha256:67e828f7bc112276537282b88b9669e6de98f7399e611e91e848d17c2755891c"
    )
    assert sha256_digest(launch_unsigned) == (
        "sha256:6e23c2aa070bb4062d917e7e53ca9e27038d1acadbcd378d7882c1d24ae7e8d5"
    )
    assert hashlib.sha256(launch_message).hexdigest() == (
        "1a1ec53d636c1f52aa72e1c310bc7d5331f5caac335ce83b4705f24cc73e62b3"
    )
    assert launch.payload_sha256 == (
        "sha256:9a8342f7e79ea2f31de82efc3cf0b6ce6f7c925ef3e325ce881f06f992cc719c"
    )
    assert launch_signature == (
        "1O-2chsFytVW03eerShDSU1FxxUWhu0lMXcdf0M1F4ACYwkm9O07tMXIefzGk-sN"
        "CAac1tSpWftQvFW6wOIzBA"
    )
    assert launch.receipt_sha256 == (
        "sha256:3d4da684fe8e24c2771193b14d0a0a82fd5046d4babe248af223b612a6231ac3"
    )
    assert sha256_digest(evidence_unsigned) == (
        "sha256:b2149dc4a292be0747f323997fc8606fe95af32bb74170458da7c6fbb986e89e"
    )
    assert hashlib.sha256(evidence_message).hexdigest() == (
        "3d398a19013282ad2f03839ee2add8de32a117210b056167a6ba4f5e3569a3f3"
    )
    assert evidence.payload_sha256 == (
        "sha256:fc00442f21d1947df545d932f9b0a6b124d05a96f5baab584c2ab7bd59d93a7a"
    )
    assert evidence_signature == (
        "BQL5OoEE-osozPT0TrrtKQUyKj0n5T7KTWUlUk6FaZmqeXLfjVr9Fvp792oNa_Wq"
        "zH5A1JjBQcWzYdux8Y5nBw"
    )
    assert evidence.receipt_sha256 == (
        "sha256:46a3076d57da8cb68f3dfbabf2870b444373cc153f353a8511afc492ecff998d"
    )

    evidence_payload = json.loads(evidence.raw)["payload"]
    assert evidence_payload["launch_decision_payload_sha256"] == (
        launch.payload_sha256
    )
    assert evidence_payload["launch_decision_receipt_sha256"] == (
        launch.receipt_sha256
    )


def test_signed_receipts_are_canonical_newline_free_and_verify() -> None:
    launch, evidence = _receipt_pair()
    for receipt, key in (
        (launch, _key(0x51)),
        (evidence, _key(0x62)),
    ):
        assert not receipt.raw.endswith(b"\n")
        assert receipt.raw == canonical_json(json.loads(receipt.raw))
        assert verify_authority_receipt(
            key.public_key(),
            receipt.profile,
            receipt.raw,
        ) == receipt
        signature = json.loads(receipt.raw)["signature"]
        assert "=" not in signature
        assert len(base64.urlsafe_b64decode(signature + "==")) == 64


def test_signature_domains_and_uint64_lengths_are_distinct() -> None:
    value = b"ab"
    launch = authority_receipt_signature_message(
        LAUNCH_AUTHORITY_DECISION,
        value,
    )
    evidence = authority_receipt_signature_message(
        EVIDENCE_STORE_ACKNOWLEDGEMENT,
        value,
    )
    assert launch != evidence
    assert launch.endswith((2).to_bytes(8, "big") + value)
    assert evidence.endswith((2).to_bytes(8, "big") + value)


def test_evidence_payload_binds_exact_launch_receipt_bytes() -> None:
    launch, _ = _receipt_pair()
    for tampered in (
        SignedAuthorityReceipt(
            profile=launch.profile,
            raw=launch.raw + b" ",
            payload_sha256=launch.payload_sha256,
            receipt_sha256=launch.receipt_sha256,
        ),
        SignedAuthorityReceipt(
            profile=launch.profile,
            raw=launch.raw,
            payload_sha256=_digest("0"),
            receipt_sha256=launch.receipt_sha256,
        ),
    ):
        with pytest.raises(
            AuthorityReceiptABIError,
            match="launch-receipt-invalid",
        ):
            build_evidence_store_payload(
                _common_bindings(),
                launch_receipt=tampered,
                storage={
                    "cas_generation": 11,
                    "previous_sha256": _digest("d"),
                    "persisted_ack_sha256": _digest("e"),
                    "fsynced_ack_sha256": _digest("f"),
                },
                issued_at=23001,
                expires_at=23400,
            )


def test_payload_builders_isolate_canonical_input_objects() -> None:
    common = _common_bindings()
    launch_payload = build_launch_authority_payload(
        common,
        issued_at=23000,
        expires_at=23500,
    )
    launch = sign_authority_receipt(
        _key(0x51),
        LAUNCH_AUTHORITY_DECISION,
        launch_payload,
    )
    storage = {
        "cas_generation": 11,
        "previous_sha256": _digest("d"),
        "persisted_ack_sha256": _digest("e"),
        "fsynced_ack_sha256": _digest("f"),
    }
    evidence_payload = build_evidence_store_payload(
        common,
        launch_receipt=launch,
        storage=storage,
        issued_at=23001,
        expires_at=23400,
    )
    original_launch = canonical_json(launch_payload)
    original_evidence = canonical_json(evidence_payload)

    common["request"]["request_id"] = "mutated"
    common["gold_operations_verification"]["replica_ids"].append(
        "replica-mutated"
    )
    storage["cas_generation"] = 99

    assert canonical_json(launch_payload) == original_launch
    assert canonical_json(evidence_payload) == original_evidence


def test_payload_builder_copies_the_single_canonical_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = _common_bindings()
    original_decode = receipts._decode_canonical_receipt
    mutated = False

    def mutate_source_after_snapshot(raw: bytes) -> dict[str, object]:
        nonlocal mutated
        if not mutated:
            mutated = True
            common["request"]["request_id"] = "mutated-after-snapshot"
        return original_decode(raw)

    monkeypatch.setattr(
        receipts,
        "_decode_canonical_receipt",
        mutate_source_after_snapshot,
    )
    payload = build_launch_authority_payload(
        common,
        issued_at=23000,
        expires_at=23500,
    )

    assert mutated is True
    assert payload["request"]["request_id"] == "request-123"
    assert common["request"]["request_id"] == "mutated-after-snapshot"


def test_canonical_json_rejects_native_excessive_depth() -> None:
    value: object = 0
    for _ in range(33):
        value = [value]
    with pytest.raises(
        AuthorityReceiptABIError,
        match="canonical-json-invalid",
    ):
        canonical_json(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: b" " + raw,
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"decision":"allow"', b'"decision":"deny"'),
        lambda raw: raw.replace(
            b"launch-authority-decision.v2",
            b"launch-authority-decision.v1",
        ),
    ],
)
def test_verifier_rejects_noncanonical_or_tampered_receipts(mutate) -> None:
    launch, _ = _receipt_pair()
    with pytest.raises(AuthorityReceiptABIError):
        verify_authority_receipt(
            _key(0x51).public_key(),
            launch.profile,
            mutate(launch.raw),
        )


def test_canonical_json_matches_native_integer_and_unicode_rules() -> None:
    assert canonical_json(
        {"z": "\U0001f642", "a": "\x7f", "n": -(1 << 63)}
    ) == (
        b'{"a":"\\u007f","n":-9223372036854775808,'
        b'"z":"\\ud83d\\ude42"}'
    )
    for invalid in (1.0, 1 << 63, {"bad": "\ud800"}):
        with pytest.raises(AuthorityReceiptABIError):
            canonical_json(invalid)


class _ForgedProfile:
    schema = "forged"
    producer = "forged"
    signature_domain = b"forged-domain\0"

    def __eq__(self, _other: object) -> bool:
        return True


@pytest.mark.parametrize(
    "profile",
    [
        AuthorityReceiptProfile(
            schema=LAUNCH_AUTHORITY_DECISION.schema,
            producer=LAUNCH_AUTHORITY_DECISION.producer,
            signature_domain=LAUNCH_AUTHORITY_DECISION.signature_domain,
        ),
        _ForgedProfile(),
    ],
)
def test_only_exact_builtin_profile_objects_are_accepted(profile) -> None:
    key = _key(0x51)
    with pytest.raises(AuthorityReceiptABIError):
        authority_receipt_signature_message(profile, b"{}")
    with pytest.raises(AuthorityReceiptABIError):
        sign_authority_receipt(key, profile, {})
    launch, _ = _receipt_pair()
    with pytest.raises(AuthorityReceiptABIError):
        verify_authority_receipt(key.public_key(), profile, launch.raw)
    forged_launch = SignedAuthorityReceipt(
        profile=profile,
        raw=launch.raw,
        payload_sha256=launch.payload_sha256,
        receipt_sha256=launch.receipt_sha256,
    )
    with pytest.raises(
        AuthorityReceiptABIError,
        match="launch-receipt-invalid",
    ):
        build_evidence_store_payload(
            _common_bindings(),
            launch_receipt=forged_launch,
            storage={
                "cas_generation": 11,
                "previous_sha256": _digest("d"),
                "persisted_ack_sha256": _digest("e"),
                "fsynced_ack_sha256": _digest("f"),
            },
            issued_at=23001,
            expires_at=23400,
        )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xef\xbb\xbf{}", "receipt-size-or-encoding-invalid"),
        (b'{"x":"\xff"}', "receipt-json-invalid"),
        (
            b'{"issued_at":' + b"1" * 5000 + b"}",
            "receipt-json-invalid",
        ),
        (b'{"issued_at":1.0}', "receipt-json-invalid"),
        (b'{"issued_at":NaN}', "receipt-json-invalid"),
        (
            b"x" * (MAX_AUTHORITY_RECEIPT_BYTES + 1),
            "receipt-size-or-encoding-invalid",
        ),
    ],
)
def test_parser_failures_are_normalized(raw: bytes, code: str) -> None:
    with pytest.raises(AuthorityReceiptABIError, match=code):
        verify_authority_receipt(
            _key(0x51).public_key(),
            LAUNCH_AUTHORITY_DECISION,
            raw,
        )


def test_verifier_rejects_duplicate_json_keys() -> None:
    launch, _ = _receipt_pair()
    key_id = authority_receipt_key_id(_key(0x51).public_key())
    raw = launch.raw.replace(
        b'{"key_id":',
        f'{{"key_id":"{key_id}","key_id":'.encode("ascii"),
        1,
    )
    with pytest.raises(
        AuthorityReceiptABIError,
        match="receipt-json-invalid",
    ):
        verify_authority_receipt(
            _key(0x51).public_key(),
            LAUNCH_AUTHORITY_DECISION,
            raw,
        )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("extra", True, "receipt-envelope-invalid"),
        ("producer", "other", "receipt-envelope-invalid"),
        ("key_id", _digest("0"), "receipt-envelope-invalid"),
        (
            "payload_sha256",
            _digest("0"),
            "receipt-payload-digest-invalid",
        ),
    ],
)
def test_verifier_rejects_conflicting_envelope_fields(
    field: str,
    value: object,
    code: str,
) -> None:
    launch, _ = _receipt_pair()
    outer = json.loads(launch.raw)
    outer[field] = value
    with pytest.raises(AuthorityReceiptABIError, match=code):
        verify_authority_receipt(
            _key(0x51).public_key(),
            LAUNCH_AUTHORITY_DECISION,
            canonical_json(outer),
        )


@pytest.mark.parametrize("replacement", ["padded", "invalid", "bit-flip"])
def test_verifier_rejects_noncanonical_or_invalid_signatures(
    replacement: str,
) -> None:
    launch, _ = _receipt_pair()
    outer = json.loads(launch.raw)
    encoded = outer["signature"]
    assert type(encoded) is str
    if replacement == "padded":
        outer["signature"] = encoded + "="
        code = "receipt-signature-encoding-invalid"
    elif replacement == "invalid":
        outer["signature"] = "*" + encoded[1:]
        code = "receipt-signature-encoding-invalid"
    else:
        signature = bytearray(base64.urlsafe_b64decode(encoded + "=="))
        signature[0] ^= 1
        outer["signature"] = (
            base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii")
        )
        code = "receipt-signature-invalid"
    with pytest.raises(AuthorityReceiptABIError, match=code):
        verify_authority_receipt(
            _key(0x51).public_key(),
            LAUNCH_AUTHORITY_DECISION,
            canonical_json(outer),
        )


def test_verifier_rejects_wrong_public_key() -> None:
    launch, _ = _receipt_pair()
    with pytest.raises(
        AuthorityReceiptABIError,
        match="receipt-envelope-invalid",
    ):
        verify_authority_receipt(
            _key(0x62).public_key(),
            LAUNCH_AUTHORITY_DECISION,
            launch.raw,
        )
