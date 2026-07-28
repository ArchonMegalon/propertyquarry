#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from scripts.propertyquarry_rybbit_evidence import (
        verify_receipt as verify_rybbit_delivery_receipt,
    )
else:
    from propertyquarry_rybbit_evidence import (
        verify_receipt as verify_rybbit_delivery_receipt,
    )


SCHEMA = "propertyquarry.launch_authority_envelope.v1"
OPERATIONS_VERIFICATION_SCHEMA = (
    "propertyquarry.flagship-operations-evidence-verification.v1"
)
OPERATIONS_VERIFICATION_PRODUCER = (
    "propertyquarry-flagship-operations-evidence-verifier"
)
OPERATIONS_SOURCE_CONTRACT_STATUS = "defined_not_live_evidence"
SECURITY_SCHEMA = "propertyquarry.release_security_receipt.v1"
SECURITY_BINDING_CONTRACT = "propertyquarry.workflow_runtime_binding"
OVERLAY_SCHEMA = "propertyquarry.evidence_overlay_read_model_receipt.v2"
RYBBIT_SCHEMA = "propertyquarry.rybbit_delivery_receipt.v1"
FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPLICA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]{0,19}")
MAX_JSON_INPUT_BYTES = 16 * 1024 * 1024
MAX_CONTROLLER_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_ACTIVATION_AUTHORITY_AGE_SECONDS = 15 * 60
AUTHORITY_PHASES = {"preactivation", "final"}
RELEASE_METADATA_DESCENDANT_PATHS = {
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
}


class EvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: object) -> str:
    return str(value or "").strip()


def _object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _contains_raw_base_id_key(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = str(key).replace("-", "_").casefold()
                if normalized in {"base_id", "baseid"}:
                    return True
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_https_origin(value: object) -> str:
    raw = _text(value)
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    canonical = f"https://{authority}"
    return canonical if raw == canonical else ""


def _authority_phase(value: object) -> str:
    normalized = _text(value).casefold()
    if normalized not in AUTHORITY_PHASES:
        raise EvidenceError("authority_phase_invalid")
    return normalized


def _stable_regular_snapshot(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    retain_bytes: bool,
) -> tuple[bytes | None, int, str]:
    source = path.expanduser()
    try:
        before = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        raise EvidenceError(f"{error_code}_missing") from exc
    if not stat.S_ISREG(before.st_mode):
        raise EvidenceError(f"{error_code}_not_regular")
    if before.st_size < 1 or before.st_size > max_bytes:
        raise EvidenceError(f"{error_code}_size_invalid")
    if stat.S_IMODE(before.st_mode) & 0o022:
        raise EvidenceError(f"{error_code}_writable_by_group_or_other")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise EvidenceError(f"{error_code}_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
        ):
            raise EvidenceError(f"{error_code}_identity_changed")
        chunks: list[bytes] | None = [] if retain_bytes else None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1_048_576, max_bytes + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise EvidenceError(f"{error_code}_size_invalid")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if (
        after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
        or total != after.st_size
    ):
        raise EvidenceError(f"{error_code}_changed_during_read")
    try:
        path_after = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        raise EvidenceError(f"{error_code}_changed_during_read") from exc
    if any(getattr(path_after, field) != getattr(after, field) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")) or not stat.S_ISREG(
        path_after.st_mode
    ):
        raise EvidenceError(f"{error_code}_changed_during_read")
    return (b"".join(chunks) if chunks is not None else None, total, digest.hexdigest())


def _stable_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
) -> bytes:
    raw, _size_bytes, _digest = _stable_regular_snapshot(
        path,
        max_bytes=max_bytes,
        error_code=error_code,
        retain_bytes=True,
    )
    if raw is None:  # pragma: no cover - retain_bytes=True is a local invariant
        raise EvidenceError(f"{error_code}_read_failed")
    return raw


def _strict_json_object(raw: bytes, *, error_code: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError(f"{error_code}_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise EvidenceError(f"{error_code}_nonfinite_number")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except EvidenceError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise EvidenceError(f"{error_code}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"{error_code}_root_not_object")
    return payload


def _input_identity(
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> dict[str, object]:
    return {
        "path": str(path.expanduser().absolute()),
        "file_name": path.name,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _record_check(
    checks: list[dict[str, object]],
    failures: list[str],
    *,
    name: str,
    ok: bool,
    failure: str,
) -> None:
    checks.append({"name": name, "ok": ok})
    if not ok:
        failures.append(failure)


def _candidate_binding(payload: object, expected: str, fields: tuple[str, ...]) -> bool:
    row = _object(payload)
    values = [_text(row.get(field)).lower() for field in fields if _text(row.get(field))]
    return bool(values) and all(FULL_GIT_SHA.fullmatch(value) and value == expected for value in values)


def _all_pass_checks(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(row, dict) and row.get("ok") is True for row in value)


def _pass_area_map(value: object) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(value, list):
        return {}, False
    result: dict[str, dict[str, Any]] = {}
    for raw_row in value:
        if not isinstance(raw_row, dict):
            return {}, False
        row = dict(raw_row)
        area = _text(row.get("area"))
        if not area or area in result:
            return {}, False
        result[area] = row
    return result, True


def _same_input_path(recorded: object, actual: Path) -> bool:
    text = _text(recorded)
    if not text:
        return False
    try:
        return Path(text).expanduser().resolve(strict=False) == actual.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


def _operations_payload_sha256(payload: dict[str, Any]) -> str:
    unhashed = dict(payload)
    unhashed.pop("payload_sha256", None)
    try:
        canonical = json.dumps(
            unhashed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return _sha256(canonical)


def _configured_replica_ids(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if any(
        not isinstance(replica_id, str)
        or REPLICA_ID.fullmatch(replica_id) is None
        or replica_id == "UNCONFIGURED"
        for replica_id in value
    ):
        return False
    return value == sorted(set(value))


def _operations_evidence_bindings_ok(
    observability: dict[str, Any],
    *,
    dashboard_render_receipt_sha256: str,
    structured_log_query_receipt_sha256: str,
    distributed_trace_query_receipt_sha256: str,
) -> bool:
    operations = _object(observability.get("operations_evidence"))
    policy_hashes = _object(observability.get("policy_hashes"))
    observability_input_hashes = _object(observability.get("shared_input_hashes"))
    operations_input_hashes = _object(operations.get("shared_input_hashes"))
    operations_receipts = _object(operations.get("receipts"))
    operations_release = _object(operations.get("release"))
    observability_release = _object(observability.get("release"))
    operations_replica_ids = operations.get("replica_ids")
    expected_inputs = {
        "dashboard_render_receipt": (
            "dashboard_render",
            dashboard_render_receipt_sha256,
        ),
        "structured_log_query_receipt": (
            "structured_log_query",
            structured_log_query_receipt_sha256,
        ),
        "distributed_trace_query_receipt": (
            "distributed_trace_query",
            distributed_trace_query_receipt_sha256,
        ),
    }
    expected_policy_sha256 = _text(
        policy_hashes.get("flagship_operations_sha256")
    )
    operations_challenge_sha256 = _text(operations.get("challenge_sha256"))
    operations_payload_sha256 = _text(operations.get("payload_sha256"))
    if (
        operations.get("schema_version") != OPERATIONS_VERIFICATION_SCHEMA
        or operations.get("producer") != OPERATIONS_VERIFICATION_PRODUCER
        or operations.get("source_contract_status")
        != OPERATIONS_SOURCE_CONTRACT_STATUS
        or operations.get("status") != "verified"
        or operations.get("cross_receipt_links_verified") is not True
        or not _configured_replica_ids(operations_replica_ids)
        or operations_replica_ids != observability.get("replica_ids")
        or not operations_release
        or operations_release != observability_release
        or not _text(operations.get("deployment_id"))
        or operations.get("deployment_id") != observability.get("deployment_id")
        or SHA256.fullmatch(operations_challenge_sha256) is None
        or operations.get("challenge_sha256")
        != observability.get("challenge_sha256")
        or SHA256.fullmatch(expected_policy_sha256) is None
        or _text(operations.get("policy_sha256")) != expected_policy_sha256
        or SHA256.fullmatch(operations_payload_sha256) is None
        or operations_payload_sha256 != _operations_payload_sha256(operations)
        or operations_input_hashes
        != {
            input_name: expected_sha256
            for input_name, (
                _receipt_name,
                expected_sha256,
            ) in expected_inputs.items()
        }
    ):
        return False
    for input_name, (
        receipt_name,
        expected_sha256,
    ) in expected_inputs.items():
        receipt = _object(operations_receipts.get(receipt_name))
        if (
            SHA256.fullmatch(expected_sha256) is None
            or _text(observability_input_hashes.get(input_name)) != expected_sha256
            or _text(receipt.get("file_sha256")) != expected_sha256
        ):
            return False
    return True


def _release_hygiene_binding_ok(
    payload: dict[str, Any],
    *,
    candidate_sha: str,
    workflow_head_sha: str,
) -> bool:
    if (
        payload.get("status") != "pass"
        or _text(payload.get("manifest_runtime_commit")).lower() != candidate_sha
        or _text(payload.get("head_commit")).lower() != workflow_head_sha
    ):
        return False
    ancestry_fields = {
        "parent_commit",
        "manifest_descendant_paths",
        "manifest_metadata_only_ancestor",
    }
    projected_fields = ancestry_fields.intersection(payload)
    if projected_fields != ancestry_fields:
        return False
    parent_commit = _text(payload.get("parent_commit")).lower()
    raw_descendant_paths = payload.get("manifest_descendant_paths")
    if not isinstance(raw_descendant_paths, list) or any(
        not isinstance(path, str) or not path for path in raw_descendant_paths
    ):
        return False
    descendant_paths = [str(path) for path in raw_descendant_paths]
    metadata_only_ancestor = payload.get("manifest_metadata_only_ancestor")
    if (
        (parent_commit and FULL_GIT_SHA.fullmatch(parent_commit) is None)
        or len(descendant_paths) != len(set(descendant_paths))
        or any(path not in RELEASE_METADATA_DESCENDANT_PATHS for path in descendant_paths)
        or type(metadata_only_ancestor) is not bool
    ):
        return False
    if candidate_sha == workflow_head_sha:
        return not descendant_paths and metadata_only_ancestor is False
    if parent_commit == candidate_sha:
        return not descendant_paths and metadata_only_ancestor is False
    return bool(descendant_paths) and metadata_only_ancestor is True


def _validate_gold(
    gold: dict[str, Any],
    *,
    candidate_sha: str,
    workflow_head_sha: str,
    activation_path: Path,
    overlay_path: Path,
    overlay_snapshot_id: str,
    rybbit_path: Path,
    dashboard_render_receipt_sha256: str,
    structured_log_query_receipt_sha256: str,
    distributed_trace_query_receipt_sha256: str,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_overlay_phase: str,
    checks: list[dict[str, object]],
    failures: list[str],
) -> None:
    customer_ux = _object(gold.get("flagship_customer_ux_evidence"))
    product_data = _object(gold.get("launch_product_data_evidence"))
    canonical = _object(gold.get("canonical_launch_evidence"))
    canonical_slo = _object(canonical.get("slo"))
    canonical_observability = _object(canonical.get("observability"))
    top_activation = _object(gold.get("activation_to_value"))
    top_overlay = _object(product_data.get("evidence_overlay_read_model"))
    gold_overlay_source_evidence = _object(top_overlay.get("source_evidence"))
    gold_overlay_source_authority = _object(top_overlay.get("source_authority"))
    top_rybbit = _object(product_data.get("rybbit_delivery"))
    top_release_hygiene = _object(gold.get("release_hygiene"))
    top_slo = _object(gold.get("slo_evidence"))
    pass_areas, unique_areas = _pass_area_map(gold.get("pass_areas"))

    top_level_ok = (
        gold.get("status") == "pass"
        and gold.get("readiness_profile") == "launch"
        and gold.get("ready_for_notification") is True
        and gold.get("blockers") == []
        and gold.get("next_required_actions") == []
    )
    _record_check(
        checks,
        failures,
        name="gold_launch_top_level_pass",
        ok=top_level_ok,
        failure="gold_launch_top_level_not_pass",
    )
    customer_ux_ok = customer_ux.get("required") is True and customer_ux.get("ready") is True and customer_ux.get("missing_receipts") == []
    _record_check(
        checks,
        failures,
        name="gold_customer_ux_ready",
        ok=customer_ux_ok,
        failure="gold_customer_ux_not_ready",
    )
    product_data_ok = (
        product_data.get("required") is True
        and product_data.get("ready") is True
        and top_overlay.get("status") == "pass"
        and top_rybbit.get("status") == "pass"
        and _candidate_binding(top_overlay, candidate_sha, ("candidate_sha",))
        and _candidate_binding(top_rybbit, candidate_sha, ("candidate_sha",))
        and gold_overlay_source_evidence.get("base_origin") == expected_teable_origin
        and gold_overlay_source_evidence.get("base_id_sha256") == expected_teable_base_id_sha256
        and not _contains_raw_base_id_key(top_overlay)
        and gold_overlay_source_authority.get("bound_independently") is True
        and gold_overlay_source_authority.get("expected_origin") == expected_teable_origin
        and gold_overlay_source_authority.get("expected_base_id_sha256") == expected_teable_base_id_sha256
        and top_overlay.get("activation_phase") == expected_overlay_phase
    )
    _record_check(
        checks,
        failures,
        name="gold_product_data_ready",
        ok=product_data_ok,
        failure="gold_product_data_not_ready",
    )
    operations_evidence_ok = _operations_evidence_bindings_ok(
        canonical_observability,
        dashboard_render_receipt_sha256=dashboard_render_receipt_sha256,
        structured_log_query_receipt_sha256=structured_log_query_receipt_sha256,
        distributed_trace_query_receipt_sha256=distributed_trace_query_receipt_sha256,
    )
    _record_check(
        checks,
        failures,
        name="gold_operations_evidence_exact_inputs",
        ok=operations_evidence_ok,
        failure="gold_operations_evidence_input_mismatch",
    )
    canonical_ok = (
        canonical.get("required") is True
        and canonical.get("status") == "pass"
        and canonical.get("validation_errors") == []
        and canonical_slo.get("status") == "pass"
        and canonical_observability.get("status") == "verified"
        and canonical_observability.get("cross_receipt_links_verified") is True
        and operations_evidence_ok
    )
    _record_check(
        checks,
        failures,
        name="gold_canonical_launch_pass",
        ok=canonical_ok,
        failure="gold_canonical_launch_not_pass",
    )
    activation_summary_ok = (
        top_activation.get("status") == "pass" and top_activation.get("flagship_proof_ok") is True and top_activation.get("proof_mode") == "deployed_playwright"
    )
    _record_check(
        checks,
        failures,
        name="gold_activation_deployed",
        ok=activation_summary_ok,
        failure="gold_activation_not_deployed",
    )

    required_areas = {
        "release_hygiene",
        "slo_evidence",
        "evidence_overlay_read_model",
        "rybbit_delivery",
        "activation_to_value",
        "canonical_launch_evidence",
    }
    areas_present = unique_areas and required_areas.issubset(pass_areas)
    _record_check(
        checks,
        failures,
        name="gold_pass_areas_unique_and_complete",
        ok=areas_present,
        failure="gold_pass_areas_incomplete_or_duplicate",
    )
    if not areas_present:
        return

    release_area = pass_areas["release_hygiene"]
    slo_area = pass_areas["slo_evidence"]
    overlay_area = pass_areas["evidence_overlay_read_model"]
    rybbit_area = pass_areas["rybbit_delivery"]
    activation_area = pass_areas["activation_to_value"]
    canonical_area = pass_areas["canonical_launch_evidence"]
    area_candidate_ok = (
        _release_hygiene_binding_ok(
            release_area,
            candidate_sha=candidate_sha,
            workflow_head_sha=workflow_head_sha,
        )
        and _release_hygiene_binding_ok(
            top_release_hygiene,
            candidate_sha=candidate_sha,
            workflow_head_sha=workflow_head_sha,
        )
        and slo_area.get("status") == "pass"
        and _candidate_binding(slo_area, candidate_sha, ("release_commit_sha",))
        and overlay_area.get("status") == "pass"
        and _candidate_binding(overlay_area, candidate_sha, ("candidate_sha",))
        and rybbit_area.get("status") == "pass"
        and _candidate_binding(rybbit_area, candidate_sha, ("candidate_sha",))
        and activation_area.get("status") == "pass"
        and _candidate_binding(
            activation_area,
            candidate_sha,
            ("candidate_sha", "release_commit_sha"),
        )
        and canonical_area.get("status") == "pass"
        and _candidate_binding(top_slo, candidate_sha, ("release_commit_sha",))
    )
    _record_check(
        checks,
        failures,
        name="gold_pass_areas_candidate_bound",
        ok=area_candidate_ok,
        failure="gold_pass_area_candidate_mismatch",
    )
    overlay_snapshot_ok = (
        SHA256.fullmatch(overlay_snapshot_id) is not None
        and _text(overlay_area.get("snapshot_id")) == overlay_snapshot_id
        and _text(top_overlay.get("snapshot_id")) == overlay_snapshot_id
    )
    _record_check(
        checks,
        failures,
        name="gold_overlay_active_snapshot_bound",
        ok=overlay_snapshot_ok,
        failure="gold_overlay_snapshot_mismatch",
    )
    exact_paths_ok = (
        _same_input_path(activation_area.get("receipt_path"), activation_path)
        and _same_input_path(overlay_area.get("receipt_path"), overlay_path)
        and _same_input_path(rybbit_area.get("receipt_path"), rybbit_path)
    )
    _record_check(
        checks,
        failures,
        name="gold_pass_areas_reference_exact_inputs",
        ok=exact_paths_ok,
        failure="gold_pass_area_input_path_mismatch",
    )


def _validate_activation(payload: dict[str, Any], candidate_sha: str) -> bool:
    live_contract = _object(payload.get("live_contract"))
    required_checks = {
        "protected_live_configuration",
        "idempotent_run_reservation",
        "activation_step_matrix_complete",
        "safe_cleanup_complete",
    }
    checks = payload.get("checks")
    check_names = {_text(row.get("name")) for row in checks if isinstance(row, dict)} if isinstance(checks, list) else set()
    return (
        payload.get("status") == "pass"
        and type(payload.get("failed_count")) is int
        and payload.get("failed_count") == 0
        and _candidate_binding(payload, candidate_sha, ("candidate_sha", "release_commit_sha"))
        and payload.get("proof_mode") == "deployed_playwright"
        and bool(_text(payload.get("run_key")))
        and live_contract.get("deployed_playwright_runner") is True
        and live_contract.get("local_execution_forbidden") is True
        and live_contract.get("provider_response_mocking_forbidden") is True
        and live_contract.get("principal_headers_forbidden") is True
        and live_contract.get("session_injection_forbidden") is True
        and _all_pass_checks(checks)
        and required_checks.issubset(check_names)
        and _all_pass_checks(payload.get("steps"))
    )


def _validate_overlay(
    payload: dict[str, Any],
    candidate_sha: str,
    *,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_phase: str,
) -> bool:
    snapshot_id = _text(payload.get("snapshot_id"))
    activation = _object(payload.get("activation"))
    read_model = _object(payload.get("read_model"))
    source_evidence = _object(payload.get("source_evidence"))
    source_authority = _object(payload.get("source_authority"))
    raw_query_budget_ms = read_model.get("query_budget_ms")
    if type(raw_query_budget_ms) in {int, float}:
        query_budget_ms = float(raw_query_budget_ms)
    else:
        query_budget_ms = math.nan
    common_valid = (
        payload.get("schema") == OVERLAY_SCHEMA
        and payload.get("status") == "pass"
        and payload.get("failures") == []
        and _candidate_binding(payload, candidate_sha, ("candidate_sha",))
        and SHA256.fullmatch(snapshot_id) is not None
        and _text(activation.get("candidate_snapshot_id")) == snapshot_id
        and activation.get("candidate_staged") is True
        and activation.get("active_pointer_switch") == "atomic_final_transaction"
        and math.isfinite(query_budget_ms)
        and query_budget_ms > 0
        and source_evidence.get("base_origin") == expected_teable_origin
        and source_evidence.get("base_id_sha256") == expected_teable_base_id_sha256
        and not _contains_raw_base_id_key(payload)
        and source_authority.get("bound_independently") is True
        and source_authority.get("expected_origin") == expected_teable_origin
        and source_authority.get("expected_base_id_sha256") == expected_teable_base_id_sha256
    )
    if not common_valid:
        return False
    if expected_phase == "staged":
        raw_query_p95_ms = read_model.get("query_p95_ms")
        query_p95_ms = (
            float(raw_query_p95_ms)
            if type(raw_query_p95_ms) in {int, float}
            else math.inf
        )
        return (
            activation.get("phase") == "staged"
            and not _text(activation.get("activated_snapshot_id"))
            and activation.get("activation_performed") is False
            and activation.get("active_snapshot_unchanged") is True
            and activation.get("active_revalidation_performed") is False
            and activation.get("active_revalidation_query_sample_count") == 0
            and read_model.get("sample_layer_count") == 8
            and read_model.get("query_sample_count") == 24
            and math.isfinite(query_p95_ms)
            and query_p95_ms >= 0
            and query_p95_ms <= query_budget_ms
        )
    if expected_phase != "active":
        return False
    raw_active_query_p95_ms = activation.get("active_revalidation_query_p95_ms")
    active_query_p95_ms = (
        float(raw_active_query_p95_ms)
        if type(raw_active_query_p95_ms) in {int, float}
        else math.inf
    )
    return (
        activation.get("phase") == "active"
        and _text(activation.get("activated_snapshot_id")) == snapshot_id
        and activation.get("activation_performed") is True
        and activation.get("active_snapshot_unchanged") is False
        and activation.get("active_revalidation_performed") is True
        and activation.get("active_revalidation_query_sample_count") == 24
        and math.isfinite(active_query_p95_ms)
        and active_query_p95_ms >= 0
        and active_query_p95_ms <= query_budget_ms
        and SHA256.fullmatch(_text(activation.get("activation_authority_sha256")))
        is not None
        and SHA256.fullmatch(_text(activation.get("staged_receipt_sha256")))
        is not None
    )


def _validate_rybbit(
    payload: dict[str, Any],
    candidate_sha: str,
    *,
    expected_public_origin: str,
    expected_analytics_origin: str,
    expected_site_id_sha256: str,
    now: datetime,
) -> bool:
    try:
        failures = verify_rybbit_delivery_receipt(
            payload,
            expected_candidate_sha=candidate_sha,
            expected_public_origin=expected_public_origin,
            expected_analytics_origin=expected_analytics_origin,
            expected_site_id_sha256=expected_site_id_sha256,
            max_age_minutes=15,
            now=now,
        )
    except Exception:
        return False
    return not failures


def _validate_security(payload: dict[str, Any], candidate_sha: str) -> bool:
    identities = _object(payload.get("identities"))
    summary = _object(payload.get("summary"))
    return (
        payload.get("schema") == SECURITY_SCHEMA
        and payload.get("mode") == "flagship"
        and payload.get("status") == "pass"
        and payload.get("gate_passed") is True
        and payload.get("severity_threshold") == "HIGH"
        and type(summary.get("blocking")) is int
        and summary.get("blocking") == 0
        and _candidate_binding(identities, candidate_sha, ("release_commit_sha",))
    )


def _expected_security_binding(
    *,
    candidate_sha: str,
    workflow_head_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
) -> dict[str, object]:
    return {
        "contract_name": SECURITY_BINDING_CONTRACT,
        "version": 1,
        "product": "PropertyQuarry",
        "runtime_commit_sha": candidate_sha,
        "workflow_head_sha": workflow_head_sha,
        "run_id": workflow_run_id,
        "run_attempt": workflow_run_attempt,
    }


def _validate_live(
    payload: dict[str, Any],
    *,
    candidate_sha: str,
    workflow_head_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    security_sha256: str,
    security_binding_sha256: str,
) -> bool:
    expected = _object(payload.get("expected"))
    actual = _object(payload.get("actual"))
    security = _object(payload.get("security_receipt_binding"))
    return (
        payload.get("contract_name") == "propertyquarry.live_release_provenance.v2"
        and payload.get("status") == "pass"
        and type(payload.get("failed_count")) is int
        and payload.get("failed_count") == 0
        and _all_pass_checks(payload.get("checks"))
        and expected == actual
        and _candidate_binding(expected, candidate_sha, ("release_commit_sha",))
        and security.get("verified") is True
        and _candidate_binding(security, candidate_sha, ("release_commit_sha",))
        and _text(security.get("workflow_head_sha")).lower() == workflow_head_sha
        and _text(security.get("workflow_run_id")) == workflow_run_id
        and _text(security.get("workflow_run_attempt")) == workflow_run_attempt
        and _text(security.get("receipt_sha256")).lower() == security_sha256
        and _text(security.get("workflow_binding_sha256")).lower() == security_binding_sha256
    )


def _validate_activation_authority(
    payload: dict[str, Any],
    *,
    candidate_sha: str,
    workflow_head_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_rybbit_public_origin: str,
    expected_rybbit_analytics_origin: str,
    expected_rybbit_site_id_sha256: str,
    expected_snapshot_id: str,
    expected_staged_receipt_sha256: str,
    now: datetime,
) -> bool:
    workflow = _object(payload.get("workflow"))
    teable = _object(payload.get("teable_authority"))
    rybbit = _object(payload.get("rybbit_authority"))
    scope = _object(payload.get("activation_scope"))
    overlay_input = _object(_object(payload.get("inputs")).get("overlay"))
    generated_at = _parse_timestamp(payload.get("generated_at"))
    fresh = (
        generated_at is not None
        and generated_at <= now
        and (now - generated_at).total_seconds()
        <= MAX_ACTIVATION_AUTHORITY_AGE_SECONDS
    )
    return (
        payload.get("schema") == SCHEMA
        and payload.get("status") == "pass"
        and payload.get("authority_phase") == "preactivation"
        and _candidate_binding(payload, candidate_sha, ("candidate_sha",))
        and workflow.get("head_sha") == workflow_head_sha
        and workflow.get("run_id") == workflow_run_id
        and workflow.get("run_attempt") == workflow_run_attempt
        and teable.get("origin") == expected_teable_origin
        and teable.get("base_id_sha256") == expected_teable_base_id_sha256
        and teable.get("supplied_independently") is True
        and rybbit.get("public_origin") == expected_rybbit_public_origin
        and rybbit.get("analytics_origin") == expected_rybbit_analytics_origin
        and rybbit.get("site_id_sha256") == expected_rybbit_site_id_sha256
        and rybbit.get("supplied_independently") is True
        and scope.get("snapshot_id") == expected_snapshot_id
        and scope.get("staged_overlay_receipt_sha256")
        == expected_staged_receipt_sha256
        and overlay_input.get("sha256") == expected_staged_receipt_sha256
        and payload.get("activation_authorized") is True
        and payload.get("launch_authorized") is False
        and payload.get("notification_authorized") is False
        and payload.get("failures") == []
        and _all_pass_checks(payload.get("checks"))
        and fresh
    )


def build_launch_authority_envelope(
    *,
    candidate_sha: str,
    workflow_head_sha: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_rybbit_public_origin: str,
    expected_rybbit_analytics_origin: str,
    expected_rybbit_site_id_sha256: str,
    gold_status_path: Path,
    live_provenance_path: Path,
    activation_receipt_path: Path,
    overlay_receipt_path: Path,
    rybbit_receipt_path: Path,
    dashboard_render_receipt_path: Path,
    structured_log_query_receipt_path: Path,
    distributed_trace_query_receipt_path: Path,
    security_receipt_path: Path,
    security_workflow_binding_path: Path,
    controller_bundle_path: Path,
    expected_controller_bundle_sha256: str,
    authority_phase: str = "final",
    activation_authority_path: Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    candidate = _text(candidate_sha).lower()
    head = _text(workflow_head_sha).lower()
    run_id = _text(workflow_run_id)
    run_attempt = _text(workflow_run_attempt)
    teable_origin = _text(expected_teable_origin)
    teable_base_id_sha256 = _text(expected_teable_base_id_sha256)
    rybbit_public_origin = _text(expected_rybbit_public_origin)
    rybbit_analytics_origin = _text(expected_rybbit_analytics_origin)
    rybbit_site_id_sha256 = _text(expected_rybbit_site_id_sha256)
    expected_bundle_sha = _text(expected_controller_bundle_sha256).lower()
    observed_at = generated_at or _utc_now()
    checks: list[dict[str, object]] = []
    failures: list[str] = []
    identities: dict[str, dict[str, object]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    raw_inputs: dict[str, bytes] = {}

    try:
        phase = _authority_phase(authority_phase)
    except EvidenceError:
        phase = _text(authority_phase).casefold()
        _record_check(
            checks,
            failures,
            name="authority_phase_valid",
            ok=False,
            failure="authority_phase_invalid",
        )
    else:
        _record_check(
            checks,
            failures,
            name="authority_phase_valid",
            ok=True,
            failure="authority_phase_invalid",
        )

    identity_ok = (
        FULL_GIT_SHA.fullmatch(candidate) is not None
        and FULL_GIT_SHA.fullmatch(head) is not None
        and POSITIVE_DECIMAL.fullmatch(run_id) is not None
        and POSITIVE_DECIMAL.fullmatch(run_attempt) is not None
        and SHA256.fullmatch(expected_bundle_sha) is not None
    )
    _record_check(
        checks,
        failures,
        name="expected_controller_identity_valid",
        ok=identity_ok,
        failure="expected_controller_identity_invalid",
    )
    teable_identity_ok = _canonical_https_origin(teable_origin) == teable_origin and SHA256.fullmatch(teable_base_id_sha256) is not None
    _record_check(
        checks,
        failures,
        name="expected_teable_authority_valid",
        ok=teable_identity_ok,
        failure="expected_teable_authority_invalid",
    )
    rybbit_identity_ok = (
        _canonical_https_origin(rybbit_public_origin) == rybbit_public_origin
        and _canonical_https_origin(rybbit_analytics_origin) == rybbit_analytics_origin
        and SHA256.fullmatch(rybbit_site_id_sha256) is not None
    )
    _record_check(
        checks,
        failures,
        name="expected_rybbit_authority_valid",
        ok=rybbit_identity_ok,
        failure="expected_rybbit_authority_invalid",
    )

    json_inputs = {
        "gold_status": gold_status_path,
        "live_provenance": live_provenance_path,
        "activation": activation_receipt_path,
        "overlay": overlay_receipt_path,
        "rybbit": rybbit_receipt_path,
        "dashboard_render_receipt": dashboard_render_receipt_path,
        "structured_log_query_receipt": structured_log_query_receipt_path,
        "distributed_trace_query_receipt": distributed_trace_query_receipt_path,
        "security": security_receipt_path,
        "security_workflow_binding": security_workflow_binding_path,
    }
    if phase == "final":
        if activation_authority_path is None:
            _record_check(
                checks,
                failures,
                name="activation_authority_input_required",
                ok=False,
                failure="activation_authority_missing",
            )
        else:
            json_inputs["activation_authority"] = activation_authority_path
    for name, path in json_inputs.items():
        try:
            raw = _stable_regular_bytes(
                path,
                max_bytes=MAX_JSON_INPUT_BYTES,
                error_code=name,
            )
            payload = _strict_json_object(raw, error_code=name)
        except EvidenceError as exc:
            _record_check(
                checks,
                failures,
                name=f"{name}_input_secure",
                ok=False,
                failure=exc.code,
            )
            continue
        raw_inputs[name] = raw
        payloads[name] = payload
        identities[name] = _input_identity(
            path,
            size_bytes=len(raw),
            sha256=_sha256(raw),
        )
        _record_check(
            checks,
            failures,
            name=f"{name}_input_secure",
            ok=True,
            failure=f"{name}_input_invalid",
        )

    try:
        _controller_raw, controller_size_bytes, controller_sha256 = _stable_regular_snapshot(
            controller_bundle_path,
            max_bytes=MAX_CONTROLLER_BUNDLE_BYTES,
            error_code="controller_bundle",
            retain_bytes=False,
        )
    except EvidenceError as exc:
        _record_check(
            checks,
            failures,
            name="controller_bundle_secure",
            ok=False,
            failure=exc.code,
        )
    else:
        raw_inputs["controller_bundle"] = b""
        identities["controller_bundle"] = _input_identity(
            controller_bundle_path,
            size_bytes=controller_size_bytes,
            sha256=controller_sha256,
        )
        bundle_ok = identity_ok and controller_sha256 == expected_bundle_sha
        _record_check(
            checks,
            failures,
            name="controller_bundle_secure",
            ok=bundle_ok,
            failure="controller_bundle_sha256_mismatch",
        )

    if (
        set(payloads) == set(json_inputs)
        and identity_ok
        and teable_identity_ok
        and rybbit_identity_ok
    ):
        _validate_gold(
            payloads["gold_status"],
            candidate_sha=candidate,
            workflow_head_sha=head,
            activation_path=activation_receipt_path,
            overlay_path=overlay_receipt_path,
            overlay_snapshot_id=_text(payloads["overlay"].get("snapshot_id")),
            rybbit_path=rybbit_receipt_path,
            dashboard_render_receipt_sha256=_sha256(
                raw_inputs["dashboard_render_receipt"]
            ),
            structured_log_query_receipt_sha256=_sha256(
                raw_inputs["structured_log_query_receipt"]
            ),
            distributed_trace_query_receipt_sha256=_sha256(
                raw_inputs["distributed_trace_query_receipt"]
            ),
            expected_teable_origin=teable_origin,
            expected_teable_base_id_sha256=teable_base_id_sha256,
            expected_overlay_phase="staged",
            checks=checks,
            failures=failures,
        )
        activation_ok = _validate_activation(payloads["activation"], candidate)
        _record_check(
            checks,
            failures,
            name="activation_candidate_deployed_pass",
            ok=activation_ok,
            failure="activation_receipt_not_candidate_deployed_pass",
        )
        overlay_ok = _validate_overlay(
            payloads["overlay"],
            candidate,
            expected_teable_origin=teable_origin,
            expected_teable_base_id_sha256=teable_base_id_sha256,
            expected_phase=("staged" if phase == "preactivation" else "active"),
        )
        _record_check(
            checks,
            failures,
            name="overlay_candidate_pass",
            ok=overlay_ok,
            failure="overlay_receipt_not_candidate_pass",
        )
        rybbit_ok = _validate_rybbit(
            payloads["rybbit"],
            candidate,
            expected_public_origin=rybbit_public_origin,
            expected_analytics_origin=rybbit_analytics_origin,
            expected_site_id_sha256=rybbit_site_id_sha256,
            now=observed_at,
        )
        _record_check(
            checks,
            failures,
            name="rybbit_candidate_pass",
            ok=rybbit_ok,
            failure="rybbit_receipt_not_candidate_pass",
        )
        security_ok = _validate_security(payloads["security"], candidate)
        _record_check(
            checks,
            failures,
            name="security_candidate_pass",
            ok=security_ok,
            failure="security_receipt_not_candidate_pass",
        )
        expected_binding = _expected_security_binding(
            candidate_sha=candidate,
            workflow_head_sha=head,
            workflow_run_id=run_id,
            workflow_run_attempt=run_attempt,
        )
        binding_ok = payloads["security_workflow_binding"] == expected_binding
        _record_check(
            checks,
            failures,
            name="security_workflow_current_run_bound",
            ok=binding_ok,
            failure="security_workflow_binding_mismatch",
        )
        live_ok = _validate_live(
            payloads["live_provenance"],
            candidate_sha=candidate,
            workflow_head_sha=head,
            workflow_run_id=run_id,
            workflow_run_attempt=run_attempt,
            security_sha256=_sha256(raw_inputs["security"]),
            security_binding_sha256=_sha256(raw_inputs["security_workflow_binding"]),
        )
        _record_check(
            checks,
            failures,
            name="live_provenance_current_run_pass",
            ok=live_ok,
            failure="live_provenance_not_current_run_pass",
        )

        if (
            phase == "final"
            and "activation_authority" in payloads
            and "activation_authority" in raw_inputs
        ):
            overlay_activation = _object(payloads["overlay"].get("activation"))
            authorized_workflow = _object(overlay_activation.get("authorized_workflow"))
            staged_overlay_receipt_sha256 = _text(
                overlay_activation.get("staged_receipt_sha256")
            ).lower()
            activation_authority_sha256 = _sha256(raw_inputs["activation_authority"])
            activation_authority_ok = (
                _validate_activation_authority(
                    payloads["activation_authority"],
                    candidate_sha=candidate,
                    workflow_head_sha=head,
                    workflow_run_id=run_id,
                    workflow_run_attempt=run_attempt,
                    expected_teable_origin=teable_origin,
                    expected_teable_base_id_sha256=teable_base_id_sha256,
                    expected_rybbit_public_origin=rybbit_public_origin,
                    expected_rybbit_analytics_origin=rybbit_analytics_origin,
                    expected_rybbit_site_id_sha256=rybbit_site_id_sha256,
                    expected_snapshot_id=_text(payloads["overlay"].get("snapshot_id")),
                    expected_staged_receipt_sha256=staged_overlay_receipt_sha256,
                    now=observed_at,
                )
                and _text(overlay_activation.get("activation_authority_sha256")).lower()
                == activation_authority_sha256
                and authorized_workflow
                == {
                    "head_sha": head,
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                }
            )
            _record_check(
                checks,
                failures,
                name="activation_authority_current_run_bound",
                ok=activation_authority_ok,
                failure="activation_authority_mismatch",
            )

    evidence_authorized = not failures and set(raw_inputs) == {
        *json_inputs,
        "controller_bundle",
    }
    overlay_payload = payloads.get("overlay", {})
    overlay_activation = _object(overlay_payload.get("activation"))
    if phase == "preactivation":
        staged_overlay_receipt_sha256 = _text(
            identities.get("overlay", {}).get("sha256")
        ).lower()
        activation_authority_sha256 = ""
    else:
        staged_overlay_receipt_sha256 = _text(
            overlay_activation.get("staged_receipt_sha256")
        ).lower()
        activation_authority_sha256 = _text(
            identities.get("activation_authority", {}).get("sha256")
        ).lower()
    gold_release_hygiene = _object(
        payloads.get("gold_status", {}).get("release_hygiene")
    )
    release_hygiene_binding = {
        field: gold_release_hygiene[field]
        for field in (
            "manifest_runtime_commit",
            "head_commit",
            "parent_commit",
            "manifest_descendant_paths",
            "manifest_metadata_only_ancestor",
        )
        if field in gold_release_hygiene
    }
    launch_authorized = evidence_authorized and phase == "final"
    return {
        "schema": SCHEMA,
        "status": "pass" if evidence_authorized else "withheld",
        "generated_at": _iso(observed_at),
        "authority_phase": phase,
        "candidate_sha": candidate,
        "workflow": {
            "head_sha": head,
            "run_id": run_id,
            "run_attempt": run_attempt,
        },
        "release_hygiene_binding": release_hygiene_binding,
        "teable_authority": {
            "origin": teable_origin,
            "base_id_sha256": teable_base_id_sha256,
            "supplied_independently": True,
        },
        "rybbit_authority": {
            "public_origin": rybbit_public_origin,
            "analytics_origin": rybbit_analytics_origin,
            "site_id_sha256": rybbit_site_id_sha256,
            "supplied_independently": True,
        },
        "controller_bundle_sha256": (_text(identities.get("controller_bundle", {}).get("sha256"))),
        "activation_scope": {
            "snapshot_id": _text(overlay_payload.get("snapshot_id")),
            "staged_overlay_receipt_sha256": staged_overlay_receipt_sha256,
            "activation_authority_sha256": activation_authority_sha256,
        },
        "inputs": identities,
        "checks": checks,
        "failures": failures,
        "activation_authorized": evidence_authorized,
        "launch_authorized": launch_authorized,
        "notification_authorized": launch_authorized,
    }


def _atomic_write_private(path: Path, payload: dict[str, object]) -> None:
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        rendered = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a fail-closed, current-run PropertyQuarry launch authority envelope.")
    parser.add_argument(
        "--authority-phase",
        choices=("preactivation", "final"),
        default="final",
    )
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--workflow-head-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True)
    parser.add_argument("--expected-teable-origin", required=True)
    parser.add_argument("--expected-teable-base-id-sha256", required=True)
    parser.add_argument("--expected-rybbit-public-origin", required=True)
    parser.add_argument("--expected-rybbit-analytics-origin", required=True)
    parser.add_argument("--expected-rybbit-site-id-sha256", required=True)
    parser.add_argument("--gold-status", required=True)
    parser.add_argument("--live-provenance", required=True)
    parser.add_argument("--activation-receipt", required=True)
    parser.add_argument("--overlay-receipt", required=True)
    parser.add_argument("--rybbit-receipt", required=True)
    parser.add_argument("--dashboard-render-receipt", required=True)
    parser.add_argument("--structured-log-query-receipt", required=True)
    parser.add_argument("--distributed-trace-query-receipt", required=True)
    parser.add_argument("--security-receipt", required=True)
    parser.add_argument("--security-workflow-binding", required=True)
    parser.add_argument("--controller-bundle", required=True)
    parser.add_argument("--activation-authority", default="")
    parser.add_argument(
        "--expected-controller-bundle-sha256",
        default=os.getenv("PROPERTYQUARRY_RELEASE_CONTROLLER_SHA256") or "",
    )
    parser.add_argument(
        "--write",
        default="_completion/property_gold_status/launch-authority.json",
    )
    args = parser.parse_args()
    envelope = build_launch_authority_envelope(
        candidate_sha=args.candidate_sha,
        workflow_head_sha=args.workflow_head_sha,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        expected_teable_origin=args.expected_teable_origin,
        expected_teable_base_id_sha256=args.expected_teable_base_id_sha256,
        expected_rybbit_public_origin=args.expected_rybbit_public_origin,
        expected_rybbit_analytics_origin=args.expected_rybbit_analytics_origin,
        expected_rybbit_site_id_sha256=args.expected_rybbit_site_id_sha256,
        gold_status_path=Path(args.gold_status),
        live_provenance_path=Path(args.live_provenance),
        activation_receipt_path=Path(args.activation_receipt),
        overlay_receipt_path=Path(args.overlay_receipt),
        rybbit_receipt_path=Path(args.rybbit_receipt),
        dashboard_render_receipt_path=Path(args.dashboard_render_receipt),
        structured_log_query_receipt_path=Path(args.structured_log_query_receipt),
        distributed_trace_query_receipt_path=Path(
            args.distributed_trace_query_receipt
        ),
        security_receipt_path=Path(args.security_receipt),
        security_workflow_binding_path=Path(args.security_workflow_binding),
        controller_bundle_path=Path(args.controller_bundle),
        expected_controller_bundle_sha256=args.expected_controller_bundle_sha256,
        authority_phase=args.authority_phase,
        activation_authority_path=(
            Path(args.activation_authority) if args.activation_authority else None
        ),
    )
    _atomic_write_private(Path(args.write), envelope)
    try:
        print(json.dumps(envelope, sort_keys=True))
    except BrokenPipeError:
        with contextlib.suppress(Exception):
            sys.stdout.close()
    return 0 if envelope.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
