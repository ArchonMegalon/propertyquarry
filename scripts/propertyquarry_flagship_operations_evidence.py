#!/usr/bin/env python3
"""Verify authenticated PropertyQuarry flagship operations evidence.

The canonical operations policy is configuration, not evidence.  A verified
result additionally requires fresh, independently authenticated dashboard,
structured-log, and distributed-trace receipts bound to the active external
release-control challenge.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

if __package__:
    from scripts import propertyquarry_evidence_contract as evidence_contract
    from scripts import propertyquarry_secure_file_io as secure_file_io
else:
    import propertyquarry_evidence_contract as evidence_contract
    import propertyquarry_secure_file_io as secure_file_io


DASHBOARD_RENDER_SCHEMA = "propertyquarry.dashboard-render-receipt.v1"
DASHBOARD_RENDER_PRODUCER = "propertyquarry-dashboard-render-capture"
STRUCTURED_LOG_QUERY_SCHEMA = "propertyquarry.structured-log-query-receipt.v1"
STRUCTURED_LOG_QUERY_PRODUCER = "propertyquarry-structured-log-query-capture"
DISTRIBUTED_TRACE_QUERY_SCHEMA = "propertyquarry.distributed-trace-query-receipt.v1"
DISTRIBUTED_TRACE_QUERY_PRODUCER = "propertyquarry-distributed-trace-query-capture"
VERIFICATION_SCHEMA = "propertyquarry.flagship-operations-evidence-verification.v1"
VERIFICATION_PRODUCER = "propertyquarry-flagship-operations-evidence-verifier"
OPERATIONS_POLICY_PATH = evidence_contract.CANONICAL_POLICY_PATHS[
    "flagship_operations_sha256"
]
COMMON_RECEIPT_KEYS = {
    "schema_version",
    "producer",
    "deployment_id",
    "challenge_nonce",
    "captured_at",
    "release",
    "replica_ids",
    "policy_sha256",
    "window",
    "evidence",
    "payload_sha256",
    "authentication",
}
SHARED_INPUT_NAMES = (
    "dashboard_render_receipt",
    "structured_log_query_receipt",
    "distributed_trace_query_receipt",
)
WINDOW_SECONDS = 6 * 60 * 60
MAX_POLICY_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RENDER_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_PANEL_SAMPLE_COUNT = 10_000_000
MAX_LOG_SAMPLES = 100
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class OperationsEvidenceValidationError(RuntimeError):
    """An operations policy or live evidence receipt is invalid."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        return evidence_contract.canonical_json_bytes(value)
    except evidence_contract.EvidenceContractError as exc:
        raise OperationsEvidenceValidationError(str(exc)) from exc


def sha256_bytes(value: bytes) -> str:
    return evidence_contract.sha256_bytes(value)


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


def isoformat(value: datetime) -> str:
    return evidence_contract.isoformat(value)


def _reject_constant(raw: str) -> object:
    raise OperationsEvidenceValidationError(
        f"non-finite JSON constant is forbidden: {raw}"
    )


def _unique_object(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperationsEvidenceValidationError(
                f"duplicate JSON key is forbidden: {key}"
            )
        result[key] = value
    return result


def _load_strict_json(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> tuple[dict[str, object], bytes]:
    try:
        raw = secure_file_io.read_stable_bytes(
            path,
            maximum_bytes=maximum_bytes,
            require_nonempty=True,
        )
    except secure_file_io.SecureFileIOError as exc:
        raise OperationsEvidenceValidationError(
            f"{field} is unavailable or unsafe: {exc}"
        ) from exc
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationsEvidenceValidationError(
            f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise OperationsEvidenceValidationError(f"{field} must be a JSON object")
    return parsed, raw


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise OperationsEvidenceValidationError(f"{field} must be an object")
    return value


def _list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise OperationsEvidenceValidationError(f"{field} must be an array")
    return value


def _exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    *,
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise OperationsEvidenceValidationError(
            f"{field} keys do not match the v1 contract; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def _text(value: object, *, field: str, maximum_length: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum_length
    ):
        raise OperationsEvidenceValidationError(
            f"{field} must be a non-empty bounded trimmed string"
        )
    return value


def _sha(value: object, *, field: str) -> str:
    digest = _text(value, field=field, maximum_length=64)
    if not SHA256_RE.fullmatch(digest):
        raise OperationsEvidenceValidationError(
            f"{field} must be 64 lowercase hexadecimal characters"
        )
    return digest


def _timestamp(value: object, *, field: str) -> datetime:
    text = _text(value, field=field, maximum_length=64)
    if not text.endswith("Z"):
        raise OperationsEvidenceValidationError(
            f"{field} must be a UTC timestamp ending in Z"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise OperationsEvidenceValidationError(
            f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise OperationsEvidenceValidationError(f"{field} must be UTC")
    return parsed.astimezone(timezone.utc)


def _string_list(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
) -> list[str]:
    raw = _list(value, field=field)
    if not raw and not allow_empty:
        raise OperationsEvidenceValidationError(f"{field} must not be empty")
    result = [
        _text(item, field=f"{field}[{index}]")
        for index, item in enumerate(raw)
    ]
    if len(result) != len(set(result)):
        raise OperationsEvidenceValidationError(f"{field} must be unique")
    return result


def _replica_ids(value: object, *, field: str) -> list[str]:
    replica_ids = _string_list(value, field=field)
    if replica_ids != sorted(replica_ids):
        raise OperationsEvidenceValidationError(
            f"{field} must be sorted and unique"
        )
    if any(
        replica_id == "UNCONFIGURED"
        or not REPLICA_ID_RE.fullmatch(replica_id)
        for replica_id in replica_ids
    ):
        raise OperationsEvidenceValidationError(
            f"{field} contains an unconfigured or invalid replica ID"
        )
    return replica_ids


def _positive_int(value: object, *, field: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise OperationsEvidenceValidationError(
            f"{field} must be a positive bounded JSON integer"
        )
    return value


def _normalize_release(
    release_commit_sha: str,
    release_image_digest: str,
) -> tuple[str, str]:
    commit_sha = str(release_commit_sha or "").strip().lower()
    image_digest = str(release_image_digest or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(commit_sha):
        raise OperationsEvidenceValidationError(
            "expected release SHA must be 40 lowercase hexadecimal characters"
        )
    if not IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise OperationsEvidenceValidationError(
            "expected image digest must be sha256:<64 lowercase hex>"
        )
    return commit_sha, image_digest


def _release(
    value: object,
    *,
    field: str,
    expected_commit_sha: str,
    expected_image_digest: str,
) -> None:
    release = _mapping(value, field=field)
    _exact_keys(release, {"commit_sha", "image_digest"}, field=field)
    commit_sha = _text(
        release["commit_sha"], field=f"{field}.commit_sha", maximum_length=40
    )
    image_digest = _text(
        release["image_digest"], field=f"{field}.image_digest", maximum_length=71
    )
    if not GIT_SHA_RE.fullmatch(commit_sha) or not IMAGE_DIGEST_RE.fullmatch(
        image_digest
    ):
        raise OperationsEvidenceValidationError(f"{field} identity is malformed")
    if (
        commit_sha != expected_commit_sha
        or image_digest != expected_image_digest
    ):
        raise OperationsEvidenceValidationError(
            f"{field} belongs to a different release"
        )


def _validate_policy(
    policy: Mapping[str, object],
) -> dict[str, object]:
    _exact_keys(
        policy,
        {
            "schema_version",
            "service",
            "source_contract_status",
            "dashboard_id",
            "title",
            "editable",
            "default_window",
            "release_filters",
            "required_live_receipts",
            "panels",
        },
        field="flagship operations policy",
    )
    if (
        policy["schema_version"] != "propertyquarry.flagship-operations.v1"
        or policy["service"] != "propertyquarry"
        or policy["source_contract_status"] != "defined_not_live_evidence"
        or policy["editable"] is not False
        or policy["default_window"] != "6h"
    ):
        raise OperationsEvidenceValidationError(
            "flagship operations policy identity or source status is invalid"
        )
    _text(policy["dashboard_id"], field="flagship operations policy.dashboard_id")
    _text(policy["title"], field="flagship operations policy.title")
    release_filters = _string_list(
        policy["release_filters"],
        field="flagship operations policy.release_filters",
    )
    if release_filters != [
        "release_commit_sha",
        "release_image_digest",
        "replica_id",
    ]:
        raise OperationsEvidenceValidationError(
            "flagship operations release filters are not canonical"
        )
    required = _mapping(
        policy["required_live_receipts"],
        field="flagship operations policy.required_live_receipts",
    )
    _exact_keys(
        required,
        {
            "max_age_seconds",
            "kinds",
            "exact_release_binding",
            "independent_authentication",
        },
        field="flagship operations policy.required_live_receipts",
    )
    max_age_seconds = _positive_int(
        required["max_age_seconds"],
        field="flagship operations policy.required_live_receipts.max_age_seconds",
        maximum=evidence_contract.MAX_EVIDENCE_AGE_SECONDS,
    )
    if (
        max_age_seconds != evidence_contract.MAX_EVIDENCE_AGE_SECONDS
        or required["kinds"]
        != [
            "dashboard_render",
            "structured_log_query",
            "distributed_trace_query",
            "alert_delivery",
        ]
        or required["exact_release_binding"] is not True
        or required["independent_authentication"] is not True
    ):
        raise OperationsEvidenceValidationError(
            "flagship operations live-receipt requirements are not canonical"
        )

    raw_panels = _list(policy["panels"], field="flagship operations policy.panels")
    if not raw_panels:
        raise OperationsEvidenceValidationError(
            "flagship operations policy must define panels"
        )
    panels: list[Mapping[str, object]] = []
    panel_by_id: dict[str, Mapping[str, object]] = {}
    for index, raw_panel in enumerate(raw_panels):
        field = f"flagship operations policy.panels[{index}]"
        panel = _mapping(raw_panel, field=field)
        source = _text(panel.get("source"), field=f"{field}.source")
        expected_keys = (
            {"id", "title", "source", "queries", "thresholds", "runbook"}
            if source == "prometheus"
            else {"id", "title", "source", "query_contract", "runbook"}
        )
        _exact_keys(panel, expected_keys, field=field)
        panel_id = _text(panel["id"], field=f"{field}.id")
        _text(panel["title"], field=f"{field}.title")
        _text(panel["runbook"], field=f"{field}.runbook")
        if panel_id in panel_by_id:
            raise OperationsEvidenceValidationError(
                "flagship operations policy contains duplicate panel IDs"
            )
        if source == "prometheus":
            _string_list(panel["queries"], field=f"{field}.queries")
            thresholds = _mapping(panel["thresholds"], field=f"{field}.thresholds")
            if not thresholds:
                raise OperationsEvidenceValidationError(
                    f"{field}.thresholds must not be empty"
                )
            canonical_json_bytes(thresholds)
        elif source == "logs":
            query = _mapping(panel["query_contract"], field=f"{field}.query_contract")
            _exact_keys(
                query,
                {"required_fields", "filter_fields", "private_payload_allowed"},
                field=f"{field}.query_contract",
            )
            required_fields = _string_list(
                query["required_fields"],
                field=f"{field}.query_contract.required_fields",
            )
            filter_fields = _string_list(
                query["filter_fields"],
                field=f"{field}.query_contract.filter_fields",
            )
            if (
                required_fields
                != [
                    "timestamp",
                    "service",
                    "event",
                    "correlation_id",
                    "trace_id",
                    "span_id",
                    "release_commit_sha",
                    "release_image_digest",
                    "replica_id",
                ]
                or filter_fields
                != [
                    "correlation_id",
                    "trace_id",
                    "release_commit_sha",
                    "release_image_digest",
                ]
                or query["private_payload_allowed"] is not False
            ):
                raise OperationsEvidenceValidationError(
                    "structured-log query contract is not canonical"
                )
        elif source == "traces":
            query = _mapping(panel["query_contract"], field=f"{field}.query_contract")
            _exact_keys(
                query,
                {
                    "propagation_format",
                    "required_boundaries",
                    "same_trace_id_required",
                    "distinct_nonzero_span_ids_required",
                    "release_attributes_required",
                    "parent_chain_required",
                },
                field=f"{field}.query_contract",
            )
            if (
                query["propagation_format"] != "W3C traceparent v00"
                or _string_list(
                    query["required_boundaries"],
                    field=f"{field}.query_contract.required_boundaries",
                )
                != [
                    "customer_api",
                    "durable_search_worker",
                    "provider_or_render_boundary",
                ]
                or query["same_trace_id_required"] is not True
                or query["distinct_nonzero_span_ids_required"] is not True
                or query["release_attributes_required"] is not True
                or query["parent_chain_required"] is not True
            ):
                raise OperationsEvidenceValidationError(
                    "distributed-trace query contract is not canonical"
                )
        else:
            raise OperationsEvidenceValidationError(
                f"{field}.source is not a supported canonical source"
            )
        panels.append(panel)
        panel_by_id[panel_id] = panel
    if sum(panel["source"] == "logs" for panel in panels) != 1 or sum(
        panel["source"] == "traces" for panel in panels
    ) != 1:
        raise OperationsEvidenceValidationError(
            "flagship operations policy must define exactly one log and one trace panel"
        )
    return {
        "max_age_seconds": max_age_seconds,
        "panels": panels,
        "panel_by_id": panel_by_id,
        "release_filters": release_filters,
    }


def _query_contract_sha256(panel: Mapping[str, object]) -> str:
    contract = panel["queries"] if "queries" in panel else panel["query_contract"]
    return sha256_bytes(canonical_json_bytes(contract))


def _validate_window(
    value: object,
    *,
    field: str,
    captured_at: datetime,
    now: datetime,
    challenge: evidence_contract.EvidenceChallenge,
    maximum_age_seconds: int,
) -> tuple[datetime, datetime]:
    window = _mapping(value, field=field)
    _exact_keys(window, {"start", "end"}, field=field)
    start = _timestamp(window["start"], field=f"{field}.start")
    end = _timestamp(window["end"], field=f"{field}.end")
    if (end - start).total_seconds() != WINDOW_SECONDS:
        raise OperationsEvidenceValidationError(
            f"{field} must be exactly six hours"
        )
    if end > captured_at:
        raise OperationsEvidenceValidationError(
            f"{field}.end must not be later than captured_at"
        )
    try:
        evidence_contract.validate_evidence_time(
            window["end"],
            field=f"{field}.end",
            now=now,
            challenge=challenge,
            maximum_age_seconds=maximum_age_seconds,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise OperationsEvidenceValidationError(str(exc)) from exc
    return start, end


def _validate_common_receipt(
    payload: Mapping[str, object],
    *,
    field: str,
    schema: str,
    producer: str,
    domain: str,
    expected_commit_sha: str,
    expected_image_digest: str,
    policy_sha256: str,
    maximum_age_seconds: int,
    anchor: evidence_contract.TrustAnchor,
    challenge: evidence_contract.EvidenceChallenge,
    now: datetime,
) -> dict[str, object]:
    _exact_keys(payload, COMMON_RECEIPT_KEYS, field=field)
    if payload["schema_version"] != schema or payload["producer"] != producer:
        raise OperationsEvidenceValidationError(
            f"{field} schema or producer is not canonical"
        )
    if _sha(payload["policy_sha256"], field=f"{field}.policy_sha256") != policy_sha256:
        raise OperationsEvidenceValidationError(
            f"{field} is not bound to the canonical operations policy"
        )
    _release(
        payload["release"],
        field=f"{field}.release",
        expected_commit_sha=expected_commit_sha,
        expected_image_digest=expected_image_digest,
    )
    replica_ids = _replica_ids(
        payload["replica_ids"],
        field=f"{field}.replica_ids",
    )
    try:
        captured_at = evidence_contract.validate_evidence_time(
            payload["captured_at"],
            field=f"{field}.captured_at",
            now=now,
            challenge=challenge,
            maximum_age_seconds=maximum_age_seconds,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise OperationsEvidenceValidationError(str(exc)) from exc
    start, end = _validate_window(
        payload["window"],
        field=f"{field}.window",
        captured_at=captured_at,
        now=now,
        challenge=challenge,
        maximum_age_seconds=maximum_age_seconds,
    )
    stored_hash = _sha(payload["payload_sha256"], field=f"{field}.payload_sha256")
    payload_hash = compute_payload_sha256(payload)
    if stored_hash != payload_hash:
        raise OperationsEvidenceValidationError(
            f"{field}.payload_sha256 does not match canonical content"
        )
    try:
        evidence_contract.verify_authenticated_payload(
            payload,
            domain=domain,
            anchor=anchor,
            challenge=challenge,
            field=field,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise OperationsEvidenceValidationError(str(exc)) from exc
    return {
        "captured_at": captured_at,
        "window_start": start,
        "window_end": end,
        "payload_sha256": payload_hash,
        "replica_ids": replica_ids,
    }


def _validate_dashboard(
    payload: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    policy_details: Mapping[str, object],
) -> dict[str, object]:
    evidence = _mapping(payload["evidence"], field="dashboard_render.evidence")
    _exact_keys(
        evidence,
        {
            "dashboard_id",
            "title",
            "editable",
            "release_filters",
            "panel_evidence",
            "artifact",
        },
        field="dashboard_render.evidence",
    )
    if (
        evidence["dashboard_id"] != policy["dashboard_id"]
        or evidence["title"] != policy["title"]
        or evidence["editable"] is not policy["editable"]
        or evidence["release_filters"] != policy_details["release_filters"]
    ):
        raise OperationsEvidenceValidationError(
            "dashboard render metadata differs from the canonical operations policy"
        )
    panels = list(policy_details["panels"])  # type: ignore[arg-type]
    raw_panel_evidence = _list(
        evidence["panel_evidence"], field="dashboard_render.evidence.panel_evidence"
    )
    if len(raw_panel_evidence) != len(panels):
        raise OperationsEvidenceValidationError(
            "dashboard render panel coverage is incomplete"
        )
    observed_ids: list[str] = []
    for index, raw_panel in enumerate(raw_panel_evidence):
        field = f"dashboard_render.evidence.panel_evidence[{index}]"
        item = _mapping(raw_panel, field=field)
        _exact_keys(
            item,
            {
                "panel_id",
                "query_contract_sha256",
                "status",
                "sample_count",
            },
            field=field,
        )
        expected_panel = panels[index]
        panel_id = _text(item["panel_id"], field=f"{field}.panel_id")
        if (
            panel_id != expected_panel["id"]
            or _sha(
                item["query_contract_sha256"],
                field=f"{field}.query_contract_sha256",
            )
            != _query_contract_sha256(expected_panel)
            or item["status"] != "rendered"
        ):
            raise OperationsEvidenceValidationError(
                "dashboard render panel coverage or query contract differs"
            )
        _positive_int(
            item["sample_count"],
            field=f"{field}.sample_count",
            maximum=MAX_PANEL_SAMPLE_COUNT,
        )
        observed_ids.append(panel_id)
    if observed_ids != list(policy_details["panel_by_id"]):
        raise OperationsEvidenceValidationError(
            "dashboard render panel IDs are not the exact canonical ordered set"
        )
    artifact = _mapping(
        evidence["artifact"], field="dashboard_render.evidence.artifact"
    )
    _exact_keys(
        artifact,
        {"media_type", "byte_length", "sha256"},
        field="dashboard_render.evidence.artifact",
    )
    if artifact["media_type"] != "image/png":
        raise OperationsEvidenceValidationError(
            "dashboard render artifact must be image/png"
        )
    artifact_bytes = _positive_int(
        artifact["byte_length"],
        field="dashboard_render.evidence.artifact.byte_length",
        maximum=MAX_RENDER_ARTIFACT_BYTES,
    )
    return {
        "artifact_sha256": _sha(
            artifact["sha256"], field="dashboard_render.evidence.artifact.sha256"
        ),
        "artifact_bytes": artifact_bytes,
        "panel_count": len(observed_ids),
    }


def _validate_log_query(
    payload: Mapping[str, object],
    *,
    policy_details: Mapping[str, object],
    expected_commit_sha: str,
    expected_image_digest: str,
    window_start: datetime,
    window_end: datetime,
    declared_replica_ids: Sequence[str],
) -> dict[str, object]:
    log_panel = next(
        panel
        for panel in policy_details["panels"]  # type: ignore[union-attr]
        if panel["source"] == "logs"
    )
    query = _mapping(log_panel["query_contract"], field="log policy query contract")
    evidence = _mapping(payload["evidence"], field="structured_log_query.evidence")
    _exact_keys(
        evidence,
        {
            "panel_id",
            "query_contract_sha256",
            "required_fields",
            "filter_fields",
            "private_payload_allowed",
            "response_sha256",
            "samples",
        },
        field="structured_log_query.evidence",
    )
    if (
        evidence["panel_id"] != log_panel["id"]
        or _sha(
            evidence["query_contract_sha256"],
            field="structured_log_query.evidence.query_contract_sha256",
        )
        != _query_contract_sha256(log_panel)
        or evidence["required_fields"] != query["required_fields"]
        or evidence["filter_fields"] != query["filter_fields"]
        or evidence["private_payload_allowed"] is not False
    ):
        raise OperationsEvidenceValidationError(
            "structured-log evidence differs from the canonical query contract"
        )
    response_sha256 = _sha(
        evidence["response_sha256"],
        field="structured_log_query.evidence.response_sha256",
    )
    samples = _list(
        evidence["samples"], field="structured_log_query.evidence.samples"
    )
    if not 1 <= len(samples) <= MAX_LOG_SAMPLES:
        raise OperationsEvidenceValidationError(
            "structured-log evidence must contain between 1 and 100 samples"
        )
    required_fields = set(query["required_fields"])  # type: ignore[arg-type]
    trace_ids: set[str] = set()
    span_ids: set[str] = set()
    for index, raw_sample in enumerate(samples):
        field = f"structured_log_query.evidence.samples[{index}]"
        sample = _mapping(raw_sample, field=field)
        _exact_keys(sample, required_fields, field=field)
        timestamp = _timestamp(sample["timestamp"], field=f"{field}.timestamp")
        if not window_start <= timestamp <= window_end:
            raise OperationsEvidenceValidationError(
                "structured-log sample timestamp is outside the query window"
            )
        for name in ("service", "event", "correlation_id", "replica_id"):
            _text(sample[name], field=f"{field}.{name}", maximum_length=256)
        if not REPLICA_ID_RE.fullmatch(str(sample["replica_id"])):
            raise OperationsEvidenceValidationError(
                "structured-log replica_id is invalid"
            )
        if sample["replica_id"] not in declared_replica_ids:
            raise OperationsEvidenceValidationError(
                "structured-log sample is outside the declared replica set"
            )
        trace_id = _text(
            sample["trace_id"], field=f"{field}.trace_id", maximum_length=32
        )
        span_id = _text(
            sample["span_id"], field=f"{field}.span_id", maximum_length=16
        )
        if (
            not TRACE_ID_RE.fullmatch(trace_id)
            or trace_id == "0" * 32
            or not SPAN_ID_RE.fullmatch(span_id)
            or span_id == "0" * 16
        ):
            raise OperationsEvidenceValidationError(
                "structured-log trace or span identity is invalid"
            )
        if (
            sample["release_commit_sha"] != expected_commit_sha
            or sample["release_image_digest"] != expected_image_digest
        ):
            raise OperationsEvidenceValidationError(
                "structured-log sample belongs to a different release"
            )
        trace_ids.add(trace_id)
        span_ids.add(span_id)
    if len(trace_ids) != 1:
        raise OperationsEvidenceValidationError(
            "structured-log samples must project one trace"
        )
    return {
        "response_sha256": response_sha256,
        "sample_count": len(samples),
        "trace_id": next(iter(trace_ids)),
        "span_ids": span_ids,
    }


def _validate_trace_query(
    payload: Mapping[str, object],
    *,
    policy_details: Mapping[str, object],
    expected_commit_sha: str,
    expected_image_digest: str,
    window_start: datetime,
    window_end: datetime,
    declared_replica_ids: Sequence[str],
) -> dict[str, object]:
    trace_panel = next(
        panel
        for panel in policy_details["panels"]  # type: ignore[union-attr]
        if panel["source"] == "traces"
    )
    query = _mapping(
        trace_panel["query_contract"], field="trace policy query contract"
    )
    evidence = _mapping(
        payload["evidence"], field="distributed_trace_query.evidence"
    )
    _exact_keys(
        evidence,
        {
            "panel_id",
            "query_contract_sha256",
            "propagation_format",
            "same_trace_id",
            "release_attributes_present",
            "response_sha256",
            "spans",
        },
        field="distributed_trace_query.evidence",
    )
    if (
        evidence["panel_id"] != trace_panel["id"]
        or _sha(
            evidence["query_contract_sha256"],
            field="distributed_trace_query.evidence.query_contract_sha256",
        )
        != _query_contract_sha256(trace_panel)
        or evidence["propagation_format"] != query["propagation_format"]
        or evidence["same_trace_id"] is not True
        or evidence["release_attributes_present"] is not True
    ):
        raise OperationsEvidenceValidationError(
            "distributed-trace evidence differs from the canonical query contract"
        )
    response_sha256 = _sha(
        evidence["response_sha256"],
        field="distributed_trace_query.evidence.response_sha256",
    )
    spans = _list(evidence["spans"], field="distributed_trace_query.evidence.spans")
    required_boundaries = list(query["required_boundaries"])  # type: ignore[arg-type]
    if len(spans) != len(required_boundaries):
        raise OperationsEvidenceValidationError(
            "distributed trace does not contain the exact required boundary count"
        )
    span_by_id: dict[str, Mapping[str, object]] = {}
    parents: dict[str, str | None] = {}
    ordered_span_ids: list[str] = []
    boundaries: list[str] = []
    trace_ids: set[str] = set()
    started_sequence: list[datetime] = []
    for index, raw_span in enumerate(spans):
        field = f"distributed_trace_query.evidence.spans[{index}]"
        span = _mapping(raw_span, field=field)
        _exact_keys(
            span,
            {
                "boundary",
                "trace_id",
                "span_id",
                "parent_span_id",
                "release_commit_sha",
                "release_image_digest",
                "replica_id",
                "started_at",
                "ended_at",
            },
            field=field,
        )
        boundary = _text(span["boundary"], field=f"{field}.boundary")
        trace_id = _text(
            span["trace_id"], field=f"{field}.trace_id", maximum_length=32
        )
        span_id = _text(
            span["span_id"], field=f"{field}.span_id", maximum_length=16
        )
        if (
            not TRACE_ID_RE.fullmatch(trace_id)
            or trace_id == "0" * 32
            or not SPAN_ID_RE.fullmatch(span_id)
            or span_id == "0" * 16
            or span_id in span_by_id
        ):
            raise OperationsEvidenceValidationError(
                "distributed trace contains an invalid or duplicate trace/span identity"
            )
        parent_raw = span["parent_span_id"]
        if parent_raw is None:
            parent_id = None
        else:
            parent_id = _text(
                parent_raw, field=f"{field}.parent_span_id", maximum_length=16
            )
            if (
                not SPAN_ID_RE.fullmatch(parent_id)
                or parent_id == "0" * 16
                or parent_id == span_id
            ):
                raise OperationsEvidenceValidationError(
                    "distributed trace parent_span_id is invalid"
                )
        if (
            span["release_commit_sha"] != expected_commit_sha
            or span["release_image_digest"] != expected_image_digest
        ):
            raise OperationsEvidenceValidationError(
                "distributed trace span belongs to a different release"
            )
        replica_id = _text(
            span["replica_id"], field=f"{field}.replica_id", maximum_length=128
        )
        if not REPLICA_ID_RE.fullmatch(replica_id):
            raise OperationsEvidenceValidationError(
                "distributed trace replica_id is invalid"
            )
        if replica_id not in declared_replica_ids:
            raise OperationsEvidenceValidationError(
                "distributed trace span is outside the declared replica set"
            )
        started_at = _timestamp(span["started_at"], field=f"{field}.started_at")
        ended_at = _timestamp(span["ended_at"], field=f"{field}.ended_at")
        if (
            started_at > ended_at
            or started_at < window_start
            or ended_at > window_end
        ):
            raise OperationsEvidenceValidationError(
                "distributed trace span times are unordered or outside the query window"
            )
        span_by_id[span_id] = span
        parents[span_id] = parent_id
        ordered_span_ids.append(span_id)
        boundaries.append(boundary)
        trace_ids.add(trace_id)
        started_sequence.append(started_at)
    if boundaries != required_boundaries:
        raise OperationsEvidenceValidationError(
            "distributed trace boundary order and coverage are not exact"
        )
    if len(trace_ids) != 1:
        raise OperationsEvidenceValidationError(
            "distributed trace spans do not share one trace ID"
        )
    if started_sequence != sorted(started_sequence):
        raise OperationsEvidenceValidationError(
            "distributed trace spans are not ordered by start time"
        )
    roots = [span_id for span_id, parent_id in parents.items() if parent_id is None]
    if len(roots) != 1:
        raise OperationsEvidenceValidationError(
            "distributed trace must contain exactly one root span"
        )
    root = roots[0]
    if span_by_id[root]["boundary"] != required_boundaries[0]:
        raise OperationsEvidenceValidationError(
            "distributed trace root must be the first canonical customer boundary"
        )
    for span_id, parent_id in parents.items():
        if parent_id is not None and parent_id not in span_by_id:
            raise OperationsEvidenceValidationError(
                "distributed trace references an unknown parent span"
            )
        seen: set[str] = set()
        current = span_id
        while current != root:
            if current in seen:
                raise OperationsEvidenceValidationError(
                    "distributed trace parent graph contains a cycle"
                )
            seen.add(current)
            parent = parents.get(current)
            if parent is None:
                raise OperationsEvidenceValidationError(
                    "distributed trace span is not reachable from the single root"
                )
            current = parent
        if parent_id is not None:
            parent_started = _timestamp(
                span_by_id[parent_id]["started_at"],
                field="distributed trace parent started_at",
            )
            child_started = _timestamp(
                span_by_id[span_id]["started_at"],
                field="distributed trace child started_at",
            )
            if child_started < parent_started:
                raise OperationsEvidenceValidationError(
                    "distributed trace child starts before its parent"
                )
    for index, span_id in enumerate(ordered_span_ids):
        expected_parent = None if index == 0 else ordered_span_ids[index - 1]
        if parents[span_id] != expected_parent:
            raise OperationsEvidenceValidationError(
                "distributed trace does not follow the canonical "
                "API-to-worker-to-provider parent chain"
            )
    return {
        "response_sha256": response_sha256,
        "span_count": len(spans),
        "trace_id": next(iter(trace_ids)),
        "span_ids": set(span_by_id),
    }


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
    """Verify the three independently authenticated live operations receipts."""

    commit_sha, image_digest = _normalize_release(
        release_commit_sha, release_image_digest
    )
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    policy, policy_raw = _load_strict_json(
        OPERATIONS_POLICY_PATH,
        field="canonical flagship operations policy",
        maximum_bytes=MAX_POLICY_BYTES,
    )
    policy_details = _validate_policy(policy)
    policy_sha256 = sha256_bytes(policy_raw)
    try:
        anchor, challenge = evidence_contract.load_evidence_challenge(
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            now=checked_at,
        )
    except evidence_contract.EvidenceContractError as exc:
        raise OperationsEvidenceValidationError(str(exc)) from exc
    if challenge.policy_hashes.get("flagship_operations_sha256") != policy_sha256:
        raise OperationsEvidenceValidationError(
            "active challenge does not bind the canonical flagship operations policy"
        )

    receipt_paths = {
        "dashboard_render_receipt": Path(dashboard_render_receipt_path),
        "structured_log_query_receipt": Path(structured_log_query_receipt_path),
        "distributed_trace_query_receipt": Path(
            distributed_trace_query_receipt_path
        ),
    }
    payloads: dict[str, dict[str, object]] = {}
    raw_receipts: dict[str, bytes] = {}
    for name in SHARED_INPUT_NAMES:
        payload, raw = _load_strict_json(
            receipt_paths[name],
            field=name.replace("_", " "),
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        payloads[name] = payload
        raw_receipts[name] = raw
    shared_input_hashes = {
        name: sha256_bytes(raw_receipts[name]) for name in SHARED_INPUT_NAMES
    }
    if (
        expected_input_hashes is not None
        and shared_input_hashes != dict(expected_input_hashes)
    ):
        raise OperationsEvidenceValidationError(
            "flagship operations shared launch input hash set differs"
        )

    common: dict[str, dict[str, object]] = {}
    for name, schema, producer, domain in (
        (
            "dashboard_render_receipt",
            DASHBOARD_RENDER_SCHEMA,
            DASHBOARD_RENDER_PRODUCER,
            evidence_contract.DASHBOARD_RENDER_DOMAIN,
        ),
        (
            "structured_log_query_receipt",
            STRUCTURED_LOG_QUERY_SCHEMA,
            STRUCTURED_LOG_QUERY_PRODUCER,
            evidence_contract.STRUCTURED_LOG_QUERY_DOMAIN,
        ),
        (
            "distributed_trace_query_receipt",
            DISTRIBUTED_TRACE_QUERY_SCHEMA,
            DISTRIBUTED_TRACE_QUERY_PRODUCER,
            evidence_contract.DISTRIBUTED_TRACE_QUERY_DOMAIN,
        ),
    ):
        common[name] = _validate_common_receipt(
            payloads[name],
            field=name.removesuffix("_receipt"),
            schema=schema,
            producer=producer,
            domain=domain,
            expected_commit_sha=commit_sha,
            expected_image_digest=image_digest,
            policy_sha256=policy_sha256,
            maximum_age_seconds=int(policy_details["max_age_seconds"]),
            anchor=anchor,
            challenge=challenge,
            now=checked_at,
        )
    windows = {
        (
            value["window_start"],
            value["window_end"],
        )
        for value in common.values()
    }
    if len(windows) != 1:
        raise OperationsEvidenceValidationError(
            "flagship operations receipts do not share the exact query window"
        )
    replica_sets = {
        tuple(value["replica_ids"])  # type: ignore[arg-type]
        for value in common.values()
    }
    if len(replica_sets) != 1:
        raise OperationsEvidenceValidationError(
            "flagship operations receipts do not share the exact replica list"
        )
    replica_ids = list(next(iter(replica_sets)))
    window_start, window_end = next(iter(windows))
    assert isinstance(window_start, datetime)
    assert isinstance(window_end, datetime)

    dashboard = _validate_dashboard(
        payloads["dashboard_render_receipt"],
        policy=policy,
        policy_details=policy_details,
    )
    logs = _validate_log_query(
        payloads["structured_log_query_receipt"],
        policy_details=policy_details,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        window_start=window_start,
        window_end=window_end,
        declared_replica_ids=replica_ids,
    )
    traces = _validate_trace_query(
        payloads["distributed_trace_query_receipt"],
        policy_details=policy_details,
        expected_commit_sha=commit_sha,
        expected_image_digest=image_digest,
        window_start=window_start,
        window_end=window_end,
        declared_replica_ids=replica_ids,
    )
    if (
        logs["trace_id"] != traces["trace_id"]
        or not set(logs["span_ids"]) & set(traces["span_ids"])  # type: ignore[arg-type]
    ):
        raise OperationsEvidenceValidationError(
            "structured-log samples are not linked to the distributed trace"
        )

    verification = {
        "schema_version": VERIFICATION_SCHEMA,
        "producer": VERIFICATION_PRODUCER,
        "verified_at": isoformat(checked_at),
        "release": {"commit_sha": commit_sha, "image_digest": image_digest},
        "deployment_id": challenge.deployment_id,
        "challenge_sha256": challenge.artifact_sha256,
        "policy_sha256": policy_sha256,
        "source_contract_status": policy["source_contract_status"],
        "replica_ids": replica_ids,
        "shared_input_hashes": shared_input_hashes,
        "status": "verified",
        "receipts": {
            "dashboard_render": {
                "file_sha256": shared_input_hashes["dashboard_render_receipt"],
                "payload_sha256": common["dashboard_render_receipt"][
                    "payload_sha256"
                ],
                **dashboard,
            },
            "structured_log_query": {
                "file_sha256": shared_input_hashes[
                    "structured_log_query_receipt"
                ],
                "payload_sha256": common["structured_log_query_receipt"][
                    "payload_sha256"
                ],
                "response_sha256": logs["response_sha256"],
                "sample_count": logs["sample_count"],
                "trace_id": logs["trace_id"],
            },
            "distributed_trace_query": {
                "file_sha256": shared_input_hashes[
                    "distributed_trace_query_receipt"
                ],
                "payload_sha256": common["distributed_trace_query_receipt"][
                    "payload_sha256"
                ],
                "response_sha256": traces["response_sha256"],
                "span_count": traces["span_count"],
                "trace_id": traces["trace_id"],
            },
        },
        "cross_receipt_links_verified": True,
    }
    return add_payload_sha256(verification)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser(
        "verify", help="verify authenticated flagship operations evidence"
    )
    verify.add_argument("--release-sha", required=True)
    verify.add_argument("--image-digest", required=True)
    verify.add_argument("--dashboard-render-receipt", type=Path, required=True)
    verify.add_argument("--structured-log-query-receipt", type=Path, required=True)
    verify.add_argument("--distributed-trace-query-receipt", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--overwrite", action="store_true")
    return parser


def atomic_write_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    overwrite: bool,
) -> None:
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
        raise OperationsEvidenceValidationError(
            "output payload is not finite JSON"
        ) from exc
    try:
        secure_file_io.atomic_write_bytes(path, encoded, overwrite=overwrite)
    except secure_file_io.OutputExistsError as exc:
        raise OperationsEvidenceValidationError(
            "output cannot be published safely: output already exists; "
            "use --overwrite to replace it"
        ) from exc
    except secure_file_io.SecureFileIOError as exc:
        raise OperationsEvidenceValidationError(
            f"output cannot be published safely: {exc}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verification = verify_operations_evidence(
            release_commit_sha=args.release_sha,
            release_image_digest=args.image_digest,
            dashboard_render_receipt_path=args.dashboard_render_receipt,
            structured_log_query_receipt_path=args.structured_log_query_receipt,
            distributed_trace_query_receipt_path=args.distributed_trace_query_receipt,
        )
        atomic_write_json(args.output, verification, overwrite=args.overwrite)
    except OperationsEvidenceValidationError as exc:
        print(f"flagship operations evidence verification failed: {exc}", file=sys.stderr)
        return 2
    print(f"flagship operations evidence verified: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
