from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from scripts import validate_propertyquarry_release_protocol as protocol


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_propertyquarry_release_protocol.py"
SCHEMA = ROOT / "docs" / "propertyquarry-release-control-protocol.v1.schema.json"


def _draft202012_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_jsonschema_uri_format_backend_is_active_and_rejects_malformed_uris() -> None:
    validator = Draft202012Validator(
        {"type": "string", "format": "uri"},
        format_checker=FormatChecker(),
    )

    validator.validate("https://authority.invalid/v2")
    for malformed in (
        "https://authority.invalid/%ZZ",
        "https://[:::]/v2",
    ):
        with pytest.raises(ValidationError):
            validator.validate(malformed)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _release() -> dict[str, Any]:
    return {
        "release_sha": "a" * 40,
        "candidate_artifact_digest": _digest("a"),
        "web_image_digest": _digest("b"),
        "render_image_digest": _digest("e"),
        "controller_digest": _digest("c"),
        "controller_manifest_digest": _digest("d"),
        "policy_digests": {
            "canonical_compose_plan": _digest("1"),
            "database_fence_policy": _digest("2"),
            "drain_keyring": _digest("3"),
            "monitoring_tools": _digest("4"),
            "monitoring_topology": _digest("5"),
            "operator_gateway_trust": _digest("6"),
        },
    }


def _signature(value: Any) -> dict[str, Any]:
    if "requested_effect" in value:
        schema = protocol.SCHEMA_SIGNED_REQUEST
        body_field = "payload"
    elif "disposition" in value:
        schema = protocol.SCHEMA_PREFLIGHT_DISPOSITION
        body_field = "preflight"
    elif "receipt_id" in value:
        schema = protocol.SCHEMA_CONTROLLER_RECEIPT
        body_field = "receipt"
    else:
        raise AssertionError("unknown signed test body")
    signature = {
        "algorithm": "ed25519",
        "key_id": "release/prod/2026-07",
        "encoding": "base64",
        "signed_preimage_sha256": _digest("0"),
        "value": base64.b64encode(b"s" * 64).decode("ascii"),
    }
    envelope = {
        "schema": schema,
        "version": 1,
        body_field: value,
        "signature": signature,
    }
    signature["signed_preimage_sha256"] = protocol.signed_preimage_sha256(
        envelope,
        body_field,
    )
    return signature


def _request(operation: str = "deploy-preflight") -> dict[str, Any]:
    mode = "production" if operation.startswith("deploy-") else "candidate"
    mutation = "forbidden" if operation.endswith("-preflight") else "controller-policy-gated"
    payload = {
        "operation": operation,
        "mode": mode,
        "audience": "propertyquarry-release-controller",
        "host": "propertyquarry.com",
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "nonce": "ab" * 16,
        "issued_at": "2026-07-16T10:00:00Z",
        "expires_at": "2026-07-16T10:05:00Z",
        "cas": {
            "namespace": "propertyquarry/releases",
            "challenge": _digest("7"),
            "expected_counter": 41,
        },
        "release": _release(),
        "requested_effect": {"mutation": mutation},
    }
    return {
        "schema": "propertyquarry.release.signed-request",
        "version": 1,
        "payload": payload,
        "signature": _signature(payload),
    }


def _request_binding(operation: str) -> dict[str, Any]:
    request = _request(operation)["payload"]
    return {
        "request_transport_sha256": _digest("8"),
        "request_id": request["request_id"],
        "nonce": request["nonce"],
        "operation": operation,
        "mode": request["mode"],
        "audience": request["audience"],
        "host": request["host"],
        "cas_namespace": request["cas"]["namespace"],
        "cas_challenge": request["cas"]["challenge"],
        "cas_expected_counter": request["cas"]["expected_counter"],
        "release": request["release"],
    }


def _controller_binding() -> dict[str, Any]:
    return {
        "controller_id": "propertyquarry-release-controller/prod",
        "manifest_digest": _digest("d"),
        "binary_digest": _digest("c"),
        "protocol_version": 1,
    }


def _preflight() -> dict[str, Any]:
    preflight = {
        "request": _request_binding("deploy-preflight"),
        "controller": _controller_binding(),
        "evaluated_at": "2026-07-16T10:00:30Z",
        "disposition": "ready",
        "mutation_performed": False,
        "cas_consumed": False,
        "checks": [
            {"id": "request.signature", "status": "pass", "code": "SIGNATURE_VALID"},
            {"id": "release.bindings", "status": "pass", "code": "BINDINGS_MATCH"},
        ],
    }
    return {
        "schema": "propertyquarry.release.preflight-disposition",
        "version": 1,
        "preflight": preflight,
        "signature": _signature(preflight),
    }


def _receipt(operation: str = "deploy-run") -> dict[str, Any]:
    production = operation == "deploy-run"
    receipt = {
        "receipt_id": "223e4567-e89b-42d3-b456-426614174000",
        "request": _request_binding(operation),
        "controller": _controller_binding(),
        "started_at": "2026-07-16T10:00:00Z",
        "finished_at": "2026-07-16T10:06:00Z",
        "outcome": "succeeded",
        "mutation": {
            "performed": True,
            "containment_before_candidate_validation": True,
            "database_changed": True,
            "traffic_changed": production,
            "rollback_performed": False,
        },
        "cas_commit": {
            "committed": True,
            "namespace": "propertyquarry/releases",
            "challenge": _digest("7"),
            "previous_counter": 41,
            "committed_counter": 42,
            "seal_digest": _digest("9"),
        },
        "evidence": [
            {"kind": "runtime-smoke", "digest": _digest("e")},
            {"kind": "rollback-proof", "digest": _digest("f")},
        ],
    }
    return {
        "schema": "propertyquarry.release.controller-receipt",
        "version": 1,
        "receipt": receipt,
        "signature": _signature(receipt),
    }


def _manifest() -> dict[str, Any]:
    return {
        "schema": "propertyquarry.release.controller-manifest",
        "version": 1,
        "controller_id": "propertyquarry-release-controller/prod",
        "protocol_version": 1,
        "audience": "propertyquarry-release-controller",
        "host": "propertyquarry.com",
        "binary_digest": _digest("c"),
        "build_release_sha": "a" * 40,
        "issued_at": "2026-07-16T09:00:00Z",
        "supported_operations": [
            "deploy-run",
            "deploy-preflight",
            "candidate-run",
            "candidate-preflight",
        ],
        "supported_signature_algorithms": ["ed25519"],
        "request_max_bytes": 1_048_576,
        "request_ttl_max_seconds": 900,
        "canonicalization": "propertyquarry-json-sort-keys-v1",
        "policy_digests": _release()["policy_digests"],
    }


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        ("signed-request", _request()),
        ("preflight-disposition", _preflight()),
        ("controller-receipt", _receipt()),
        ("controller-manifest", _manifest()),
    ],
)
def test_protocol_v1_accepts_each_conformant_document(kind: str, document: dict[str, Any]) -> None:
    assert protocol.validate_document(document, kind) == kind
    assert protocol.validate_document(document, "auto") == document["schema"]
    assert list(_draft202012_validator().iter_errors(document)) == []


def test_protocol_parser_rejects_duplicate_keys_at_any_depth() -> None:
    with pytest.raises(protocol.ProtocolValidationError, match="duplicate object key 'nonce'"):
        protocol.load_document_bytes(b'{"payload":{"nonce":"a","nonce":"b"}}')


@pytest.mark.parametrize("raw", [b'{"value":NaN}', b'{"value":Infinity}'])
def test_protocol_parser_rejects_nonstandard_numbers(raw: bytes) -> None:
    with pytest.raises(protocol.ProtocolValidationError, match="non-standard JSON number"):
        protocol.load_document_bytes(raw)


def test_protocol_parser_enforces_transport_bound() -> None:
    with pytest.raises(protocol.ProtocolValidationError, match="exceeds 1048576 bytes"):
        protocol.load_document_bytes(b" " * (protocol.MAX_DOCUMENT_BYTES + 1))


def test_signed_request_rejects_unknown_nested_property() -> None:
    request = _request()
    request["payload"]["release"]["branch"] = "main"
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match="unknown properties: branch"):
        protocol.validate_signed_request(request)


@pytest.mark.parametrize(
    ("operation", "mutation"),
    [
        ("deploy-preflight", "controller-policy-gated"),
        ("candidate-preflight", "controller-policy-gated"),
        ("deploy-run", "forbidden"),
        ("candidate-run", "forbidden"),
    ],
)
def test_signed_request_operation_strictly_bounds_mutation_intent(
    operation: str,
    mutation: str,
) -> None:
    request = _request(operation)
    request["payload"]["requested_effect"]["mutation"] = mutation
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match="requested_effect.mutation"):
        protocol.validate_signed_request(request)


def test_signed_request_rejects_operation_mode_mismatch() -> None:
    request = _request("deploy-preflight")
    request["payload"]["mode"] = "candidate"
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match="does not match operation"):
        protocol.validate_signed_request(request)


@pytest.mark.parametrize(
    ("field_path", "replacement", "message"),
    [
        (("nonce",), "a" * 31, "invalid format"),
        (("release", "release_sha"), "main", "invalid format"),
        (("release", "web_image_digest"), "propertyquarry:latest", "invalid format"),
    ],
)
def test_signed_request_rejects_mutable_or_malformed_release_identity(
    field_path: tuple[str, ...],
    replacement: str,
    message: str,
) -> None:
    request = _request()
    target = request["payload"]
    for field in field_path[:-1]:
        target = target[field]
    target[field_path[-1]] = replacement
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match=message):
        protocol.validate_signed_request(request)


def test_signed_request_requires_every_fixed_policy_digest() -> None:
    request = _request()
    del request["payload"]["release"]["policy_digests"]["database_fence_policy"]
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match="database_fence_policy"):
        protocol.validate_signed_request(request)


def test_signed_request_rejects_lifetime_over_900_seconds() -> None:
    request = _request()
    request["payload"]["expires_at"] = "2026-07-16T10:15:01Z"
    request["signature"] = _signature(request["payload"])
    with pytest.raises(protocol.ProtocolValidationError, match="at most 900 seconds"):
        protocol.validate_signed_request(request)


def test_signed_request_detects_signed_preimage_mismatch_without_claiming_trust() -> None:
    request = _request()
    request["payload"]["cas"]["expected_counter"] = 42
    with pytest.raises(
        protocol.ProtocolValidationError,
        match="does not bind the canonical domain-separated signed preimage",
    ):
        protocol.validate_signed_request(request)


def test_signature_preimage_binds_key_id() -> None:
    request = _request()
    request["signature"]["key_id"] = "release/prod/downgraded-role"
    with pytest.raises(
        protocol.ProtocolValidationError,
        match="does not bind the canonical domain-separated signed preimage",
    ):
        protocol.validate_signed_request(request)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "propertyquarry.release.signed-request.v0"),
        ("version", 0),
    ],
)
def test_signature_preimage_domain_separates_envelope_schema_and_version(
    field: str,
    replacement: Any,
) -> None:
    request = _request()
    original_digest = request["signature"]["signed_preimage_sha256"]
    request[field] = replacement
    assert protocol.signed_preimage_sha256(request, "payload") != original_digest
    with pytest.raises(protocol.ProtocolValidationError, match=field):
        protocol.validate_signed_request(request)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("algorithm", "rsa-sha256", "must equal 'ed25519'"),
        ("value", base64.b64encode(b"s" * 63).decode("ascii"), "exactly 64 bytes"),
    ],
)
def test_protocol_v1_prevents_signature_algorithm_confusion_and_wrong_length(
    field: str,
    value: str,
    message: str,
) -> None:
    request = _request()
    request["signature"][field] = value
    with pytest.raises(protocol.ProtocolValidationError, match=message):
        protocol.validate_signed_request(request)


def test_schema_and_validator_reject_noncanonical_base64_padding_bits() -> None:
    request = _request()
    canonical = request["signature"]["value"]
    request["signature"]["value"] = canonical[:-3] + "x=="

    assert list(_draft202012_validator().iter_errors(request))
    with pytest.raises(protocol.ProtocolValidationError, match="canonical padded base64"):
        protocol.validate_signed_request(request)


def test_schema_patterns_require_absolute_end_and_reject_terminal_line_breaks() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    patterns: list[str] = []

    def collect_patterns(value: Any) -> None:
        if isinstance(value, dict):
            pattern = value.get("pattern")
            if isinstance(pattern, str):
                patterns.append(pattern)
            for child in value.values():
                collect_patterns(child)
        elif isinstance(value, list):
            for child in value:
                collect_patterns(child)

    collect_patterns(schema)
    assert patterns
    assert [pattern for pattern in patterns if not pattern.endswith("$(?![\\s\\S])")] == []

    release_line_break = _request()
    release_line_break["payload"]["release"]["release_sha"] = "a" * 40 + "\n"
    release_line_break["signature"] = _signature(release_line_break["payload"])
    assert list(_draft202012_validator().iter_errors(release_line_break))
    with pytest.raises(protocol.ProtocolValidationError, match="invalid format"):
        protocol.validate_signed_request(release_line_break)

    key_line_break = _request()
    key_line_break["signature"]["key_id"] = "release/prod/2026-07\n"
    key_line_break["signature"]["signed_preimage_sha256"] = protocol.signed_preimage_sha256(
        key_line_break,
        "payload",
    )
    assert list(_draft202012_validator().iter_errors(key_line_break))
    with pytest.raises(protocol.ProtocolValidationError, match="invalid format"):
        protocol.validate_signed_request(key_line_break)


@pytest.mark.parametrize(
    ("field", "value"),
    [("mutation_performed", True), ("cas_consumed", True)],
)
def test_preflight_disposition_cannot_claim_mutation_or_cas_consumption(
    field: str,
    value: bool,
) -> None:
    disposition = _preflight()
    disposition["preflight"][field] = value
    disposition["signature"] = _signature(disposition["preflight"])
    with pytest.raises(protocol.ProtocolValidationError, match=field):
        protocol.validate_preflight_disposition(disposition)


def test_preflight_disposition_rejects_run_operation() -> None:
    disposition = _preflight()
    disposition["preflight"]["request"] = _request_binding("deploy-run")
    disposition["signature"] = _signature(disposition["preflight"])
    with pytest.raises(protocol.ProtocolValidationError, match="request.operation"):
        protocol.validate_preflight_disposition(disposition)


def test_preflight_disposition_requires_check_consensus() -> None:
    disposition = _preflight()
    disposition["preflight"]["checks"][0]["status"] = "fail"
    disposition["signature"] = _signature(disposition["preflight"])
    with pytest.raises(protocol.ProtocolValidationError, match="ready requires every check"):
        protocol.validate_preflight_disposition(disposition)

    disposition["preflight"]["disposition"] = "not-ready"
    disposition["signature"] = _signature(disposition["preflight"])
    protocol.validate_preflight_disposition(disposition)

    disposition["preflight"]["checks"][1]["status"] = "not-run"
    disposition["signature"] = _signature(disposition["preflight"])
    with pytest.raises(protocol.ProtocolValidationError, match="no not-run checks"):
        protocol.validate_preflight_disposition(disposition)

    disposition["preflight"]["disposition"] = "indeterminate"
    disposition["signature"] = _signature(disposition["preflight"])
    protocol.validate_preflight_disposition(disposition)


def test_preflight_disposition_is_signed_and_payload_bound() -> None:
    disposition = _preflight()
    disposition["preflight"]["evaluated_at"] = "2026-07-16T10:00:31Z"
    with pytest.raises(
        protocol.ProtocolValidationError,
        match="does not bind the canonical domain-separated signed preimage",
    ):
        protocol.validate_preflight_disposition(disposition)


def test_schema_and_validator_reject_exact_duplicate_checks() -> None:
    disposition = _preflight()
    disposition["preflight"]["checks"].append(
        copy.deepcopy(disposition["preflight"]["checks"][0])
    )
    disposition["signature"] = _signature(disposition["preflight"])

    assert list(_draft202012_validator().iter_errors(disposition))
    with pytest.raises(protocol.ProtocolValidationError, match="duplicate check id"):
        protocol.validate_preflight_disposition(disposition)


def test_same_check_id_with_different_body_is_explicitly_semantic_only() -> None:
    disposition = _preflight()
    disposition["preflight"]["checks"][1]["id"] = disposition["preflight"]["checks"][0]["id"]
    disposition["signature"] = _signature(disposition["preflight"])

    assert list(_draft202012_validator().iter_errors(disposition)) == []
    with pytest.raises(protocol.ProtocolValidationError, match="duplicate check id"):
        protocol.validate_preflight_disposition(disposition)


@pytest.mark.parametrize("document_kind", ["preflight", "receipt"])
@pytest.mark.parametrize(
    ("controller_field", "release_field"),
    [
        ("binary_digest", "controller_digest"),
        ("manifest_digest", "controller_manifest_digest"),
    ],
)
def test_response_controller_identity_matches_signed_request_release_binding(
    document_kind: str,
    controller_field: str,
    release_field: str,
) -> None:
    if document_kind == "preflight":
        document = _preflight()
        payload = document["preflight"]
        validator = protocol.validate_preflight_disposition
    else:
        document = _receipt()
        payload = document["receipt"]
        validator = protocol.validate_controller_receipt
    payload["controller"][controller_field] = _digest("0")
    document["signature"] = _signature(payload)
    with pytest.raises(
        protocol.ProtocolValidationError,
        match=f"does not match request.release.{release_field}",
    ):
        validator(document)


def test_controller_receipt_is_bound_to_run_operation_and_cas_challenge() -> None:
    receipt = _receipt()
    receipt["receipt"]["request"] = _request_binding("deploy-preflight")
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="request.operation"):
        protocol.validate_controller_receipt(receipt)

    receipt = _receipt()
    receipt["receipt"]["cas_commit"]["challenge"] = _digest("0")
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="signed-request binding"):
        protocol.validate_controller_receipt(receipt)


@pytest.mark.parametrize(
    ("cas_field", "replacement"),
    [
        ("namespace", "propertyquarry/other-releases"),
        ("previous_counter", 40),
    ],
)
def test_controller_receipt_cas_commit_matches_full_signed_request_tuple(
    cas_field: str,
    replacement: Any,
) -> None:
    receipt = _receipt()
    receipt["receipt"]["cas_commit"][cas_field] = replacement
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="signed-request binding"):
        protocol.validate_controller_receipt(receipt)


def test_candidate_receipt_cannot_claim_production_traffic_change() -> None:
    receipt = _receipt("candidate-run")
    receipt["receipt"]["mutation"]["traffic_changed"] = True
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="cannot change production traffic"):
        protocol.validate_controller_receipt(receipt)


def test_controller_receipt_requires_single_step_cas_commit() -> None:
    receipt = _receipt()
    receipt["receipt"]["cas_commit"]["committed_counter"] = 43
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="advance exactly once"):
        protocol.validate_controller_receipt(receipt)


def test_receipt_mutation_requires_prior_containment() -> None:
    receipt = _receipt()
    receipt["receipt"]["mutation"]["containment_before_candidate_validation"] = False
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="whenever mutation was performed"):
        protocol.validate_controller_receipt(receipt)

    receipt = _receipt()
    receipt["receipt"]["mutation"]["performed"] = False
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="subordinate mutation flags"):
        protocol.validate_controller_receipt(receipt)


@pytest.mark.parametrize(
    ("mutation_field", "value", "message"),
    [
        ("performed", False, "succeeded requires mutation"),
        ("rollback_performed", True, "only for a rolled-back outcome"),
        ("traffic_changed", False, "successful deploy-run requires"),
    ],
)
def test_succeeded_receipt_rejects_contradictory_mutation_story(
    mutation_field: str,
    value: bool,
    message: str,
) -> None:
    receipt = _receipt()
    receipt["receipt"]["mutation"][mutation_field] = value
    if mutation_field == "performed":
        for subordinate in (
            "containment_before_candidate_validation",
            "database_changed",
            "traffic_changed",
            "rollback_performed",
        ):
            receipt["receipt"]["mutation"][subordinate] = False
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match=message):
        protocol.validate_controller_receipt(receipt)


def test_succeeded_receipt_requires_committed_cas_and_evidence() -> None:
    receipt = _receipt()
    receipt["receipt"]["cas_commit"]["committed"] = False
    receipt["receipt"]["cas_commit"]["committed_counter"] = 41
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="committed external CAS seal"):
        protocol.validate_controller_receipt(receipt)

    receipt = _receipt()
    receipt["receipt"]["evidence"] = []
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="at least one evidence binding"):
        protocol.validate_controller_receipt(receipt)


def test_schema_and_validator_reject_exact_duplicate_evidence_bindings() -> None:
    receipt = _receipt()
    receipt["receipt"]["evidence"].append(
        copy.deepcopy(receipt["receipt"]["evidence"][0])
    )
    receipt["signature"] = _signature(receipt["receipt"])

    assert list(_draft202012_validator().iter_errors(receipt))
    with pytest.raises(protocol.ProtocolValidationError, match="duplicate evidence binding"):
        protocol.validate_controller_receipt(receipt)


def test_rejected_receipt_requires_no_mutation_and_no_cas_commit() -> None:
    receipt = _receipt()
    receipt["receipt"]["outcome"] = "rejected"
    receipt["receipt"]["mutation"] = {
        "performed": False,
        "containment_before_candidate_validation": False,
        "database_changed": False,
        "traffic_changed": False,
        "rollback_performed": False,
    }
    receipt["receipt"]["cas_commit"]["committed"] = False
    receipt["receipt"]["cas_commit"]["committed_counter"] = 41
    receipt["signature"] = _signature(receipt["receipt"])
    protocol.validate_controller_receipt(receipt)

    mutated = copy.deepcopy(receipt)
    mutated["receipt"]["mutation"]["performed"] = True
    mutated["receipt"]["mutation"]["containment_before_candidate_validation"] = True
    mutated["signature"] = _signature(mutated["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="rejected requires no mutation"):
        protocol.validate_controller_receipt(mutated)

    committed = copy.deepcopy(receipt)
    committed["receipt"]["cas_commit"]["committed"] = True
    committed["receipt"]["cas_commit"]["committed_counter"] = 42
    committed["signature"] = _signature(committed["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="rejected requires no CAS commit"):
        protocol.validate_controller_receipt(committed)


@pytest.mark.parametrize("outcome", ["failed", "rolled-back"])
def test_non_success_terminal_receipts_require_external_cas_seal(outcome: str) -> None:
    receipt = _receipt()
    receipt["receipt"]["outcome"] = outcome
    if outcome == "rolled-back":
        receipt["receipt"]["mutation"]["rollback_performed"] = True
    receipt["signature"] = _signature(receipt["receipt"])
    protocol.validate_controller_receipt(receipt)

    receipt["receipt"]["cas_commit"]["committed"] = False
    receipt["receipt"]["cas_commit"]["committed_counter"] = 41
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="committed external CAS seal"):
        protocol.validate_controller_receipt(receipt)


def test_rolled_back_receipt_requires_rollback_fact() -> None:
    receipt = _receipt()
    receipt["receipt"]["outcome"] = "rolled-back"
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="true for a rolled-back outcome"):
        protocol.validate_controller_receipt(receipt)


def test_failed_receipt_cannot_claim_completed_rollback() -> None:
    receipt = _receipt()
    receipt["receipt"]["outcome"] = "failed"
    receipt["receipt"]["mutation"]["rollback_performed"] = True
    receipt["signature"] = _signature(receipt["receipt"])
    with pytest.raises(protocol.ProtocolValidationError, match="only for a rolled-back outcome"):
        protocol.validate_controller_receipt(receipt)


def test_controller_manifest_is_compatibility_metadata_not_an_open_extension_point() -> None:
    manifest = _manifest()
    manifest["supported_operations"][-1] = "deploy-run"
    with pytest.raises(protocol.ProtocolValidationError, match="each protocol-v1 operation exactly once"):
        protocol.validate_controller_manifest(manifest)

    manifest = _manifest()
    manifest["trust_root"] = "/candidate/controlled/key"
    with pytest.raises(protocol.ProtocolValidationError, match="unknown properties: trust_root"):
        protocol.validate_controller_manifest(manifest)

    manifest = _manifest()
    manifest["supported_signature_algorithms"] = ["ed25519", "rsa-sha256"]
    with pytest.raises(protocol.ProtocolValidationError, match=r"must equal \['ed25519'\]"):
        protocol.validate_controller_manifest(manifest)


def test_json_schema_is_closed_for_every_object_definition() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    object_nodes: list[tuple[str, dict[str, Any]]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                object_nodes.append((path, value))
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(schema, "$")
    assert object_nodes
    assert [path for path, node in object_nodes if node.get("additionalProperties") is not False] == []
    assert len(schema["oneOf"]) == 4


def test_cli_reports_conformance_without_claiming_authority(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--kind", "signed-request", str(request_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "conformance only" in result.stdout
    assert "no trust or authority granted" in result.stdout


def test_cli_rejects_duplicate_keys(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(request_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "duplicate object key" in result.stderr


def test_cli_unreadable_document_error_is_deterministic_and_redacted(tmp_path: Path) -> None:
    missing = tmp_path / "private-controller-request.json"
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(missing)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == "INVALID: $: cannot read document\n"
    assert str(missing) not in result.stderr
