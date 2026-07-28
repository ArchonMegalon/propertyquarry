#!/usr/bin/env python3
"""Canonical PropertyQuarry observability receipt validation.

The verifier consumes raw, release-bound receipts.  It never trusts a
producer's boolean status or stored hash without recomputing it.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import math
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from scripts import propertyquarry_evidence_contract as evidence_contract
    from scripts import propertyquarry_secure_file_io as secure_file_io
else:
    import propertyquarry_evidence_contract as evidence_contract
    import propertyquarry_secure_file_io as secure_file_io


MONITORING_SCHEMA = "propertyquarry.monitoring-runtime-proof.v2"
MONITORING_PRODUCER = "propertyquarry-monitoring-runtime-proof"
RANGE_SCHEMA = evidence_contract.RANGE_RECEIPT_SCHEMA
RANGE_PRODUCER = evidence_contract.RANGE_RECEIPT_PRODUCER
ALERT_SCHEMA = evidence_contract.OPERATOR_GATEWAY_ACK_SCHEMA
ALERT_PRODUCER = evidence_contract.OPERATOR_GATEWAY_ACK_PRODUCER
VERIFICATION_SCHEMA = "propertyquarry.observability-receipt-verification.v2"
VERIFICATION_PRODUCER = "propertyquarry-observability-receipt-verifier"
OPERATIONS_VERIFICATION_SCHEMA = (
    "propertyquarry.flagship-operations-evidence-verification.v1"
)
OPERATIONS_VERIFICATION_PRODUCER = (
    "propertyquarry-flagship-operations-evidence-verifier"
)
OPERATIONS_SHARED_INPUT_NAMES = (
    "dashboard_render_receipt",
    "structured_log_query_receipt",
    "distributed_trace_query_receipt",
)
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RANGE_RESPONSE_BYTES = 128 * 1024 * 1024
MIN_RANGE_SECONDS = evidence_contract.RANGE_WINDOW_SECONDS


class ReceiptValidationError(RuntimeError):
    """A receipt is malformed, tampered, stale, or bound to another release."""


def verify_operations_evidence(
    *,
    release_commit_sha: str,
    release_image_digest: str,
    dashboard_render_receipt_path: Path,
    structured_log_query_receipt_path: Path,
    distributed_trace_query_receipt_path: Path,
    now: datetime | None = None,
    expected_input_hashes: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Invoke the standalone verifier without creating an import cycle."""

    if __package__:
        from scripts import propertyquarry_flagship_operations_evidence as verifier
    else:
        import propertyquarry_flagship_operations_evidence as verifier

    try:
        return verifier.verify_operations_evidence(
            release_commit_sha=release_commit_sha,
            release_image_digest=release_image_digest,
            dashboard_render_receipt_path=dashboard_render_receipt_path,
            structured_log_query_receipt_path=structured_log_query_receipt_path,
            distributed_trace_query_receipt_path=distributed_trace_query_receipt_path,
            now=now,
            expected_input_hashes=expected_input_hashes,
        )
    except verifier.OperationsEvidenceValidationError as exc:
        raise ReceiptValidationError(str(exc)) from exc


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError("receipt contains a non-canonical JSON value") from exc
    return rendered.encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def compute_payload_sha256(payload: Mapping[str, object]) -> str:
    unhashed = copy.deepcopy(dict(payload))
    unhashed.pop("payload_sha256", None)
    authentication = unhashed.get("authentication")
    if isinstance(authentication, dict):
        authentication.pop("signature", None)
    return sha256_bytes(canonical_json_bytes(unhashed))


def add_payload_sha256(payload: Mapping[str, object]) -> dict[str, object]:
    result = copy.deepcopy(dict(payload))
    result["payload_sha256"] = compute_payload_sha256(result)
    return result


def authenticate_payload(
    payload: Mapping[str, object],
    *,
    domain: str,
    anchor: evidence_contract.TrustAnchor,
    challenge: evidence_contract.EvidenceChallenge,
    signature_provider: evidence_contract.SignatureProvider = evidence_contract.request_release_control_signature,
) -> dict[str, object]:
    """Ask the external authority to authenticate a complete evidence payload."""

    result = copy.deepcopy(dict(payload))
    result["deployment_id"] = challenge.deployment_id
    result["challenge_nonce"] = challenge.nonce
    result["authentication"] = {
        "scheme": evidence_contract.AUTH_SCHEME,
        "key_id": anchor.key_id,
        "challenge_sha256": challenge.artifact_sha256,
    }
    result = add_payload_sha256(result)
    signature = signature_provider(domain, result)
    authentication = dict(result["authentication"])
    authentication["signature"] = signature
    result["authentication"] = authentication
    evidence_contract.verify_authenticated_payload(
        result,
        domain=domain,
        anchor=anchor,
        challenge=challenge,
        field="authenticated evidence",
    )
    return result


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_constant(raw: str) -> object:
    raise ReceiptValidationError(f"non-finite JSON constant is forbidden: {raw}")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptValidationError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def load_json_receipt(path: Path, *, name: str) -> tuple[dict[str, object], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReceiptValidationError(f"{name} receipt is unreadable: {path}") from exc
    if not payload or len(payload) > MAX_RECEIPT_BYTES:
        raise ReceiptValidationError(f"{name} receipt size is invalid")
    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptValidationError(f"{name} receipt is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ReceiptValidationError(f"{name} receipt must be a JSON object")
    return parsed, payload


def load_prometheus_range_response(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReceiptValidationError(f"Prometheus range response is unreadable: {path}") from exc
    if not payload or len(payload) > MAX_RANGE_RESPONSE_BYTES:
        raise ReceiptValidationError("Prometheus range response size is invalid")
    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptValidationError("Prometheus range response is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ReceiptValidationError("Prometheus range response must be a JSON object")
    return parsed, payload


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ReceiptValidationError(f"{field} must be an object")
    return value


def _list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ReceiptValidationError(f"{field} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ReceiptValidationError(
            f"{field} keys do not match v1 contract; missing={missing}, unexpected={unexpected}"
        )


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReceiptValidationError(f"{field} must be a non-empty trimmed string")
    return value


def _sha(value: object, *, field: str) -> str:
    text = _text(value, field=field)
    if not SHA256_RE.fullmatch(text):
        raise ReceiptValidationError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _timestamp(value: object, *, field: str) -> datetime:
    text = _text(value, field=field)
    if not text.endswith("Z"):
        raise ReceiptValidationError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ReceiptValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptValidationError(f"{field} must include UTC timezone information")
    return parsed.astimezone(timezone.utc)


def _release(
    value: object,
    *,
    field: str,
    expected_commit_sha: str,
    expected_image_digest: str,
) -> Mapping[str, object]:
    release = _mapping(value, field=field)
    _exact_keys(release, {"commit_sha", "image_digest"}, field=field)
    commit_sha = _text(release["commit_sha"], field=f"{field}.commit_sha")
    image_digest = _text(release["image_digest"], field=f"{field}.image_digest")
    if not GIT_SHA_RE.fullmatch(commit_sha):
        raise ReceiptValidationError(f"{field}.commit_sha is not a full lowercase Git SHA")
    if not IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise ReceiptValidationError(f"{field}.image_digest is not an immutable lowercase digest")
    if commit_sha != expected_commit_sha or image_digest != expected_image_digest:
        raise ReceiptValidationError(f"{field} belongs to a different release")
    return release


def _verify_stored_payload_hash(payload: Mapping[str, object], *, field: str) -> str:
    stored = _sha(payload.get("payload_sha256"), field=f"{field}.payload_sha256")
    actual = compute_payload_sha256(payload)
    if stored != actual:
        raise ReceiptValidationError(f"{field}.payload_sha256 does not match canonical content")
    return actual


def _replica_ids(value: object, *, field: str) -> list[str]:
    raw = _list(value, field=field)
    if not raw:
        raise ReceiptValidationError(f"{field} must not be empty")
    result: list[str] = []
    for index, item in enumerate(raw):
        replica_id = _text(item, field=f"{field}[{index}]")
        if not REPLICA_ID_RE.fullmatch(replica_id) or replica_id == "UNCONFIGURED":
            raise ReceiptValidationError(f"{field}[{index}] is not a configured replica ID")
        result.append(replica_id)
    if result != sorted(set(result)):
        raise ReceiptValidationError(f"{field} must be sorted and unique")
    return result


def _private_instance(value: object, *, field: str) -> str:
    instance = _text(value, field=field)
    try:
        parsed = urllib.parse.urlsplit(f"//{instance}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ReceiptValidationError(f"{field} must be a private IP literal with port") from exc
    if (
        not hostname
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ReceiptValidationError(f"{field} must be a private IP literal with port")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ReceiptValidationError(f"{field} must use a direct IP literal") from exc
    if not (address.is_loopback or address.is_private):
        raise ReceiptValidationError(f"{field} must use a private IP literal")
    return instance


def validate_alert_delivery_receipt(
    payload: Mapping[str, object],
    *,
    expected_commit_sha: str,
    expected_image_digest: str,
    operator_gateway_trust: evidence_contract.OperatorGatewayTrust,
    challenge: evidence_contract.EvidenceChallenge,
    now: datetime,
) -> dict[str, object]:
    _exact_keys(
        payload,
        {
            "schema_version",
            "producer",
            "audience",
            "gateway",
            "deployment_id",
            "challenge_nonce",
            "challenge_sha256",
            "release",
            "nonce",
            "alert",
            "delivery",
            "payload_sha256",
            "authentication",
        },
        field="alert_delivery",
    )
    if payload["schema_version"] != ALERT_SCHEMA or payload["producer"] != ALERT_PRODUCER:
        raise ReceiptValidationError("operator gateway acknowledgement schema is not canonical")
    if payload["audience"] != operator_gateway_trust.audience:
        raise ReceiptValidationError("operator gateway acknowledgement audience is not pinned")
    gateway = _mapping(payload["gateway"], field="operator gateway acknowledgement.gateway")
    _exact_keys(
        gateway,
        {"endpoint_origin", "tls_spki_sha256"},
        field="operator gateway acknowledgement.gateway",
    )
    try:
        gateway_origin, _socket_identity = evidence_contract.canonical_endpoint_origin(
            gateway["endpoint_origin"],
            field="operator gateway acknowledgement endpoint",
            require_https=True,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    if (
        gateway_origin != operator_gateway_trust.endpoint_origin
        or gateway["tls_spki_sha256"] != operator_gateway_trust.tls_spki_sha256
    ):
        raise ReceiptValidationError("operator gateway endpoint or TLS identity is not pinned")
    if (
        payload["deployment_id"] != challenge.deployment_id
        or payload["challenge_nonce"] != challenge.nonce
        or payload["challenge_sha256"] != challenge.artifact_sha256
    ):
        raise ReceiptValidationError("operator gateway acknowledgement challenge binding differs")
    _release(
        payload["release"],
        field="alert_delivery.release",
        expected_commit_sha=expected_commit_sha,
        expected_image_digest=expected_image_digest,
    )
    nonce = _text(payload["nonce"], field="alert_delivery.nonce")
    if not NONCE_RE.fullmatch(nonce) or nonce != challenge.nonce:
        raise ReceiptValidationError(
            "alert_delivery.nonce must exactly match the active challenge nonce"
        )
    alert = _mapping(payload["alert"], field="operator gateway acknowledgement.alert")
    _exact_keys(
        alert,
        {"alertname", "labels", "fingerprint_sha256", "labels_sha256", "sent_at"},
        field="operator gateway acknowledgement.alert",
    )
    if alert["alertname"] != "PropertyQuarryReleaseProof":
        raise ReceiptValidationError("operator gateway acknowledgement is for another alert")
    alert_fingerprint = _sha(
        alert["fingerprint_sha256"],
        field="operator gateway acknowledgement alert fingerprint",
    )
    labels_sha256 = _sha(
        alert["labels_sha256"],
        field="operator gateway acknowledgement labels hash",
    )
    labels = _mapping(
        alert["labels"], field="operator gateway acknowledgement alert labels"
    )
    expected_labels = evidence_contract.canonical_release_proof_labels(
        release_commit_sha=expected_commit_sha,
        release_image_digest=expected_image_digest,
        deployment_id=challenge.deployment_id,
        nonce=challenge.nonce,
        challenge_sha256=challenge.artifact_sha256,
    )
    if labels != expected_labels or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in labels.items()
    ):
        raise ReceiptValidationError(
            "operator gateway acknowledgement labels are not the canonical proof route"
        )
    delivery = _mapping(payload["delivery"], field="operator gateway acknowledgement.delivery")
    _exact_keys(
        delivery,
        {"channel", "status", "delivered_at", "delivery_id_sha256"},
        field="operator gateway acknowledgement.delivery",
    )
    if delivery["channel"] != "telegram" or delivery["status"] != "acknowledged":
        raise ReceiptValidationError("operator gateway did not acknowledge final Telegram delivery")
    try:
        sent_at = evidence_contract.validate_evidence_time(
            alert["sent_at"],
            field="operator gateway acknowledgement alert sent_at",
            now=now,
            challenge=challenge,
        )
        delivered_at = evidence_contract.validate_evidence_time(
            delivery["delivered_at"],
            field="operator gateway acknowledgement delivered_at",
            now=now,
            challenge=challenge,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    if delivered_at < sent_at:
        raise ReceiptValidationError("operator gateway acknowledgement predates alert injection")
    if (delivered_at - sent_at).total_seconds() > 120:
        raise ReceiptValidationError("operator delivery acknowledgement exceeded the proof window")
    if labels_sha256 != sha256_bytes(canonical_json_bytes(expected_labels)):
        raise ReceiptValidationError(
            "operator gateway acknowledgement label hash does not match canonical labels"
        )
    if alert_fingerprint != evidence_contract.alert_fingerprint_sha256(
        labels=expected_labels,
        sent_at=sent_at,
    ):
        raise ReceiptValidationError(
            "operator gateway acknowledgement fingerprint does not match canonical alert"
        )
    delivery_id = _sha(
        delivery["delivery_id_sha256"],
        field="operator gateway acknowledgement delivery ID",
    )
    expected_delivery_id = sha256_bytes(
        canonical_json_bytes(
            {
                "key_id": operator_gateway_trust.key_id,
                "audience": operator_gateway_trust.audience,
                "deployment_id": challenge.deployment_id,
                "nonce": challenge.nonce,
                "alert_fingerprint_sha256": alert_fingerprint,
                "delivered_at": isoformat(delivered_at),
            }
        )
    )
    if delivery_id != expected_delivery_id:
        raise ReceiptValidationError("operator gateway delivery ID is not replay-bound")
    payload_hash = _verify_stored_payload_hash(payload, field="alert_delivery")
    try:
        evidence_contract.verify_operator_gateway_signature(
            payload,
            trust=operator_gateway_trust,
            field="operator gateway acknowledgement",
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    return {
        "nonce": nonce,
        "sent_at": sent_at,
        "received_at": delivered_at,
        "alert_fingerprint_sha256": alert_fingerprint,
        "labels_sha256": labels_sha256,
        "delivery_id_sha256": delivery_id,
        "payload_sha256": payload_hash,
    }


def validate_monitoring_runtime_receipt(
    payload: Mapping[str, object],
    *,
    expected_commit_sha: str,
    expected_image_digest: str,
    expected_snapshot_bundle_sha256: str,
    expected_replica_bindings: Mapping[str, Mapping[str, str]],
    anchor: evidence_contract.TrustAnchor,
    operator_gateway_trust: evidence_contract.OperatorGatewayTrust,
    canonical_monitoring_identity: Mapping[str, object],
    challenge: evidence_contract.EvidenceChallenge,
    now: datetime,
) -> dict[str, object]:
    _exact_keys(
        payload,
        {
            "schema_version",
            "producer",
            "deployment_id",
            "challenge_nonce",
            "captured_at",
            "release",
            "snapshot_bundle_sha256",
            "identity",
            "prometheus",
            "alertmanager",
            "alert_delivery_receipt_sha256",
            "started_at",
            "completed_at",
            "payload_sha256",
            "authentication",
        },
        field="monitoring_runtime",
    )
    if payload["schema_version"] != MONITORING_SCHEMA or payload["producer"] != MONITORING_PRODUCER:
        raise ReceiptValidationError("monitoring-runtime schema or producer is not canonical v2")
    _release(
        payload["release"],
        field="monitoring_runtime.release",
        expected_commit_sha=expected_commit_sha,
        expected_image_digest=expected_image_digest,
    )
    try:
        started_at = evidence_contract.validate_evidence_time(
            payload["started_at"], field="monitoring_runtime.started_at", now=now, challenge=challenge
        )
        completed_at = evidence_contract.validate_evidence_time(
            payload["completed_at"], field="monitoring_runtime.completed_at", now=now, challenge=challenge
        )
        captured_at = evidence_contract.validate_evidence_time(
            payload["captured_at"], field="monitoring_runtime.captured_at", now=now, challenge=challenge
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    if not started_at <= captured_at <= completed_at:
        raise ReceiptValidationError("monitoring-runtime timestamps are not ordered")
    if _sha(
        payload["snapshot_bundle_sha256"], field="monitoring_runtime.snapshot_bundle_sha256"
    ) != expected_snapshot_bundle_sha256:
        raise ReceiptValidationError("monitoring-runtime proof is not bound to the fresh snapshot bundle")

    identity = _mapping(payload["identity"], field="monitoring_runtime.identity")
    canonical_identity = _mapping(
        canonical_monitoring_identity.get("identity"),
        field="canonical monitoring identity",
    )
    canonical_tools = _mapping(
        canonical_monitoring_identity.get("monitoring_tools"),
        field="canonical monitoring tools",
    )
    identity_fields = set(canonical_identity) | {
        "operator_gateway_trust_sha256",
        "operator_gateway_key_id_sha256",
        "operator_gateway_audience_sha256",
    }
    _exact_keys(identity, identity_fields, field="monitoring_runtime.identity")
    for name in identity_fields:
        _sha(identity[name], field=f"monitoring_runtime.identity.{name}")
    if any(identity[name] != expected for name, expected in canonical_identity.items()):
        raise ReceiptValidationError(
            "monitoring-runtime canonical topology, policy, or tool identity differs"
        )
    if (
        identity["operator_gateway_trust_sha256"] != operator_gateway_trust.file_sha256
        or identity["operator_gateway_key_id_sha256"]
        != sha256_bytes(operator_gateway_trust.key_id.encode("utf-8"))
        or identity["operator_gateway_audience_sha256"]
        != sha256_bytes(operator_gateway_trust.audience.encode("utf-8"))
    ):
        raise ReceiptValidationError(
            "monitoring-runtime operator gateway trust identity differs"
        )
    for name in evidence_contract.CANONICAL_POLICY_PATHS:
        if identity[name] != challenge.policy_hashes[name]:
            raise ReceiptValidationError(
                f"monitoring-runtime policy hash differs from challenge: {name}"
            )

    prometheus = _mapping(payload["prometheus"], field="monitoring_runtime.prometheus")
    _exact_keys(
        prometheus,
        {"loaded_config_sha256", "rules_sha256", "expected_replica_ids", "targets"},
        field="monitoring_runtime.prometheus",
    )
    _sha(prometheus["loaded_config_sha256"], field="monitoring_runtime.prometheus.loaded_config_sha256")
    _sha(prometheus["rules_sha256"], field="monitoring_runtime.prometheus.rules_sha256")
    if (
        prometheus["loaded_config_sha256"]
        != canonical_identity["prometheus_config_sha256"]
        or prometheus["rules_sha256"] != canonical_identity["alert_rules_sha256"]
    ):
        raise ReceiptValidationError(
            "monitoring-runtime loaded Prometheus config or rules identity differs"
        )
    expected_ids = _replica_ids(
        prometheus["expected_replica_ids"], field="monitoring_runtime.prometheus.expected_replica_ids"
    )
    targets = _list(prometheus["targets"], field="monitoring_runtime.prometheus.targets")
    if len(targets) != len(expected_ids):
        raise ReceiptValidationError("monitoring-runtime target count does not match expected replicas")
    seen: list[str] = []
    for index, raw_target in enumerate(targets):
        target = _mapping(raw_target, field=f"monitoring_runtime.prometheus.targets[{index}]")
        _exact_keys(
            target,
            {
                "replica_id",
                "container_id",
                "container_image_id",
                "release_commit_sha",
                "release_image_digest",
                "start_snapshot_sha256",
                "end_snapshot_sha256",
                "instance",
                "health",
                "last_scrape_at",
                "scrape_url_sha256",
            },
            field=f"monitoring_runtime.prometheus.targets[{index}]",
        )
        replica_id = _text(target["replica_id"], field=f"monitoring_runtime.prometheus.targets[{index}].replica_id")
        expected_binding = expected_replica_bindings.get(replica_id)
        if expected_binding is None or any(
            target.get(name) != expected_binding[name]
            for name in (
                "container_id",
                "container_image_id",
                "release_commit_sha",
                "release_image_digest",
                "start_snapshot_sha256",
                "end_snapshot_sha256",
            )
        ):
            raise ReceiptValidationError(
                f"monitoring target {replica_id} differs from the fresh container/image/snapshot binding"
            )
        _private_instance(
            target["instance"],
            field=f"monitoring_runtime.prometheus.targets[{index}].instance",
        )
        if target["health"] != "up":
            raise ReceiptValidationError(f"monitoring target {replica_id} is not healthy")
        last_scrape = _timestamp(
            target["last_scrape_at"],
            field=f"monitoring_runtime.prometheus.targets[{index}].last_scrape_at",
        )
        if last_scrape > completed_at or (completed_at - last_scrape).total_seconds() > 120:
            raise ReceiptValidationError(f"monitoring target {replica_id} scrape is not fresh")
        _sha(
            target["scrape_url_sha256"],
            field=f"monitoring_runtime.prometheus.targets[{index}].scrape_url_sha256",
        )
        seen.append(replica_id)
    if seen != expected_ids:
        raise ReceiptValidationError("monitoring-runtime targets must be sorted and exact for expected replicas")

    alertmanager = _mapping(payload["alertmanager"], field="monitoring_runtime.alertmanager")
    _exact_keys(
        alertmanager,
        {"loaded_config_sha256", "status", "proof_secret_configured"},
        field="monitoring_runtime.alertmanager",
    )
    _sha(alertmanager["loaded_config_sha256"], field="monitoring_runtime.alertmanager.loaded_config_sha256")
    if (
        alertmanager["loaded_config_sha256"]
        != canonical_identity["alertmanager_config_sha256"]
    ):
        raise ReceiptValidationError(
            "monitoring-runtime loaded Alertmanager config identity differs"
        )
    if alertmanager["status"] != "ready" or alertmanager["proof_secret_configured"] is not True:
        raise ReceiptValidationError("Alertmanager was not ready with its proof secret configured")
    alert_receipt_hash = _sha(
        payload["alert_delivery_receipt_sha256"],
        field="monitoring_runtime.alert_delivery_receipt_sha256",
    )
    payload_hash = _verify_stored_payload_hash(payload, field="monitoring_runtime")
    try:
        evidence_contract.verify_authenticated_payload(
            payload,
            domain=evidence_contract.MONITORING_DOMAIN,
            anchor=anchor,
            challenge=challenge,
            field="monitoring_runtime",
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    return {
        "expected_replica_ids": expected_ids,
        "started_at": started_at,
        "completed_at": completed_at,
        "identity": dict(identity),
        "monitoring_tools": dict(canonical_tools),
        "alert_delivery_receipt_sha256": alert_receipt_hash,
        "payload_sha256": payload_hash,
    }


def validate_prometheus_range_receipt(
    payload: Mapping[str, object],
    *,
    expected_commit_sha: str,
    expected_image_digest: str,
    expected_snapshot_bundle_sha256: str,
    expected_replica_bindings: Mapping[str, Mapping[str, str]],
    anchor: evidence_contract.TrustAnchor,
    challenge: evidence_contract.EvidenceChallenge,
    now: datetime,
) -> dict[str, object]:
    _exact_keys(
        payload,
        set(evidence_contract.RANGE_RECEIPT_KEYS),
        field="prometheus_range",
    )
    if payload["schema"] != RANGE_SCHEMA or payload["producer"] != RANGE_PRODUCER:
        raise ReceiptValidationError("Prometheus-range schema or producer is not canonical v2")
    _release(
        payload["release"],
        field="prometheus_range.release",
        expected_commit_sha=expected_commit_sha,
        expected_image_digest=expected_image_digest,
    )
    try:
        evidence_contract.validate_evidence_time(
            payload["captured_at"], field="prometheus_range.captured_at", now=now, challenge=challenge
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    if _sha(
        payload["snapshot_bundle_sha256"], field="prometheus_range.snapshot_bundle_sha256"
    ) != expected_snapshot_bundle_sha256:
        raise ReceiptValidationError("Prometheus range receipt is not bound to the fresh snapshot bundle")
    config_hash = _sha(payload["prometheus_config_sha256"], field="prometheus_range.prometheus_config_sha256")
    if config_hash != challenge.policy_hashes["prometheus_config_sha256"]:
        raise ReceiptValidationError(
            "Prometheus range config hash differs from the signed challenge policy"
        )
    expected_ids = _replica_ids(payload["expected_replica_ids"], field="prometheus_range.expected_replica_ids")

    try:
        start, end, step_seconds = evidence_contract.validate_range_query_contract(
            payload["query"], expected_expression=evidence_contract.PROMETHEUS_RANGE_QUERY
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    if end > now.astimezone(timezone.utc) + timedelta(seconds=evidence_contract.MAX_FUTURE_SKEW_SECONDS):
        raise ReceiptValidationError("Prometheus range query end is future-dated")
    if (now.astimezone(timezone.utc) - end).total_seconds() > evidence_contract.MAX_EVIDENCE_AGE_SECONDS:
        raise ReceiptValidationError("Prometheus range query end is stale")

    transport = _mapping(payload["transport"], field="prometheus_range.transport")
    _exact_keys(
        transport,
        {"endpoint_path", "authenticated", "credential_persisted", "http_status", "tls_verified", "connected_peer_ip"},
        field="prometheus_range.transport",
    )
    if (
        transport["endpoint_path"] != "/api/v1/query_range"
        or transport["authenticated"] is not True
        or transport["credential_persisted"] is not False
        or transport["http_status"] != 200
        or transport["tls_verified"] is not True
    ):
        raise ReceiptValidationError("Prometheus range transport proof is not launch-safe")
    try:
        peer = ipaddress.ip_address(_text(transport["connected_peer_ip"], field="prometheus_range.transport.connected_peer_ip"))
    except ValueError as exc:
        raise ReceiptValidationError("Prometheus range peer must be an IP literal") from exc
    if not (peer.is_loopback or peer.is_private):
        raise ReceiptValidationError("Prometheus range peer must be loopback or private")

    replicas = _list(payload["replicas"], field="prometheus_range.replicas")
    if len(replicas) != len(expected_ids):
        raise ReceiptValidationError("Prometheus range replica count does not match expected replicas")
    replica_ids: list[str] = []
    replica_container_ids: dict[str, str] = {}
    for index, raw_replica in enumerate(replicas):
        replica = _mapping(raw_replica, field=f"prometheus_range.replicas[{index}]")
        _exact_keys(
            replica,
            {
                "replica_id",
                "container_id",
                "container_image_id",
                "release_commit_sha",
                "release_image_digest",
                "start_snapshot_sha256",
                "end_snapshot_sha256",
            },
            field=f"prometheus_range.replicas[{index}]",
        )
        replica_id = _text(replica["replica_id"], field=f"prometheus_range.replicas[{index}].replica_id")
        container_id = _text(replica["container_id"], field=f"prometheus_range.replicas[{index}].container_id")
        container_image_id = _text(
            replica["container_image_id"],
            field=f"prometheus_range.replicas[{index}].container_image_id",
        )
        if not IMAGE_DIGEST_RE.fullmatch(container_image_id):
            raise ReceiptValidationError(
                f"prometheus_range.replicas[{index}].container_image_id must be an immutable digest"
            )
        if replica["release_commit_sha"] != expected_commit_sha or replica["release_image_digest"] != expected_image_digest:
            raise ReceiptValidationError(f"Prometheus range replica {replica_id} belongs to another release")
        _sha(replica["start_snapshot_sha256"], field=f"prometheus_range.replicas[{index}].start_snapshot_sha256")
        _sha(replica["end_snapshot_sha256"], field=f"prometheus_range.replicas[{index}].end_snapshot_sha256")
        expected_binding = expected_replica_bindings.get(replica_id)
        if expected_binding is None or any(replica.get(name) != expected_binding[name] for name in expected_binding):
            raise ReceiptValidationError(
                f"Prometheus range replica {replica_id} differs from the fresh container/image/snapshot binding"
            )
        replica_ids.append(replica_id)
        replica_container_ids[replica_id] = container_id
    if replica_ids != expected_ids:
        raise ReceiptValidationError("Prometheus range replicas must be sorted and exact")

    series = _mapping(payload["series"], field="prometheus_range.series")
    _exact_keys(series, {"result_type", "count", "sha256"}, field="prometheus_range.series")
    if series["result_type"] != "matrix":
        raise ReceiptValidationError("Prometheus range result type must be matrix")
    count = series["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ReceiptValidationError("Prometheus range series count must be positive")
    _sha(series["sha256"], field="prometheus_range.series.sha256")
    _sha(payload["range_response_sha256"], field="prometheus_range.range_response_sha256")
    response_bytes = payload["range_response_bytes"]
    if isinstance(response_bytes, bool) or not isinstance(response_bytes, int) or response_bytes <= 0:
        raise ReceiptValidationError("Prometheus range response byte count must be positive")
    payload_hash = _verify_stored_payload_hash(payload, field="prometheus_range")
    try:
        evidence_contract.verify_authenticated_payload(
            payload,
            domain=evidence_contract.RANGE_DOMAIN,
            anchor=anchor,
            challenge=challenge,
            field="prometheus_range",
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    return {
        "expected_replica_ids": expected_ids,
        "prometheus_config_sha256": config_hash,
        "release_commit_sha": expected_commit_sha,
        "release_image_digest": expected_image_digest,
        "series_count": count,
        "series_sha256": _sha(series["sha256"], field="prometheus_range.series.sha256"),
        "range_response_sha256": _sha(
            payload["range_response_sha256"],
            field="prometheus_range.range_response_sha256",
        ),
        "range_response_bytes": response_bytes,
        "window_start": start,
        "window_end": end,
        "step_seconds": step_seconds,
        "replica_container_ids": replica_container_ids,
        "payload_sha256": payload_hash,
    }


def validate_prometheus_range_response(
    response: Mapping[str, object],
    raw: bytes,
    *,
    receipt_result: Mapping[str, object],
) -> dict[str, object]:
    _exact_keys(response, {"status", "data"}, field="Prometheus range response")
    if response["status"] != "success":
        raise ReceiptValidationError("Prometheus range response did not report success")
    data = _mapping(response["data"], field="Prometheus range response.data")
    _exact_keys(data, {"resultType", "result"}, field="Prometheus range response.data")
    if data["resultType"] != "matrix":
        raise ReceiptValidationError("Prometheus range response resultType must be matrix")
    result = _list(data["result"], field="Prometheus range response.data.result")
    if len(result) != receipt_result["series_count"]:
        raise ReceiptValidationError("Prometheus range response series count differs from receipt")
    expected_ids = set(receipt_result["expected_replica_ids"])
    start = receipt_result["window_start"]
    end = receipt_result["window_end"]
    step_seconds = receipt_result["step_seconds"]
    assert isinstance(start, datetime) and isinstance(end, datetime) and isinstance(step_seconds, int)
    window_seconds = (end - start).total_seconds()
    if not window_seconds.is_integer() or int(window_seconds) % step_seconds:
        raise ReceiptValidationError("Prometheus range response window is not step-aligned")
    expected_sample_count = int(window_seconds) // step_seconds + 1
    represented_ids: set[str] = set()
    for series_index, raw_series in enumerate(result):
        series = _mapping(raw_series, field=f"Prometheus range series[{series_index}]")
        _exact_keys(series, {"metric", "values"}, field=f"Prometheus range series[{series_index}]")
        metric = _mapping(series["metric"], field=f"Prometheus range series[{series_index}].metric")
        replica_id = _text(metric.get("replica_id"), field=f"Prometheus range series[{series_index}].metric.replica_id")
        container_id = _text(
            metric.get("container_id"),
            field=f"Prometheus range series[{series_index}].metric.container_id",
        )
        if replica_id not in expected_ids:
            raise ReceiptValidationError("Prometheus range response contains an unexpected replica")
        if (
            metric.get("release_commit_sha") != receipt_result["release_commit_sha"]
            or metric.get("release_image_digest") != receipt_result["release_image_digest"]
        ):
            raise ReceiptValidationError("Prometheus range response contains series from another release")
        if container_id != receipt_result["replica_container_ids"][replica_id]:
            raise ReceiptValidationError("Prometheus range response container identity differs from receipt")
        values = _list(series["values"], field=f"Prometheus range series[{series_index}].values")
        if len(values) != expected_sample_count:
            raise ReceiptValidationError(
                "Prometheus range response series is sparse or has a scrape-continuity gap"
            )
        metric_name = _text(
            metric.get("__name__"),
            field=f"Prometheus range series[{series_index}].metric.__name__",
        )
        previous_timestamp = -math.inf
        previous_value = -math.inf
        for sample_index, raw_sample in enumerate(values):
            sample = _list(raw_sample, field=f"Prometheus range series[{series_index}].values[{sample_index}]")
            if len(sample) != 2:
                raise ReceiptValidationError("Prometheus range response sample must contain timestamp and value")
            try:
                timestamp = float(sample[0])
                value = float(sample[1])
            except (TypeError, ValueError) as exc:
                raise ReceiptValidationError("Prometheus range response sample is not numeric") from exc
            if not math.isfinite(timestamp) or not math.isfinite(value) or timestamp <= previous_timestamp:
                raise ReceiptValidationError("Prometheus range response samples must be finite and time-ordered")
            expected_timestamp = start.timestamp() + sample_index * step_seconds
            if abs(timestamp - expected_timestamp) > 1.0:
                raise ReceiptValidationError(
                    "Prometheus range response sample is not aligned to the canonical query cadence"
                )
            if value < 0:
                raise ReceiptValidationError("Prometheus range response sample is negative")
            if metric_name == "propertyquarry_runtime_build_info":
                if value != 1.0:
                    raise ReceiptValidationError(
                        "Prometheus range runtime identity gauge is invalid"
                    )
            elif value < previous_value:
                raise ReceiptValidationError(
                    "Prometheus range response contains an interior counter reset"
                )
            previous_timestamp = timestamp
            previous_value = value
        if abs(previous_timestamp - end.timestamp()) > 1.0:
            raise ReceiptValidationError(
                "Prometheus range response does not cover the complete query window"
            )
        represented_ids.add(replica_id)
    if represented_ids != expected_ids:
        raise ReceiptValidationError("Prometheus range response does not represent every expected replica")
    normalized_hash = sha256_bytes(canonical_json_bytes(result))
    if normalized_hash != receipt_result["series_sha256"]:
        raise ReceiptValidationError("Prometheus range normalized series hash differs from receipt")
    raw_hash = sha256_bytes(raw)
    if raw_hash != receipt_result["range_response_sha256"] or len(raw) != receipt_result["range_response_bytes"]:
        raise ReceiptValidationError("Prometheus range raw response bytes differ from receipt")
    return {
        "file_sha256": raw_hash,
        "bytes": len(raw),
        "series_sha256": normalized_hash,
        "series_count": len(result),
    }


def validate_snapshot_bundle_identity(
    payload: Mapping[str, object],
    raw: bytes,
    *,
    expected_commit_sha: str,
    expected_image_digest: str,
    challenge: evidence_contract.EvidenceChallenge,
    now: datetime,
) -> tuple[str, dict[str, dict[str, str]]]:
    _exact_keys(
        payload,
        {
            "schema",
            "capture_tool",
            "release_commit_sha",
            "release_image_digest",
            "window_start",
            "window_end",
            "window_seconds",
            "replica_count",
            "replicas",
            "payload_sha256",
        },
        field="metrics snapshot bundle",
    )
    if payload["schema"] != "propertyquarry.metrics_snapshot_bundle.v2" or payload["capture_tool"] != "propertyquarry.slo_metrics_capture.v2":
        raise ReceiptValidationError("metrics snapshot bundle identity is invalid")
    if payload["release_commit_sha"] != expected_commit_sha or payload["release_image_digest"] != expected_image_digest:
        raise ReceiptValidationError("metrics snapshot bundle belongs to another release")
    _verify_stored_payload_hash(payload, field="metrics snapshot bundle")
    try:
        evidence_contract.validate_evidence_time(
            payload["window_end"], field="metrics snapshot bundle window_end", now=now, challenge=challenge
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    replicas = _list(payload["replicas"], field="metrics snapshot bundle replicas")
    count = payload["replica_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count != len(replicas) or count < 1:
        raise ReceiptValidationError("metrics snapshot replica count is invalid")
    bindings: dict[str, dict[str, str]] = {}
    for index, raw_replica in enumerate(replicas):
        replica = _mapping(raw_replica, field=f"metrics snapshot replica[{index}]")
        required = {
            "container_id",
            "container_image_id",
            "replica_id",
            "release_commit_sha",
            "release_image_digest",
            "docker_inspect_sha256",
            "start",
            "end",
        }
        _exact_keys(replica, required, field=f"metrics snapshot replica[{index}]")
        replica_id = _text(replica["replica_id"], field=f"metrics snapshot replica[{index}].replica_id")
        if replica_id in bindings:
            raise ReceiptValidationError("metrics snapshot contains duplicate replica identities")
        start_ref = _mapping(replica["start"], field=f"metrics snapshot replica[{index}].start")
        end_ref = _mapping(replica["end"], field=f"metrics snapshot replica[{index}].end")
        for phase, reference in (("start", start_ref), ("end", end_ref)):
            _exact_keys(reference, {"captured_at", "path", "sha256", "bytes"}, field=f"metrics snapshot replica[{index}].{phase}")
            _sha(reference["sha256"], field=f"metrics snapshot replica[{index}].{phase}.sha256")
        binding = {
            "replica_id": replica_id,
            "container_id": _text(replica["container_id"], field=f"metrics snapshot replica[{index}].container_id"),
            "container_image_id": _text(replica["container_image_id"], field=f"metrics snapshot replica[{index}].container_image_id"),
            "release_commit_sha": _text(replica["release_commit_sha"], field=f"metrics snapshot replica[{index}].release_commit_sha"),
            "release_image_digest": _text(replica["release_image_digest"], field=f"metrics snapshot replica[{index}].release_image_digest"),
            "start_snapshot_sha256": str(start_ref["sha256"]),
            "end_snapshot_sha256": str(end_ref["sha256"]),
        }
        if binding["release_commit_sha"] != expected_commit_sha or binding["release_image_digest"] != expected_image_digest:
            raise ReceiptValidationError("metrics snapshot replica belongs to another release")
        if not IMAGE_DIGEST_RE.fullmatch(binding["container_image_id"]):
            raise ReceiptValidationError("metrics snapshot replica image identity is invalid")
        bindings[replica_id] = binding
    if sorted(bindings) != list(bindings):
        raise ReceiptValidationError("metrics snapshot replicas must be sorted by replica ID")
    return sha256_bytes(raw), bindings


def _normalize_expected_release(release_commit_sha: str, release_image_digest: str) -> tuple[str, str]:
    commit_sha = str(release_commit_sha or "").strip().lower()
    image_digest = str(release_image_digest or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(commit_sha):
        raise ReceiptValidationError("expected release SHA must be 40 lowercase hexadecimal characters")
    if not IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise ReceiptValidationError("expected image digest must be sha256:<64 lowercase hex>")
    return commit_sha, image_digest


def verify_receipt_bundle(
    *,
    release_commit_sha: str,
    release_image_digest: str,
    monitoring_receipt_path: Path,
    metrics_snapshot_path: Path,
    metrics_probe_path: Path | None = None,
    prometheus_range_receipt_path: Path,
    prometheus_range_response_path: Path,
    alert_delivery_receipt_path: Path,
    dashboard_render_receipt_path: Path,
    structured_log_query_receipt_path: Path,
    distributed_trace_query_receipt_path: Path,
    now: datetime | None = None,
    expected_input_hashes: Mapping[str, str] | None = None,
) -> dict[str, object]:
    commit_sha, image_digest = _normalize_expected_release(release_commit_sha, release_image_digest)
    checked_at = (now or utc_now()).astimezone(timezone.utc)
    try:
        anchor, challenge = evidence_contract.load_evidence_challenge(
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            now=checked_at,
        )
        operator_gateway_trust = evidence_contract.load_operator_gateway_trust(
            evidence_anchor=anchor
        )
        canonical_monitoring_identity = (
            evidence_contract.load_canonical_monitoring_identity()
        )
    except evidence_contract.EvidenceContractError as exc:
        raise ReceiptValidationError(str(exc)) from exc
    snapshot_payload, snapshot_raw = load_json_receipt(metrics_snapshot_path, name="metrics snapshot bundle")
    metrics_probe_raw = (
        load_json_receipt(metrics_probe_path, name="metrics probe bundle")[1]
        if metrics_probe_path is not None
        else None
    )
    snapshot_sha256, replica_bindings = validate_snapshot_bundle_identity(
        snapshot_payload,
        snapshot_raw,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        challenge=challenge,
        now=checked_at,
    )
    monitoring_payload, monitoring_raw = load_json_receipt(monitoring_receipt_path, name="monitoring-runtime")
    range_payload, range_raw = load_json_receipt(prometheus_range_receipt_path, name="Prometheus-range")
    range_response, range_response_raw = load_prometheus_range_response(prometheus_range_response_path)
    alert_payload, alert_raw = load_json_receipt(alert_delivery_receipt_path, name="alert-delivery")
    shared_input_hashes = {
        "metrics_snapshot": sha256_bytes(snapshot_raw),
        "monitoring_receipt": sha256_bytes(monitoring_raw),
        "prometheus_range_receipt": sha256_bytes(range_raw),
        "prometheus_range_response": sha256_bytes(range_response_raw),
        "alert_delivery_receipt": sha256_bytes(alert_raw),
    }
    if metrics_probe_raw is not None:
        shared_input_hashes["metrics_probe"] = sha256_bytes(metrics_probe_raw)
    expected_operations_hashes = None
    if expected_input_hashes is not None:
        expected_operations_hashes = {
            name: str(expected_input_hashes[name])
            for name in OPERATIONS_SHARED_INPUT_NAMES
            if name in expected_input_hashes
        }
    operations_evidence_raw = verify_operations_evidence(
        release_commit_sha=commit_sha,
        release_image_digest=image_digest,
        dashboard_render_receipt_path=dashboard_render_receipt_path,
        structured_log_query_receipt_path=structured_log_query_receipt_path,
        distributed_trace_query_receipt_path=distributed_trace_query_receipt_path,
        now=checked_at,
        expected_input_hashes=expected_operations_hashes,
    )
    if not isinstance(operations_evidence_raw, Mapping):
        raise ReceiptValidationError(
            "flagship operations verifier did not return a receipt object"
        )
    operations_evidence = dict(operations_evidence_raw)
    operations_input_hashes = _mapping(
        operations_evidence.get("shared_input_hashes"),
        field="flagship operations shared input hashes",
    )
    if set(operations_input_hashes) != set(OPERATIONS_SHARED_INPUT_NAMES):
        raise ReceiptValidationError(
            "flagship operations shared input hash set is not canonical"
        )
    for name in OPERATIONS_SHARED_INPUT_NAMES:
        value = operations_input_hashes.get(name)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise ReceiptValidationError(
                f"flagship operations shared input hash is invalid: {name}"
            )
        if name in shared_input_hashes:
            raise ReceiptValidationError(
                f"flagship operations shared input hash overlaps: {name}"
            )
        shared_input_hashes[name] = value
    if expected_input_hashes is not None and shared_input_hashes != dict(expected_input_hashes):
        raise ReceiptValidationError("observability shared launch input hash set differs")
    if (
        operations_evidence.get("schema_version") != OPERATIONS_VERIFICATION_SCHEMA
        or operations_evidence.get("producer")
        != OPERATIONS_VERIFICATION_PRODUCER
        or operations_evidence.get("status") != "verified"
        or operations_evidence.get("source_contract_status")
        != "defined_not_live_evidence"
        or operations_evidence.get("cross_receipt_links_verified") is not True
    ):
        raise ReceiptValidationError(
            "flagship operations verifier receipt is not canonical and verified"
        )
    if operations_evidence.get("release") != {
        "commit_sha": commit_sha,
        "image_digest": image_digest,
    }:
        raise ReceiptValidationError(
            "flagship operations verifier release differs"
        )
    if operations_evidence.get("deployment_id") != challenge.deployment_id:
        raise ReceiptValidationError(
            "flagship operations verifier deployment differs"
        )
    if operations_evidence.get("challenge_sha256") != challenge.artifact_sha256:
        raise ReceiptValidationError(
            "flagship operations verifier challenge differs"
        )
    if operations_evidence.get("policy_sha256") != challenge.policy_hashes.get(
        "flagship_operations_sha256"
    ):
        raise ReceiptValidationError(
            "flagship operations verifier policy differs"
        )
    if operations_evidence.get("payload_sha256") != compute_payload_sha256(
        operations_evidence
    ):
        raise ReceiptValidationError(
            "flagship operations verifier payload hash differs"
        )
    monitoring = validate_monitoring_runtime_receipt(
        monitoring_payload,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        expected_snapshot_bundle_sha256=snapshot_sha256,
        expected_replica_bindings=replica_bindings,
        anchor=anchor,
        operator_gateway_trust=operator_gateway_trust,
        canonical_monitoring_identity=canonical_monitoring_identity,
        challenge=challenge,
        now=checked_at,
    )
    operations_replica_ids = operations_evidence.get("replica_ids")
    if (
        not isinstance(operations_replica_ids, list)
        or not operations_replica_ids
        or any(
            not isinstance(replica_id, str)
            or REPLICA_ID_RE.fullmatch(replica_id) is None
            or replica_id == "UNCONFIGURED"
            for replica_id in operations_replica_ids
        )
        or operations_replica_ids != sorted(set(operations_replica_ids))
    ):
        raise ReceiptValidationError(
            "flagship operations verifier replica_ids are not a sorted unique valid list"
        )
    if operations_replica_ids != monitoring["expected_replica_ids"]:
        raise ReceiptValidationError(
            "flagship operations and monitoring replica sets differ"
        )
    range_result = validate_prometheus_range_receipt(
        range_payload,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        expected_snapshot_bundle_sha256=snapshot_sha256,
        expected_replica_bindings=replica_bindings,
        anchor=anchor,
        challenge=challenge,
        now=checked_at,
    )
    range_response_result = validate_prometheus_range_response(
        range_response,
        range_response_raw,
        receipt_result=range_result,
    )
    alert = validate_alert_delivery_receipt(
        alert_payload,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        operator_gateway_trust=operator_gateway_trust,
        challenge=challenge,
        now=checked_at,
    )
    if monitoring["expected_replica_ids"] != range_result["expected_replica_ids"]:
        raise ReceiptValidationError("short-window and long-window replica sets differ")
    identity = _mapping(monitoring["identity"], field="monitoring identity")
    if identity["prometheus_config_sha256"] != range_result["prometheus_config_sha256"]:
        raise ReceiptValidationError("short-window and long-window Prometheus config identities differ")
    alert_file_hash = sha256_bytes(alert_raw)
    if monitoring["alert_delivery_receipt_sha256"] != alert_file_hash:
        raise ReceiptValidationError("monitoring receipt does not bind the supplied alert-delivery receipt bytes")
    if alert["sent_at"] < monitoring["started_at"] or alert["received_at"] > monitoring["completed_at"]:
        raise ReceiptValidationError("alert delivery did not occur inside the monitoring proof window")

    verified_at = checked_at
    receipt = {
        "schema_version": VERIFICATION_SCHEMA,
        "producer": VERIFICATION_PRODUCER,
        "verified_at": isoformat(verified_at),
        "release": {"commit_sha": commit_sha, "image_digest": image_digest},
        "deployment_id": challenge.deployment_id,
        "challenge_sha256": challenge.artifact_sha256,
        "operator_gateway": {
            "key_id": operator_gateway_trust.key_id,
            "audience": operator_gateway_trust.audience,
            "endpoint_origin": operator_gateway_trust.endpoint_origin,
            "tls_spki_sha256": operator_gateway_trust.tls_spki_sha256,
            "trust_anchor_sha256": operator_gateway_trust.file_sha256,
        },
        "policy_hashes": dict(challenge.policy_hashes),
        "canonical_monitoring_identity": dict(
            canonical_monitoring_identity["identity"]
        ),
        "monitoring_tools": dict(canonical_monitoring_identity["monitoring_tools"]),
        "shared_input_hashes": shared_input_hashes,
        "operations_evidence": operations_evidence,
        "snapshot_bundle_sha256": snapshot_sha256,
        "status": "verified",
        "replica_ids": monitoring["expected_replica_ids"],
        "receipts": {
            "monitoring_runtime": {
                "file_sha256": sha256_bytes(monitoring_raw),
                "payload_sha256": monitoring["payload_sha256"],
            },
            "prometheus_range": {
                "file_sha256": sha256_bytes(range_raw),
                "payload_sha256": range_result["payload_sha256"],
            },
            "prometheus_range_response": range_response_result,
            "alert_delivery": {
                "file_sha256": alert_file_hash,
                "payload_sha256": alert["payload_sha256"],
            },
        },
        "cross_receipt_links_verified": True,
    }
    return add_payload_sha256(receipt)


def atomic_write_json(path: Path, payload: Mapping[str, object], *, overwrite: bool) -> None:
    try:
        encoded = (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError("output payload is not finite JSON") from exc
    try:
        secure_file_io.atomic_write_bytes(path, encoded, overwrite=overwrite)
    except secure_file_io.OutputExistsError as exc:
        raise ReceiptValidationError(
            "output cannot be published safely: output already exists; "
            "use --overwrite to replace it"
        ) from exc
    except secure_file_io.SecureFileIOError as exc:
        raise ReceiptValidationError(
            f"output cannot be published safely: {exc}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify canonical authenticated v2 observability receipts")
    verify.add_argument("--release-sha", required=True)
    verify.add_argument("--image-digest", required=True)
    verify.add_argument("--monitoring-receipt", type=Path, required=True)
    verify.add_argument("--metrics-snapshot", type=Path, required=True)
    verify.add_argument("--metrics-probe", type=Path, required=True)
    verify.add_argument("--prometheus-range-receipt", type=Path, required=True)
    verify.add_argument("--prometheus-range-response", type=Path, required=True)
    verify.add_argument("--alert-delivery-receipt", type=Path, required=True)
    verify.add_argument("--dashboard-render-receipt", type=Path, required=True)
    verify.add_argument("--structured-log-query-receipt", type=Path, required=True)
    verify.add_argument("--distributed-trace-query-receipt", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = verify_receipt_bundle(
            release_commit_sha=args.release_sha,
            release_image_digest=args.image_digest,
            monitoring_receipt_path=args.monitoring_receipt,
            metrics_snapshot_path=args.metrics_snapshot,
            metrics_probe_path=args.metrics_probe,
            prometheus_range_receipt_path=args.prometheus_range_receipt,
            prometheus_range_response_path=args.prometheus_range_response,
            alert_delivery_receipt_path=args.alert_delivery_receipt,
            dashboard_render_receipt_path=args.dashboard_render_receipt,
            structured_log_query_receipt_path=args.structured_log_query_receipt,
            distributed_trace_query_receipt_path=args.distributed_trace_query_receipt,
        )
        atomic_write_json(args.output, receipt, overwrite=args.overwrite)
    except ReceiptValidationError as exc:
        print(f"observability receipt verification failed: {exc}", file=sys.stderr)
        return 2
    print(f"observability receipts verified: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
