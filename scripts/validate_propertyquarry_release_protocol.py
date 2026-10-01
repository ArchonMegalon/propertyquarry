#!/usr/bin/env python3
"""Offline structural validator for PropertyQuarry release-control protocol v1.

This module intentionally performs no signature verification, trust lookup,
freshness decision, CAS operation, policy authorization, or deployment action.
It only rejects malformed or internally inconsistent protocol documents.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


MAX_DOCUMENT_BYTES = 1_048_576
PROTOCOL_VERSION = 1
AUDIENCE = "propertyquarry-release-controller"
MAX_REQUEST_LIFETIME_SECONDS = 900
SIGNATURE_DOMAIN = "propertyquarry.release-control.signature.v1"

SCHEMA_SIGNED_REQUEST = "propertyquarry.release.signed-request"
SCHEMA_PREFLIGHT_DISPOSITION = "propertyquarry.release.preflight-disposition"
SCHEMA_CONTROLLER_RECEIPT = "propertyquarry.release.controller-receipt"
SCHEMA_CONTROLLER_MANIFEST = "propertyquarry.release.controller-manifest"

OPERATIONS = {
    "deploy-run",
    "deploy-preflight",
    "candidate-run",
    "candidate-preflight",
}
PREFLIGHT_OPERATIONS = {"deploy-preflight", "candidate-preflight"}
RUN_OPERATIONS = {"deploy-run", "candidate-run"}
MODE_BY_OPERATION = {
    "deploy-run": "production",
    "deploy-preflight": "production",
    "candidate-run": "candidate",
    "candidate-preflight": "candidate",
}
POLICY_DIGEST_NAMES = {
    "canonical_compose_plan",
    "database_fence_policy",
    "drain_keyring",
    "monitoring_tools",
    "monitoring_topology",
    "operator_gateway_trust",
}

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RELEASE_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UUID_V4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,127})\Z")
_LOWER_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._/-]{0,127})\Z")
_CHECK_ID_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_REASON_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


class ProtocolValidationError(ValueError):
    """A deterministic protocol-conformance failure."""


def _fail(path: str, message: str) -> None:
    raise ProtocolValidationError(f"{path}: {message}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolValidationError(f"$: duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise ProtocolValidationError(f"$: non-standard JSON number {token!r} is forbidden")


def load_document_bytes(raw: bytes) -> Any:
    """Parse a bounded UTF-8 JSON document while rejecting duplicate keys."""

    if not raw:
        _fail("$", "document must not be empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        _fail("$", f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolValidationError("$: document is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except ProtocolValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ProtocolValidationError(
            f"$: invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except RecursionError as exc:
        raise ProtocolValidationError("$: JSON nesting is too deep") from exc
    except ValueError as exc:
        raise ProtocolValidationError("$: JSON number is outside parser bounds") from exc


def load_document(path: Path) -> Any:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise ProtocolValidationError("$: cannot read document") from exc
    return load_document_bytes(raw)


def canonical_json_bytes(value: Any) -> bytes:
    """Return protocol-v1 deterministic JSON bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def signature_preimage(envelope: Mapping[str, Any], body_field: str) -> dict[str, Any]:
    """Build the non-circular, domain-separated Ed25519 preimage for v1."""

    if body_field not in {"payload", "preflight", "receipt"}:
        raise ProtocolValidationError(f"$: unsupported signed body field {body_field!r}")
    signature = envelope["signature"]
    return {
        "domain": SIGNATURE_DOMAIN,
        "schema": envelope["schema"],
        "version": envelope["version"],
        body_field: envelope[body_field],
        "signature_context": {
            "algorithm": signature["algorithm"],
            "key_id": signature["key_id"],
            "encoding": signature["encoding"],
        },
    }


def signed_preimage_sha256(envelope: Mapping[str, Any], body_field: str) -> str:
    return canonical_sha256(signature_preimage(envelope, body_field))


def _object(
    value: Any,
    path: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    required_keys = set(required)
    optional_keys = set(optional)
    actual_keys = set(value)
    missing = sorted(required_keys - actual_keys)
    unknown = sorted(actual_keys - required_keys - optional_keys)
    if missing:
        _fail(path, "missing required properties: " + ", ".join(missing))
    if unknown:
        _fail(path, "unknown properties: " + ", ".join(unknown))
    return value


def _string(
    value: Any,
    path: str,
    *,
    minimum: int = 1,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    if len(value) < minimum or len(value) > maximum:
        _fail(path, f"length must be between {minimum} and {maximum}")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(path, "has an invalid format")
    return value


def _literal(value: Any, path: str, expected: Any) -> None:
    if value != expected or type(value) is not type(expected):
        _fail(path, f"must equal {expected!r}")


def _enum(value: Any, path: str, allowed: Iterable[str]) -> str:
    allowed_values = set(allowed)
    if not isinstance(value, str) or value not in allowed_values:
        _fail(path, "must be one of: " + ", ".join(sorted(allowed_values)))
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 9_223_372_036_854_775_807,
) -> int:
    if type(value) is not int:
        _fail(path, "must be an integer")
    if value < minimum or value > maximum:
        _fail(path, f"must be between {minimum} and {maximum}")
    return value


def _array(
    value: Any,
    path: str,
    *,
    minimum: int,
    maximum: int,
) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    if len(value) < minimum or len(value) > maximum:
        _fail(path, f"must contain between {minimum} and {maximum} items")
    return value


def _digest(value: Any, path: str) -> str:
    return _string(value, path, maximum=71, pattern=_DIGEST_RE)


def _release_sha(value: Any, path: str) -> str:
    return _string(value, path, maximum=64, pattern=_RELEASE_SHA_RE)


def _request_id(value: Any, path: str) -> str:
    return _string(value, path, maximum=36, pattern=_UUID_V4_RE)


def _nonce(value: Any, path: str) -> str:
    return _string(value, path, maximum=32, pattern=_NONCE_RE)


def _host(value: Any, path: str) -> str:
    host = _string(value, path, maximum=253)
    labels = host.split(".")
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        _fail(path, "must be a canonical lowercase DNS host name")
    return host


def _timestamp(value: Any, path: str) -> datetime:
    timestamp = _string(value, path, maximum=20, pattern=_TIMESTAMP_RE)
    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProtocolValidationError(f"{path}: is not a real UTC timestamp") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _validate_policy_digests(value: Any, path: str) -> dict[str, Any]:
    obj = _object(value, path, required=POLICY_DIGEST_NAMES)
    for name in sorted(POLICY_DIGEST_NAMES):
        _digest(obj[name], f"{path}.{name}")
    return obj


def _validate_release_binding(value: Any, path: str) -> dict[str, Any]:
    obj = _object(
        value,
        path,
        required={
            "release_sha",
            "candidate_artifact_digest",
            "web_image_digest",
            "render_image_digest",
            "controller_digest",
            "controller_manifest_digest",
            "policy_digests",
        },
    )
    _release_sha(obj["release_sha"], f"{path}.release_sha")
    _digest(obj["candidate_artifact_digest"], f"{path}.candidate_artifact_digest")
    _digest(obj["web_image_digest"], f"{path}.web_image_digest")
    _digest(obj["render_image_digest"], f"{path}.render_image_digest")
    _digest(obj["controller_digest"], f"{path}.controller_digest")
    _digest(obj["controller_manifest_digest"], f"{path}.controller_manifest_digest")
    _validate_policy_digests(obj["policy_digests"], f"{path}.policy_digests")
    return obj


def _validate_signature(
    envelope: Mapping[str, Any],
    path: str,
    body_field: str,
) -> None:
    value = envelope["signature"]
    obj = _object(
        value,
        path,
        required={
            "algorithm",
            "key_id",
            "encoding",
            "signed_preimage_sha256",
            "value",
        },
    )
    _literal(obj["algorithm"], f"{path}.algorithm", "ed25519")
    _string(obj["key_id"], f"{path}.key_id", maximum=128, pattern=_SAFE_ID_RE)
    _literal(obj["encoding"], f"{path}.encoding", "base64")
    preimage_digest = _digest(
        obj["signed_preimage_sha256"],
        f"{path}.signed_preimage_sha256",
    )
    expected_digest = signed_preimage_sha256(envelope, body_field)
    if preimage_digest != expected_digest:
        _fail(
            f"{path}.signed_preimage_sha256",
            "does not bind the canonical domain-separated signed preimage",
        )
    encoded = _string(obj["value"], f"{path}.value", maximum=4096)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolValidationError(f"{path}.value: must be canonical base64 syntax") from exc
    if len(decoded) != 64:
        _fail(f"{path}.value", "an ed25519 signature must contain exactly 64 bytes")
    if base64.b64encode(decoded).decode("ascii") != encoded:
        _fail(f"{path}.value", "must use canonical padded base64 encoding")


def _validate_cas_request(value: Any, path: str) -> dict[str, Any]:
    obj = _object(value, path, required={"namespace", "challenge", "expected_counter"})
    _string(obj["namespace"], f"{path}.namespace", maximum=128, pattern=_LOWER_ID_RE)
    _digest(obj["challenge"], f"{path}.challenge")
    _integer(obj["expected_counter"], f"{path}.expected_counter")
    return obj


def _validate_requested_effect(value: Any, path: str, operation: str) -> None:
    obj = _object(value, path, required={"mutation"})
    mutation = _enum(
        obj["mutation"],
        f"{path}.mutation",
        {"forbidden", "controller-policy-gated"},
    )
    expected = "forbidden" if operation in PREFLIGHT_OPERATIONS else "controller-policy-gated"
    if mutation != expected:
        _fail(
            f"{path}.mutation",
            f"must be {expected!r} for operation {operation!r}",
        )


def validate_signed_request(document: Any) -> None:
    envelope = _object(
        document,
        "$",
        required={"schema", "version", "payload", "signature"},
    )
    _literal(envelope["schema"], "$.schema", SCHEMA_SIGNED_REQUEST)
    _literal(envelope["version"], "$.version", PROTOCOL_VERSION)
    payload = _object(
        envelope["payload"],
        "$.payload",
        required={
            "operation",
            "mode",
            "audience",
            "host",
            "request_id",
            "nonce",
            "issued_at",
            "expires_at",
            "cas",
            "release",
            "requested_effect",
        },
    )
    operation = _enum(payload["operation"], "$.payload.operation", OPERATIONS)
    mode = _enum(payload["mode"], "$.payload.mode", {"production", "candidate"})
    if MODE_BY_OPERATION[operation] != mode:
        _fail("$.payload.mode", f"does not match operation {operation!r}")
    _literal(payload["audience"], "$.payload.audience", AUDIENCE)
    _host(payload["host"], "$.payload.host")
    _request_id(payload["request_id"], "$.payload.request_id")
    _nonce(payload["nonce"], "$.payload.nonce")
    issued_at = _timestamp(payload["issued_at"], "$.payload.issued_at")
    expires_at = _timestamp(payload["expires_at"], "$.payload.expires_at")
    lifetime = (expires_at - issued_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_REQUEST_LIFETIME_SECONDS:
        _fail(
            "$.payload.expires_at",
            f"must be after issued_at by at most {MAX_REQUEST_LIFETIME_SECONDS} seconds",
        )
    _validate_cas_request(payload["cas"], "$.payload.cas")
    _validate_release_binding(payload["release"], "$.payload.release")
    _validate_requested_effect(payload["requested_effect"], "$.payload.requested_effect", operation)
    _validate_signature(envelope, "$.signature", "payload")


def _validate_request_binding(
    value: Any,
    path: str,
    *,
    allowed_operations: Iterable[str],
) -> tuple[dict[str, Any], str, str]:
    obj = _object(
        value,
        path,
        required={
            "request_transport_sha256",
            "request_id",
            "nonce",
            "operation",
            "mode",
            "audience",
            "host",
            "cas_namespace",
            "cas_challenge",
            "cas_expected_counter",
            "release",
        },
    )
    _digest(obj["request_transport_sha256"], f"{path}.request_transport_sha256")
    _request_id(obj["request_id"], f"{path}.request_id")
    _nonce(obj["nonce"], f"{path}.nonce")
    operation = _enum(obj["operation"], f"{path}.operation", allowed_operations)
    mode = _enum(obj["mode"], f"{path}.mode", {"production", "candidate"})
    if MODE_BY_OPERATION[operation] != mode:
        _fail(f"{path}.mode", f"does not match operation {operation!r}")
    _literal(obj["audience"], f"{path}.audience", AUDIENCE)
    _host(obj["host"], f"{path}.host")
    _string(
        obj["cas_namespace"],
        f"{path}.cas_namespace",
        maximum=128,
        pattern=_LOWER_ID_RE,
    )
    _digest(obj["cas_challenge"], f"{path}.cas_challenge")
    _integer(obj["cas_expected_counter"], f"{path}.cas_expected_counter")
    _validate_release_binding(obj["release"], f"{path}.release")
    return obj, operation, mode


def _validate_controller_binding(value: Any, path: str) -> None:
    obj = _object(
        value,
        path,
        required={"controller_id", "manifest_digest", "binary_digest", "protocol_version"},
    )
    _string(obj["controller_id"], f"{path}.controller_id", maximum=128, pattern=_SAFE_ID_RE)
    _digest(obj["manifest_digest"], f"{path}.manifest_digest")
    _digest(obj["binary_digest"], f"{path}.binary_digest")
    _literal(obj["protocol_version"], f"{path}.protocol_version", PROTOCOL_VERSION)


def _validate_controller_release_cross_binding(
    request: Mapping[str, Any],
    controller: Mapping[str, Any],
    path: str,
) -> None:
    release = request["release"]
    comparisons = (
        ("binary_digest", "controller_digest"),
        ("manifest_digest", "controller_manifest_digest"),
    )
    for controller_field, release_field in comparisons:
        if controller[controller_field] != release[release_field]:
            _fail(
                f"{path}.controller.{controller_field}",
                f"does not match request.release.{release_field}",
            )


def _validate_check(value: Any, path: str) -> None:
    obj = _object(value, path, required={"id", "status", "code"})
    _string(obj["id"], f"{path}.id", maximum=64, pattern=_CHECK_ID_RE)
    _enum(obj["status"], f"{path}.status", {"pass", "fail", "not-run"})
    _string(obj["code"], f"{path}.code", maximum=64, pattern=_REASON_CODE_RE)


def validate_preflight_disposition(document: Any) -> None:
    envelope = _object(
        document,
        "$",
        required={"schema", "version", "preflight", "signature"},
    )
    _literal(envelope["schema"], "$.schema", SCHEMA_PREFLIGHT_DISPOSITION)
    _literal(envelope["version"], "$.version", PROTOCOL_VERSION)
    obj = _object(
        envelope["preflight"],
        "$.preflight",
        required={
            "request",
            "controller",
            "evaluated_at",
            "disposition",
            "mutation_performed",
            "cas_consumed",
            "checks",
        },
    )
    request, _operation, _mode = _validate_request_binding(
        obj["request"],
        "$.preflight.request",
        allowed_operations=PREFLIGHT_OPERATIONS,
    )
    _validate_controller_binding(obj["controller"], "$.preflight.controller")
    _validate_controller_release_cross_binding(request, obj["controller"], "$.preflight")
    _timestamp(obj["evaluated_at"], "$.preflight.evaluated_at")
    _enum(
        obj["disposition"],
        "$.preflight.disposition",
        {"ready", "not-ready", "indeterminate"},
    )
    _literal(obj["mutation_performed"], "$.preflight.mutation_performed", False)
    _literal(obj["cas_consumed"], "$.preflight.cas_consumed", False)
    checks = _array(obj["checks"], "$.preflight.checks", minimum=1, maximum=128)
    seen: set[str] = set()
    for index, check in enumerate(checks):
        _validate_check(check, f"$.preflight.checks[{index}]")
        check_id = check["id"]
        if check_id in seen:
            _fail("$.preflight.checks", f"duplicate check id {check_id!r}")
        seen.add(check_id)
    statuses = {check["status"] for check in checks}
    if obj["disposition"] == "ready" and statuses != {"pass"}:
        _fail("$.preflight.disposition", "ready requires every check to pass")
    if obj["disposition"] == "not-ready" and (
        "fail" not in statuses or "not-run" in statuses
    ):
        _fail(
            "$.preflight.disposition",
            "not-ready requires at least one failed check and no not-run checks",
        )
    if obj["disposition"] == "indeterminate" and "not-run" not in statuses:
        _fail(
            "$.preflight.disposition",
            "indeterminate requires at least one not-run check",
        )
    _validate_signature(envelope, "$.signature", "preflight")


def _validate_mutation(value: Any, path: str, *, mode: str, outcome: str) -> None:
    obj = _object(
        value,
        path,
        required={
            "performed",
            "containment_before_candidate_validation",
            "database_changed",
            "traffic_changed",
            "rollback_performed",
        },
    )
    performed = _boolean(obj["performed"], f"{path}.performed")
    containment = _boolean(
        obj["containment_before_candidate_validation"],
        f"{path}.containment_before_candidate_validation",
    )
    database_changed = _boolean(obj["database_changed"], f"{path}.database_changed")
    traffic_changed = _boolean(obj["traffic_changed"], f"{path}.traffic_changed")
    rollback = _boolean(obj["rollback_performed"], f"{path}.rollback_performed")
    if not performed and any((containment, database_changed, traffic_changed, rollback)):
        _fail(path, "subordinate mutation flags must be false when performed is false")
    if performed and not containment:
        _fail(
            f"{path}.containment_before_candidate_validation",
            "must be true whenever mutation was performed",
        )
    if mode == "candidate" and traffic_changed:
        _fail(f"{path}.traffic_changed", "candidate operations cannot change production traffic")
    if outcome == "rolled-back" and not rollback:
        _fail(f"{path}.rollback_performed", "must be true for a rolled-back outcome")
    if outcome != "rolled-back" and rollback:
        _fail(
            f"{path}.rollback_performed",
            "may be true only for a rolled-back outcome",
        )


def _validate_cas_commit(
    value: Any,
    path: str,
    *,
    request_namespace: str,
    request_challenge: str,
    request_expected_counter: int,
) -> None:
    obj = _object(
        value,
        path,
        required={
            "committed",
            "namespace",
            "challenge",
            "previous_counter",
            "committed_counter",
            "seal_digest",
        },
    )
    committed = _boolean(obj["committed"], f"{path}.committed")
    namespace = _string(
        obj["namespace"],
        f"{path}.namespace",
        maximum=128,
        pattern=_LOWER_ID_RE,
    )
    if namespace != request_namespace:
        _fail(f"{path}.namespace", "does not match the signed-request binding")
    challenge = _digest(obj["challenge"], f"{path}.challenge")
    if challenge != request_challenge:
        _fail(f"{path}.challenge", "does not match the signed-request binding")
    previous = _integer(obj["previous_counter"], f"{path}.previous_counter")
    if previous != request_expected_counter:
        _fail(f"{path}.previous_counter", "does not match the signed-request binding")
    committed_counter = _integer(obj["committed_counter"], f"{path}.committed_counter")
    _digest(obj["seal_digest"], f"{path}.seal_digest")
    expected = previous + 1 if committed else previous
    if committed_counter != expected:
        _fail(
            f"{path}.committed_counter",
            "must advance exactly once when committed and remain unchanged otherwise",
        )


def _validate_evidence(value: Any, path: str) -> None:
    obj = _object(value, path, required={"kind", "digest"})
    _string(obj["kind"], f"{path}.kind", maximum=64, pattern=_CHECK_ID_RE)
    _digest(obj["digest"], f"{path}.digest")


def validate_controller_receipt(document: Any) -> None:
    envelope = _object(
        document,
        "$",
        required={"schema", "version", "receipt", "signature"},
    )
    _literal(envelope["schema"], "$.schema", SCHEMA_CONTROLLER_RECEIPT)
    _literal(envelope["version"], "$.version", PROTOCOL_VERSION)
    receipt = _object(
        envelope["receipt"],
        "$.receipt",
        required={
            "receipt_id",
            "request",
            "controller",
            "started_at",
            "finished_at",
            "outcome",
            "mutation",
            "cas_commit",
            "evidence",
        },
    )
    _request_id(receipt["receipt_id"], "$.receipt.receipt_id")
    request, _operation, mode = _validate_request_binding(
        receipt["request"],
        "$.receipt.request",
        allowed_operations=RUN_OPERATIONS,
    )
    _validate_controller_binding(receipt["controller"], "$.receipt.controller")
    _validate_controller_release_cross_binding(
        request,
        receipt["controller"],
        "$.receipt",
    )
    started = _timestamp(receipt["started_at"], "$.receipt.started_at")
    finished = _timestamp(receipt["finished_at"], "$.receipt.finished_at")
    if finished < started:
        _fail("$.receipt.finished_at", "must not precede started_at")
    outcome = _enum(
        receipt["outcome"],
        "$.receipt.outcome",
        {"succeeded", "rejected", "failed", "rolled-back"},
    )
    _validate_mutation(receipt["mutation"], "$.receipt.mutation", mode=mode, outcome=outcome)
    _validate_cas_commit(
        receipt["cas_commit"],
        "$.receipt.cas_commit",
        request_namespace=request["cas_namespace"],
        request_challenge=request["cas_challenge"],
        request_expected_counter=request["cas_expected_counter"],
    )
    evidence = _array(receipt["evidence"], "$.receipt.evidence", minimum=0, maximum=128)
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(evidence):
        _validate_evidence(item, f"$.receipt.evidence[{index}]")
        identity = (item["kind"], item["digest"])
        if identity in seen:
            _fail("$.receipt.evidence", "duplicate evidence binding")
        seen.add(identity)
    mutation = receipt["mutation"]
    cas_commit = receipt["cas_commit"]
    if outcome == "succeeded":
        if not mutation["performed"] or not mutation["containment_before_candidate_validation"]:
            _fail(
                "$.receipt.outcome",
                "succeeded requires mutation after candidate containment",
            )
        if not cas_commit["committed"]:
            _fail("$.receipt.outcome", "succeeded requires a committed external CAS seal")
        if request["operation"] == "deploy-run" and not mutation["traffic_changed"]:
            _fail("$.receipt.outcome", "successful deploy-run requires a production traffic change")
        if not evidence:
            _fail("$.receipt.evidence", "succeeded requires at least one evidence binding")
    elif outcome == "rejected":
        if mutation["performed"]:
            _fail("$.receipt.outcome", "rejected requires no mutation")
        if cas_commit["committed"]:
            _fail("$.receipt.outcome", "rejected requires no CAS commit")
    else:
        if not cas_commit["committed"]:
            _fail(
                "$.receipt.outcome",
                f"{outcome} requires a committed external CAS seal",
            )
    _validate_signature(envelope, "$.signature", "receipt")


def validate_controller_manifest(document: Any) -> None:
    obj = _object(
        document,
        "$",
        required={
            "schema",
            "version",
            "controller_id",
            "protocol_version",
            "audience",
            "host",
            "binary_digest",
            "build_release_sha",
            "issued_at",
            "supported_operations",
            "supported_signature_algorithms",
            "request_max_bytes",
            "request_ttl_max_seconds",
            "canonicalization",
            "policy_digests",
        },
    )
    _literal(obj["schema"], "$.schema", SCHEMA_CONTROLLER_MANIFEST)
    _literal(obj["version"], "$.version", PROTOCOL_VERSION)
    _string(obj["controller_id"], "$.controller_id", maximum=128, pattern=_SAFE_ID_RE)
    _literal(obj["protocol_version"], "$.protocol_version", PROTOCOL_VERSION)
    _literal(obj["audience"], "$.audience", AUDIENCE)
    _host(obj["host"], "$.host")
    _digest(obj["binary_digest"], "$.binary_digest")
    _release_sha(obj["build_release_sha"], "$.build_release_sha")
    _timestamp(obj["issued_at"], "$.issued_at")
    operations = _array(
        obj["supported_operations"],
        "$.supported_operations",
        minimum=4,
        maximum=4,
    )
    if any(type(item) is not str for item in operations) or set(operations) != OPERATIONS:
        _fail("$.supported_operations", "must contain each protocol-v1 operation exactly once")
    _literal(
        obj["supported_signature_algorithms"],
        "$.supported_signature_algorithms",
        ["ed25519"],
    )
    _literal(obj["request_max_bytes"], "$.request_max_bytes", MAX_DOCUMENT_BYTES)
    _literal(
        obj["request_ttl_max_seconds"],
        "$.request_ttl_max_seconds",
        MAX_REQUEST_LIFETIME_SECONDS,
    )
    _literal(
        obj["canonicalization"],
        "$.canonicalization",
        "propertyquarry-json-sort-keys-v1",
    )
    _validate_policy_digests(obj["policy_digests"], "$.policy_digests")


VALIDATORS: Mapping[str, Callable[[Any], None]] = {
    "signed-request": validate_signed_request,
    "preflight-disposition": validate_preflight_disposition,
    "controller-receipt": validate_controller_receipt,
    "controller-manifest": validate_controller_manifest,
}
VALIDATOR_BY_SCHEMA: Mapping[str, Callable[[Any], None]] = {
    SCHEMA_SIGNED_REQUEST: validate_signed_request,
    SCHEMA_PREFLIGHT_DISPOSITION: validate_preflight_disposition,
    SCHEMA_CONTROLLER_RECEIPT: validate_controller_receipt,
    SCHEMA_CONTROLLER_MANIFEST: validate_controller_manifest,
}


def validate_document(document: Any, kind: str = "auto") -> str:
    if kind == "auto":
        if not isinstance(document, dict):
            _fail("$", "must be an object")
        schema = document.get("schema")
        validator = VALIDATOR_BY_SCHEMA.get(schema)
        if validator is None:
            _fail("$.schema", "is not a recognized PropertyQuarry release protocol schema")
        validator(document)
        return schema
    validator = VALIDATORS[kind]
    validator(document)
    return kind


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate PropertyQuarry release-control protocol shape and internal bindings. "
            "This does not authenticate, authorize, or execute a release."
        )
    )
    parser.add_argument(
        "--kind",
        choices=("auto", *VALIDATORS.keys()),
        default="auto",
        help="expected document kind (default: infer from schema)",
    )
    parser.add_argument("document", type=Path, help="UTF-8 JSON document to validate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = load_document(args.document)
        validated_as = validate_document(document, args.kind)
    except ProtocolValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"VALID: {validated_as}; conformance only (no trust or authority granted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
