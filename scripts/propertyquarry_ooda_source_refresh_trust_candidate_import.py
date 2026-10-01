#!/usr/bin/env python3
"""Govern one public-key candidate import without granting producer trust."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as authorization
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_secure_file_io import (
    OutputExistsError,
    atomic_write_bytes,
    read_stable_bytes,
)
from scripts.propertyquarry_strict_json import loads_strict_json_object


CLAIM_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_import_claim.v1"
)
RESULT_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_import_result.v1"
)
VERIFY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1"
)
HISTORY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_import_history.v1"
)
PRESENTATION_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_import_presentation.v1"
)
SOURCE_DISCOVERY_SCHEMA = (
    "propertyquarry.ooda_source_refresh_trust_candidate_source_discovery.v1"
)
DEFAULT_IMPORT_DIR = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-imports"
)
DEFAULT_PRESENTATION_STATE_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "source-refresh-trust-candidate-import-presentation.json"
)
DEFAULT_SOURCE_DISCOVERY_DIR = Path("state/incoming_propertyquarry_trust")
DEFAULT_PRESENTATION_REMINDER_SECONDS = 86400.0
MAX_IMPORT_FILES = 128
MAX_DISCOVERY_FILES = 16
MAX_SOURCE_BYTES = trust_intake.MAX_RECEIPT_BYTES
CONFIRMATION = "IMPORT_VERIFIED_PUBLIC_KEY_CANDIDATE"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"pqtrustintake_[0-9a-f]{24}\Z")
_CLAIM_ID = re.compile(r"pqtrustimport_[0-9a-f]{24}\Z")
_DISCOVERY_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,122}\.json\Z")
_PRESENTABLE_IMPORT_STATES = {
    "awaiting_external_artifact",
    "external_artifact_not_admissible",
    "ready_for_manual_import",
    "recovery_required",
}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return trust_intake._canonical(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime | None:
    return trust_intake._timestamp(value)


def _with_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("integrity", None)
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(result)),
    }
    return result


def _integrity_verified(value: Mapping[str, Any]) -> bool:
    normalized = dict(value)
    integrity = normalized.pop("integrity", None)
    return bool(
        isinstance(integrity, Mapping)
        and frozenset(integrity)
        == {"algorithm", "canonical_payload_sha256"}
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    )


def _safety_fields() -> dict[str, bool]:
    return {
        "private_key_material_requested": False,
        "private_key_material_recorded": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "secret_values_recorded": False,
    }


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "import_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "repair the exact current intake/source binding before retrying; "
            "do not copy private keys or edit the trust registry"
        ),
        "request_id": "",
        "claim_id": "",
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        **_safety_fields(),
        "progress": {"current_evidence_verified": False},
        "actions": [],
    }


def _process_group_read_only(
    metadata: os.stat_result,
    *,
    directory: bool,
) -> bool:
    mode = stat.S_IMODE(metadata.st_mode)
    process_groups = {os.getegid(), *os.getgroups()}
    required_group_bits = 0o050 if directory else 0o040
    forbidden_group_bits = 0o020 if directory else 0o030
    return bool(
        metadata.st_gid in process_groups
        and mode & 0o007 == 0
        and mode & required_group_bits == required_group_bits
        and mode & forbidden_group_bits == 0
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _file_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


def _read_group_public_source_bytes(path: Path) -> bytes:
    """Read one public candidate through a retained group-readable parent FD."""

    target = Path(path).absolute()
    parent = target.parent
    if (
        target.name in {"", ".", ".."}
        or parent.resolve(strict=True) != parent
    ):
        raise ValueError("trust_candidate_import_group_source_path_invalid")
    parent_descriptor = -1
    source_descriptor = -1
    try:
        named_parent = parent.lstat()
        if not (
            stat.S_ISDIR(named_parent.st_mode)
            and _process_group_read_only(named_parent, directory=True)
        ):
            raise ValueError(
                "trust_candidate_import_group_source_directory_invalid"
            )
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        parent_identity = _directory_identity(opened_parent)
        if parent_identity != _directory_identity(named_parent):
            raise ValueError(
                "trust_candidate_import_group_source_directory_changed"
            )
        source_descriptor = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        opened_source = os.fstat(source_descriptor)
        if not (
            stat.S_ISREG(opened_source.st_mode)
            and opened_source.st_nlink == 1
            and 0 < opened_source.st_size <= MAX_SOURCE_BYTES
            and _process_group_read_only(opened_source, directory=False)
        ):
            raise ValueError(
                "trust_candidate_import_group_source_file_invalid"
            )
        source_identity = _file_identity(opened_source)
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(source_descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise ValueError(
                "trust_candidate_import_group_source_size_invalid"
            )
        reopened_source = os.fstat(source_descriptor)
        renamed_source = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        reopened_parent = os.fstat(parent_descriptor)
        renamed_parent = parent.lstat()
        if not (
            _file_identity(reopened_source) == source_identity
            and _file_identity(renamed_source) == source_identity
            and _directory_identity(reopened_parent) == parent_identity
            and _directory_identity(renamed_parent) == parent_identity
            and _process_group_read_only(reopened_source, directory=False)
            and _process_group_read_only(reopened_parent, directory=True)
        ):
            raise ValueError(
                "trust_candidate_import_group_source_changed"
            )
        return raw
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _private_directory(
    path: Path,
    *,
    create: bool,
    allow_group_read: bool = False,
) -> Path:
    target = Path(path).absolute()
    if not target.exists() and create:
        target.mkdir(parents=True, mode=0o700)
    metadata = target.lstat()
    owner_private = bool(
        metadata.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )
    if not stat.S_ISDIR(metadata.st_mode) or not (
        owner_private
        or (
            allow_group_read
            and _process_group_read_only(metadata, directory=True)
        )
    ):
        raise ValueError("trust_candidate_import_directory_not_admissible")
    return target


def _private_object(
    path: Path,
    *,
    field: str,
    allow_group_read: bool = False,
) -> tuple[dict[str, Any], bytes, str]:
    target = Path(path).absolute()
    metadata = target.lstat()
    owner_private = bool(
        metadata.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not (
            owner_private
            or (
                allow_group_read
                and _process_group_read_only(metadata, directory=False)
            )
        )
    ):
        raise ValueError(f"{field}_not_admissible")
    if metadata.st_uid in {0, os.geteuid()}:
        raw = read_stable_bytes(target, maximum_bytes=MAX_SOURCE_BYTES)
    else:
        raw = _read_group_public_source_bytes(target)
    return (
        loads_strict_json_object(
            raw,
            field=field,
            maximum_bytes=MAX_SOURCE_BYTES,
        ),
        raw,
        _sha256(raw),
    )


def _intake_admissible(
    value: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    progress = value.get("progress")
    source_binding = value.get("source_binding")
    requested_lanes = value.get("requested_lanes")
    expires_at = _timestamp(value.get("expires_at"))
    return bool(
        value.get("schema") == trust_intake.VERIFY_SCHEMA
        and value.get("status") == "verified"
        and value.get("request_status") == "staged"
        and value.get("intake_state")
        == "awaiting_producer_public_key_evidence"
        and _REQUEST_ID.fullmatch(str(value.get("request_id") or ""))
        and _SHA256.fullmatch(
            str(value.get("semantic_request_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(value.get("verification_receipt_sha256") or "")
        )
        and expires_at is not None
        and now <= expires_at
        and isinstance(requested_lanes, list)
        and requested_lanes == sorted(set(requested_lanes))
        and bool(requested_lanes)
        and all(lane in source_refresh._LANE_ARTIFACTS for lane in requested_lanes)
        and value.get("candidate_requirements")
        == trust_intake._candidate_requirements()
        and not list(value.get("candidates") or [])
        and value.get("request_staged") is True
        and value.get("public_key_candidate_recorded") is False
        and isinstance(source_binding, Mapping)
        and _SHA256.fullmatch(
            str(source_binding.get("trust_registry_sha256") or "")
        )
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and progress.get("claim_binding_verified") is True
        and progress.get("trust_registry_binding_verified") is True
        and progress.get("candidate_evidence_count") == 0
        and value.get("private_key_material_requested") is False
        and value.get("private_key_material_recorded") is False
        and value.get("trust_enrollment_authorized") is False
        and value.get("trust_registry_modified") is False
        and value.get("producer_dispatch_authorized") is False
        and value.get("producer_refresh_authorized") is False
        and value.get("automatic_source_refresh_allowed") is False
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
    )


def _source_candidate(
    source_path: Path,
    *,
    intake_report: Mapping[str, Any],
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    candidate, raw, _source_digest = _private_object(
        Path(source_path).absolute(),
        field="source_refresh_trust_candidate_import_source",
        allow_group_read=True,
    )
    canonical = _canonical(candidate)
    public_key_sha256 = str(candidate.get("public_key_sha256") or "")
    verified = trust_intake._verify_candidate_evidence(
        candidate,
        candidate_path=Path(source_path).absolute(),
        candidate_receipt_sha256=_sha256(canonical),
        expected_public_key_sha256=public_key_sha256,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    requested_lanes = set(
        str(lane) for lane in list(intake_report.get("requested_lanes") or [])
    )
    source_binding = dict(intake_report.get("source_binding") or {})
    if not (
        verified.get("request_id") == intake_report.get("request_id")
        and verified.get("semantic_request_sha256")
        == intake_report.get("semantic_request_sha256")
        and verified.get("trust_registry_sha256")
        == source_binding.get("trust_registry_sha256")
        and set(verified.get("lanes") or []) <= requested_lanes
    ):
        raise ValueError("trust_candidate_import_source_binding_mismatch")
    return {
        "candidate": candidate,
        "canonical": canonical,
        "source_sha256": _sha256(raw),
        "candidate_sha256": _sha256(canonical),
        "public_key_sha256": public_key_sha256,
        "producer_id": str(verified.get("producer_id") or ""),
        "key_id": str(verified.get("key_id") or ""),
        "lanes": list(verified.get("lanes") or []),
        "expires_at": str(verified.get("expires_at") or ""),
    }


def _source_discovery_admissible(value: Mapping[str, Any]) -> bool:
    progress = value.get("progress")
    state = str(value.get("discovery_state") or "")
    selected_path = str(value.get("selected_source_path") or "")
    selected_source_sha256 = str(
        value.get("selected_source_sha256") or ""
    )
    selected_public_key_sha256 = str(
        value.get("selected_public_key_sha256") or ""
    )
    expected_keys = {
        "schema",
        "status",
        "discovery_state",
        "updated_at",
        "blocking_reason",
        "next_action",
        "incoming_directory",
        "directory_present",
        "selected_source_path",
        "selected_source_sha256",
        "selected_public_key_sha256",
        "read_only",
        "source_directory_created",
        "source_copy_attempted",
        "source_file_modified",
        "invalid_path_names_recorded",
        "action_required",
        "interrupt_operator",
        "progress",
        "integrity",
        *_safety_fields(),
    }
    if not (
        frozenset(value) == expected_keys
        and value.get("schema") == SOURCE_DISCOVERY_SCHEMA
        and value.get("status") == "verified"
        and state
        in {
            "directory_absent",
            "empty",
            "candidate_ready",
            "candidates_not_admissible",
            "candidate_ambiguous",
            "directory_not_admissible",
            "too_many_entries",
        }
        and _timestamp(value.get("updated_at")) is not None
        and Path(str(value.get("incoming_directory") or "")).is_absolute()
        and isinstance(value.get("directory_present"), bool)
        and value.get("read_only") is True
        and value.get("source_directory_created") is False
        and value.get("source_copy_attempted") is False
        and value.get("source_file_modified") is False
        and value.get("invalid_path_names_recorded") is False
        and isinstance(value.get("action_required"), bool)
        and value.get("interrupt_operator") is False
        and all(value.get(key) is False for key in _safety_fields())
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
        and all(
            isinstance(progress.get(key), int)
            and not isinstance(progress.get(key), bool)
            and 0 <= int(progress.get(key) or 0) <= MAX_DISCOVERY_FILES
            for key in (
                "scanned_file_count",
                "valid_candidate_count",
                "invalid_candidate_count",
            )
        )
        and int(progress.get("valid_candidate_count") or 0)
        + int(progress.get("invalid_candidate_count") or 0)
        == int(progress.get("scanned_file_count") or 0)
        and _integrity_verified(value)
    ):
        return False
    if state == "candidate_ready":
        source = Path(selected_path)
        incoming = Path(str(value.get("incoming_directory") or ""))
        return bool(
            value.get("directory_present") is True
            and value.get("action_required") is True
            and source.is_absolute()
            and source.parent == incoming
            and _DISCOVERY_FILE.fullmatch(source.name)
            and _SHA256.fullmatch(selected_source_sha256)
            and _SHA256.fullmatch(selected_public_key_sha256)
            and progress.get("scanned_file_count") == 1
            and progress.get("valid_candidate_count") == 1
            and progress.get("invalid_candidate_count") == 0
            and not str(value.get("blocking_reason") or "")
        )
    return bool(
        not selected_path
        and not selected_source_sha256
        and not selected_public_key_sha256
        and (state != "directory_absent" or value.get("directory_present") is False)
        and (state == "directory_absent" or value.get("directory_present") is True)
        and (
            value.get("action_required")
            is (
                state
                in {
                    "candidates_not_admissible",
                    "candidate_ambiguous",
                    "directory_not_admissible",
                    "too_many_entries",
                }
            )
        )
    )


def discover_candidate_source(
    intake_report: Mapping[str, Any],
    *,
    source_discovery_dir: Path = DEFAULT_SOURCE_DISCOVERY_DIR,
    now: datetime | None = None,
    max_age_seconds: float = trust_intake.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Inspect one bounded private drop folder without copying or changing it."""

    observed_now = _now(now)
    target = Path(source_discovery_dir).absolute()
    state = "directory_absent"
    reason = ""
    next_action = "place one producer-owned public candidate JSON in the private drop folder"
    directory_present = False
    scanned_count = 0
    valid_rows: list[dict[str, Any]] = []
    invalid_count = 0
    try:
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            directory_present = True
            _private_directory(
                target,
                create=False,
                allow_group_read=True,
            )
            entries = sorted(os.scandir(target), key=lambda entry: entry.name)
            if len(entries) > MAX_DISCOVERY_FILES:
                state = "too_many_entries"
                reason = "trust_candidate_source_discovery_too_many_entries"
                next_action = (
                    "reduce the private drop folder to one admissible public candidate JSON"
                )
            else:
                scanned_count = len(entries)
                for entry in entries:
                    if (
                        _DISCOVERY_FILE.fullmatch(entry.name) is None
                        or entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        invalid_count += 1
                        continue
                    try:
                        source = _source_candidate(
                            Path(entry.path),
                            intake_report=intake_report,
                            now=observed_now,
                            max_age_seconds=max_age_seconds,
                        )
                    except Exception:
                        invalid_count += 1
                        continue
                    valid_rows.append(
                        {
                            "source_path": str(Path(entry.path).absolute()),
                            "source_sha256": source["source_sha256"],
                            "public_key_sha256": source["public_key_sha256"],
                        }
                    )
                if invalid_count:
                    state = "candidates_not_admissible"
                    reason = "trust_candidate_source_discovery_entry_not_admissible"
                    next_action = (
                        "replace the private drop contents with exactly one current, "
                        "request-bound public candidate JSON"
                    )
                elif len(valid_rows) > 1:
                    state = "candidate_ambiguous"
                    reason = "trust_candidate_source_discovery_ambiguous"
                    next_action = (
                        "reduce the private drop folder to exactly one current public candidate JSON"
                    )
                elif valid_rows:
                    state = "candidate_ready"
                    next_action = (
                        "review the discovered hashes and invoke the exact governed import manually"
                    )
                else:
                    state = "empty"
    except ValueError:
        state = "directory_not_admissible"
        reason = "trust_candidate_source_discovery_directory_not_admissible"
        next_action = (
            "repair the private drop directory ownership and mode without adding private keys"
        )
        valid_rows = []
        invalid_count = 0
        scanned_count = 0
    selected = valid_rows[0] if state == "candidate_ready" else {}
    report = _with_integrity(
        {
            "schema": SOURCE_DISCOVERY_SCHEMA,
            "status": "verified",
            "discovery_state": state,
            "updated_at": observed_now.isoformat(),
            "blocking_reason": reason,
            "next_action": next_action,
            "incoming_directory": str(target),
            "directory_present": directory_present,
            "selected_source_path": str(selected.get("source_path") or ""),
            "selected_source_sha256": str(
                selected.get("source_sha256") or ""
            ),
            "selected_public_key_sha256": str(
                selected.get("public_key_sha256") or ""
            ),
            "read_only": True,
            "source_directory_created": False,
            "source_copy_attempted": False,
            "source_file_modified": False,
            "invalid_path_names_recorded": False,
            "action_required": state
            in {
                "candidate_ready",
                "candidates_not_admissible",
                "candidate_ambiguous",
                "directory_not_admissible",
                "too_many_entries",
            },
            "interrupt_operator": False,
            "progress": {
                "current_evidence_verified": True,
                "scanned_file_count": scanned_count,
                "valid_candidate_count": len(valid_rows),
                "invalid_candidate_count": invalid_count,
            },
            **_safety_fields(),
        }
    )
    if not _source_discovery_admissible(report):
        raise ValueError("trust_candidate_source_discovery_not_admissible")
    return report


def _artifact_request(
    intake_report: Mapping[str, Any],
    *,
    candidate_dir: Path,
    source_discovery_dir: Path,
) -> dict[str, Any]:
    source_binding = dict(intake_report.get("source_binding") or {})
    return {
        "artifact_kind": "producer_owned_ed25519_public_key_candidate",
        "request_id": str(intake_report.get("request_id") or ""),
        "semantic_request_sha256": str(
            intake_report.get("semantic_request_sha256") or ""
        ),
        "trust_registry_sha256": str(
            source_binding.get("trust_registry_sha256") or ""
        ),
        "requested_lanes": list(intake_report.get("requested_lanes") or []),
        "expires_at": str(intake_report.get("expires_at") or ""),
        "candidate_schema": trust_intake.CANDIDATE_SCHEMA,
        "candidate_requirements": dict(
            intake_report.get("candidate_requirements") or {}
        ),
        "import_destination_directory": str(Path(candidate_dir).absolute()),
        "imported_filename": "<public_key_sha256>.json",
        "source_filename_pattern": "*.json",
        "source_discovery_directory": str(
            Path(source_discovery_dir).absolute()
        ),
        "automatic_read_only_discovery": True,
        "private_key_material_allowed": False,
        "candidate_confers_authority": False,
        "out_of_band_identity_verification_required": True,
    }


def inspect_candidate_import_readiness(
    intake_report: Mapping[str, Any],
    *,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    import_dir: Path = DEFAULT_IMPORT_DIR,
    source_path: Path | None = None,
    source_discovery_dir: Path | None = None,
    now: datetime | None = None,
    max_age_seconds: float = trust_intake.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    history = inspect_import_history(
        import_dir=import_dir,
        candidate_dir=candidate_dir,
        now=observed_now,
    )
    if history.get("status") != "verified":
        return _blocked(
            str(history.get("blocking_reason") or "trust_candidate_import_history_invalid"),
            now=observed_now,
        )
    latest = dict(history.get("latest_import") or {})
    request_id = str(intake_report.get("request_id") or "")
    if latest and latest.get("request_id") == request_id:
        if latest.get("import_state") == "succeeded":
            return {
                **latest,
                "schema": VERIFY_SCHEMA,
                "status": "verified",
                "updated_at": observed_now.isoformat(),
                "action_required": True,
                "interrupt_operator": False,
                "operator_review_required": True,
                "next_action": (
                    "run the safe OODA tick so the imported candidate enters "
                    "out-of-band identity review"
                ),
                "progress": {"current_evidence_verified": True},
                "history": history,
            }
        recovery_reason = str(latest.get("blocking_reason") or "").strip()
        if not recovery_reason:
            recovery_reason = "trust_candidate_import_claim_without_result"
        return {
            **latest,
            "schema": VERIFY_SCHEMA,
            "status": "verified",
            "import_state": "recovery_required",
            "updated_at": observed_now.isoformat(),
            "blocking_reason": recovery_reason,
            "semantic_request_sha256": str(
                intake_report.get("semantic_request_sha256") or ""
            ),
            "intake_verification_sha256": str(
                intake_report.get("verification_receipt_sha256") or ""
            ),
            "action_required": True,
            "interrupt_operator": False,
            "operator_review_required": False,
            "candidate_import_authorized": False,
            "candidate_imported": False,
            "public_key_candidate_recorded": False,
            **_safety_fields(),
            "next_action": (
                "inspect the immutable import claim and result receipts; do "
                "not delete, overwrite, or retry the candidate automatically"
            ),
            "progress": {
                "current_evidence_verified": True,
                "import_history_verified": True,
                "recovery_reference_verified": True,
            },
            "history": history,
        }
    if intake_report.get("status") == "verified" and (
        intake_report.get("intake_state") == "not_required"
        or list(intake_report.get("candidates") or [])
    ):
        return {
            **_blocked("", now=observed_now),
            "status": "verified",
            "import_state": "not_required",
            "blocking_reason": "",
            "next_action": str(intake_report.get("next_action") or ""),
            "progress": {"current_evidence_verified": True},
            "history": history,
        }
    if not _intake_admissible(intake_report, now=observed_now):
        return _blocked(
            "trust_candidate_import_intake_not_admissible",
            now=observed_now,
        )
    configured_discovery_dir = Path(
        source_discovery_dir or DEFAULT_SOURCE_DISCOVERY_DIR
    ).absolute()
    source_discovery: dict[str, Any] = {}
    if source_path is None and source_discovery_dir is not None:
        source_discovery = discover_candidate_source(
            intake_report,
            source_discovery_dir=configured_discovery_dir,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        if source_discovery.get("discovery_state") == "candidate_ready":
            source_path = Path(
                str(source_discovery.get("selected_source_path") or "")
            )
    report = {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "import_state": "awaiting_external_artifact",
        "updated_at": observed_now.isoformat(),
        "blocking_reason": "",
        "next_action": (
            "obtain the producer-owned public candidate JSON, then inspect and "
            "import it with the exact governed CLI; never provide a private key"
        ),
        "request_id": request_id,
        "semantic_request_sha256": str(
            intake_report.get("semantic_request_sha256") or ""
        ),
        "intake_verification_sha256": str(
            intake_report.get("verification_receipt_sha256") or ""
        ),
        "artifact_request": _artifact_request(
            intake_report,
            candidate_dir=candidate_dir,
            source_discovery_dir=configured_discovery_dir,
        ),
        "action_required": True,
        "interrupt_operator": source_path is None,
        "operator_review_required": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        **_safety_fields(),
        "progress": {
            "current_evidence_verified": True,
            "source_discovery_verified": (
                not source_discovery
                or _source_discovery_admissible(source_discovery)
            ),
        },
        "actions": [],
        "history": history,
    }
    if source_discovery:
        report["source_discovery"] = source_discovery
        report["source_discovery_receipt_sha256"] = _sha256(
            _canonical(source_discovery)
        )
        discovery_state = str(
            source_discovery.get("discovery_state") or ""
        )
        if discovery_state in {
            "candidates_not_admissible",
            "candidate_ambiguous",
            "directory_not_admissible",
            "too_many_entries",
        }:
            report.update(
                {
                    "import_state": "external_artifact_not_admissible",
                    "blocking_reason": str(
                        source_discovery.get("blocking_reason") or ""
                    ),
                    "next_action": str(
                        source_discovery.get("next_action") or ""
                    ),
                    "interrupt_operator": True,
                }
            )
            return report
    if source_path is None:
        return report
    if Path(source_path).absolute().is_relative_to(Path(candidate_dir).absolute()):
        return _blocked(
            "trust_candidate_import_source_already_in_destination",
            now=observed_now,
        )
    try:
        source = _source_candidate(
            source_path,
            intake_report=intake_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
    except Exception:
        return _blocked(
            "trust_candidate_import_source_not_admissible",
            now=observed_now,
        )
    report.update(
        {
            "import_state": "ready_for_manual_import",
            "next_action": (
                "invoke the exact governed import with explicit operator identity, "
                "evidence, hashes, and literal confirmation"
            ),
            "source_sha256": source["source_sha256"],
            "candidate_sha256": source["candidate_sha256"],
            "public_key_sha256": source["public_key_sha256"],
            "producer_id": source["producer_id"],
            "key_id": source["key_id"],
            "candidate_lanes": source["lanes"],
            "source_path": str(Path(source_path).absolute()),
            "candidate_destination": str(
                Path(candidate_dir).absolute()
                / f"{source['public_key_sha256']}.json"
            ),
            "interrupt_operator": True,
        }
    )
    return report


def _bounded_presentation_reminder_seconds(value: float) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or not 300.0 <= normalized <= 604800.0:
        raise ValueError(
            "trust_candidate_import_presentation_reminder_not_admissible"
        )
    return normalized


def _presentation_report_admissible(report: Mapping[str, Any]) -> bool:
    artifact_request = report.get("artifact_request")
    progress = report.get("progress")
    import_state = str(report.get("import_state") or "")
    expected_artifact_keys = {
        "artifact_kind",
        "request_id",
        "semantic_request_sha256",
        "trust_registry_sha256",
        "requested_lanes",
        "expires_at",
        "candidate_schema",
        "candidate_requirements",
        "import_destination_directory",
        "imported_filename",
        "source_filename_pattern",
        "source_discovery_directory",
        "automatic_read_only_discovery",
        "private_key_material_allowed",
        "candidate_confers_authority",
        "out_of_band_identity_verification_required",
    }
    if not (
        report.get("schema") == VERIFY_SCHEMA
        and report.get("status") == "verified"
        and import_state in _PRESENTABLE_IMPORT_STATES
        and _REQUEST_ID.fullmatch(str(report.get("request_id") or ""))
        and _SHA256.fullmatch(
            str(report.get("semantic_request_sha256") or "")
        )
        and _SHA256.fullmatch(
            str(report.get("intake_verification_sha256") or "")
        )
        and report.get("action_required") is True
        and report.get("operator_review_required") is False
        and report.get("candidate_import_authorized") is False
        and (
            report.get("candidate_import_attempted") is False
            or import_state == "recovery_required"
            and report.get("candidate_import_attempted") is True
        )
        and report.get("candidate_imported") is False
        and report.get("public_key_candidate_recorded") is False
        and all(report.get(key) is False for key in _safety_fields())
        and isinstance(progress, Mapping)
        and progress.get("current_evidence_verified") is True
    ):
        return False
    if import_state == "recovery_required":
        progress = dict(progress)
        return bool(
            _CLAIM_ID.fullmatch(str(report.get("claim_id") or ""))
            and _SHA256.fullmatch(
                str(report.get("claim_sha256") or "")
            )
            and _SHA256.fullmatch(
                str(report.get("public_key_sha256") or "")
            )
            and (
                report.get("result_sha256") == ""
                or _SHA256.fullmatch(
                    str(report.get("result_sha256") or "")
                )
            )
            and bool(str(report.get("blocking_reason") or "").strip())
            and progress.get("import_history_verified") is True
            and progress.get("recovery_reference_verified") is True
        )
    if not (
        isinstance(artifact_request, Mapping)
        and frozenset(artifact_request) == expected_artifact_keys
        and artifact_request.get("artifact_kind")
        == "producer_owned_ed25519_public_key_candidate"
        and artifact_request.get("request_id") == report.get("request_id")
        and artifact_request.get("semantic_request_sha256")
        == report.get("semantic_request_sha256")
        and _SHA256.fullmatch(
            str(artifact_request.get("trust_registry_sha256") or "")
        )
        and isinstance(artifact_request.get("requested_lanes"), list)
        and artifact_request.get("requested_lanes")
        == sorted(set(artifact_request.get("requested_lanes") or []))
        and bool(artifact_request.get("requested_lanes"))
        and _timestamp(artifact_request.get("expires_at")) is not None
        and artifact_request.get("candidate_schema")
        == trust_intake.CANDIDATE_SCHEMA
        and artifact_request.get("candidate_requirements")
        == trust_intake._candidate_requirements()
        and Path(
            str(artifact_request.get("import_destination_directory") or "")
        ).is_absolute()
        and artifact_request.get("imported_filename")
        == "<public_key_sha256>.json"
        and artifact_request.get("source_filename_pattern") == "*.json"
        and Path(
            str(artifact_request.get("source_discovery_directory") or "")
        ).is_absolute()
        and artifact_request.get("automatic_read_only_discovery") is True
        and artifact_request.get("private_key_material_allowed") is False
        and artifact_request.get("candidate_confers_authority") is False
        and artifact_request.get("out_of_band_identity_verification_required")
        is True
    ):
        return False
    source_discovery = report.get("source_discovery")
    if source_discovery is not None and not (
        isinstance(source_discovery, Mapping)
        and _source_discovery_admissible(source_discovery)
        and _SHA256.fullmatch(
            str(report.get("source_discovery_receipt_sha256") or "")
        )
        and report.get("source_discovery_receipt_sha256")
        == _sha256(_canonical(source_discovery))
        and progress.get("source_discovery_verified") is True
    ):
        return False
    if import_state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
    }:
        return bool(
            not any(
                str(report.get(key) or "")
                for key in (
                    "source_sha256",
                    "candidate_sha256",
                    "public_key_sha256",
                )
            )
            and (
                import_state != "external_artifact_not_admissible"
                or (
                    isinstance(source_discovery, Mapping)
                    and source_discovery.get("action_required") is True
                    and source_discovery.get("discovery_state")
                    not in {"directory_absent", "empty", "candidate_ready"}
                )
            )
        )
    return bool(
        all(
            _SHA256.fullmatch(str(report.get(key) or ""))
            for key in (
                "source_sha256",
                "candidate_sha256",
                "public_key_sha256",
            )
        )
        and Path(str(report.get("source_path") or "")).is_absolute()
        and Path(str(report.get("candidate_destination") or "")).is_absolute()
    )


def _presentation_digest(report: Mapping[str, Any]) -> str:
    if not _presentation_report_admissible(report):
        raise ValueError(
            "trust_candidate_import_presentation_report_not_admissible"
        )
    if report.get("import_state") == "recovery_required":
        return _sha256(
            _canonical(
                {
                    "request_id": str(report.get("request_id") or ""),
                    "semantic_request_sha256": str(
                        report.get("semantic_request_sha256") or ""
                    ),
                    "import_state": "recovery_required",
                    "claim_id": str(report.get("claim_id") or ""),
                    "claim_sha256": str(
                        report.get("claim_sha256") or ""
                    ),
                    "result_sha256": str(
                        report.get("result_sha256") or ""
                    ),
                    "public_key_sha256": str(
                        report.get("public_key_sha256") or ""
                    ),
                    "blocking_reason": str(
                        report.get("blocking_reason") or ""
                    ),
                    "candidate_import_attempted": (
                        report.get("candidate_import_attempted") is True
                    ),
                }
            )
        )
    artifact_request = dict(report.get("artifact_request") or {})
    artifact_request.pop("expires_at", None)
    semantic: dict[str, Any] = {
        "request_id": str(report.get("request_id") or ""),
        "semantic_request_sha256": str(
            report.get("semantic_request_sha256") or ""
        ),
        "import_state": str(report.get("import_state") or ""),
        "artifact_request": artifact_request,
    }
    source_discovery = report.get("source_discovery")
    if isinstance(source_discovery, Mapping):
        discovery_progress = dict(source_discovery.get("progress") or {})
        semantic["source_discovery"] = {
            "discovery_state": str(
                source_discovery.get("discovery_state") or ""
            ),
            "incoming_directory": str(
                source_discovery.get("incoming_directory") or ""
            ),
            "scanned_file_count": discovery_progress.get(
                "scanned_file_count"
            ),
            "valid_candidate_count": discovery_progress.get(
                "valid_candidate_count"
            ),
            "invalid_candidate_count": discovery_progress.get(
                "invalid_candidate_count"
            ),
            "selected_source_sha256": str(
                source_discovery.get("selected_source_sha256") or ""
            ),
            "selected_public_key_sha256": str(
                source_discovery.get("selected_public_key_sha256") or ""
            ),
        }
    if report.get("import_state") == "ready_for_manual_import":
        semantic["source_sha256"] = str(report.get("source_sha256") or "")
        semantic["public_key_sha256"] = str(
            report.get("public_key_sha256") or ""
        )
    return _sha256(_canonical(semantic))


def apply_candidate_import_presentation_state(
    report: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Suppress repeat interruption while retaining a pending artifact action."""

    projected = dict(report)
    if (
        report.get("status") != "verified"
        or report.get("action_required") is not True
        or report.get("import_state")
        not in _PRESENTABLE_IMPORT_STATES
    ):
        projected["interrupt_operator"] = False
        projected["presentation"] = {
            "state": "not_required",
            "presentation_digest": "",
            "already_presented": False,
            "reminder_due": False,
        }
        return projected
    observed_now = _now(now)
    presentation_digest = _presentation_digest(report)
    target = Path(state_path).absolute()
    try:
        state, _raw, _digest = _private_object(
            target,
            field="trust_candidate_import_presentation",
        )
    except FileNotFoundError:
        state = {}
    presented_at: datetime | None = None
    reminder_after_seconds = DEFAULT_PRESENTATION_REMINDER_SECONDS
    if state:
        expected_keys = {
            "schema",
            "request_id",
            "semantic_request_sha256",
            "import_state",
            "presentation_digest",
            "presented_at",
            "reminder_after_seconds",
            "delivery_state_updated",
            "provider_quota_consumed",
            "trust_registry_modified",
            "protected_operation_executed",
            "integrity",
        }
        presented_at = _timestamp(state.get("presented_at"))
        try:
            reminder_after_seconds = _bounded_presentation_reminder_seconds(
                float(state.get("reminder_after_seconds"))
            )
        except (TypeError, ValueError):
            reminder_after_seconds = -1.0
        if not (
            frozenset(state) == expected_keys
            and state.get("schema") == PRESENTATION_SCHEMA
            and _REQUEST_ID.fullmatch(str(state.get("request_id") or ""))
            and _SHA256.fullmatch(
                str(state.get("semantic_request_sha256") or "")
            )
            and state.get("import_state") in _PRESENTABLE_IMPORT_STATES
            and _SHA256.fullmatch(
                str(state.get("presentation_digest") or "")
            )
            and presented_at is not None
            and (observed_now - presented_at).total_seconds() >= -30.0
            and reminder_after_seconds >= 300.0
            and _integrity_verified(state)
            and state.get("delivery_state_updated") is False
            and state.get("provider_quota_consumed") is False
            and state.get("trust_registry_modified") is False
            and state.get("protected_operation_executed") is False
        ):
            raise ValueError(
                "trust_candidate_import_presentation_not_admissible"
            )
    same_presentation = bool(
        state
        and state.get("presentation_digest") == presentation_digest
        and state.get("request_id") == report.get("request_id")
    )
    presentation_age_seconds = (
        max(0.0, (observed_now - presented_at).total_seconds())
        if same_presentation and presented_at is not None
        else 0.0
    )
    reminder_due = bool(
        same_presentation
        and presentation_age_seconds >= reminder_after_seconds
    )
    already_presented = same_presentation and not reminder_due
    projected["interrupt_operator"] = not already_presented
    projected["presentation"] = {
        "state": (
            "already_presented"
            if already_presented
            else "reminder_due"
            if reminder_due
            else "novel"
        ),
        "presentation_digest": presentation_digest,
        "already_presented": already_presented,
        "reminder_due": reminder_due,
        "presented_at": presented_at.isoformat() if presented_at else "",
        "reminder_after_seconds": reminder_after_seconds,
    }
    return projected


def record_candidate_import_presentation(
    report: Mapping[str, Any],
    *,
    state_path: Path = DEFAULT_PRESENTATION_STATE_PATH,
    expected_presentation_digest: str,
    now: datetime | None = None,
    reminder_after_seconds: float = DEFAULT_PRESENTATION_REMINDER_SECONDS,
) -> dict[str, Any]:
    """Record only the exact public-artifact action that was just presented."""

    observed_now = _now(now)
    if report.get("action_required") is not True:
        return {
            "status": "not_required",
            "presentation_digest": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    try:
        presentation_digest = _presentation_digest(report)
        bounded_reminder = _bounded_presentation_reminder_seconds(
            reminder_after_seconds
        )
    except (TypeError, ValueError):
        presentation_digest = ""
        bounded_reminder = -1.0
    presentation = dict(report.get("presentation") or {})
    if not (
        _SHA256.fullmatch(str(expected_presentation_digest or ""))
        and presentation_digest == expected_presentation_digest
        and (
            not presentation
            or presentation.get("presentation_digest")
            == expected_presentation_digest
        )
        and bounded_reminder >= 300.0
    ):
        return {
            "status": "blocked",
            "blocking_reason": (
                "trust_candidate_import_presentation_binding_mismatch"
            ),
            "presentation_digest": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    target = Path(state_path).absolute()
    state = _with_integrity(
        {
            "schema": PRESENTATION_SCHEMA,
            "request_id": str(report.get("request_id") or ""),
            "semantic_request_sha256": str(
                report.get("semantic_request_sha256") or ""
            ),
            "import_state": str(report.get("import_state") or ""),
            "presentation_digest": presentation_digest,
            "presented_at": observed_now.isoformat(),
            "reminder_after_seconds": bounded_reminder,
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    )
    try:
        atomic_write_bytes(target, _canonical(state), overwrite=True)
        persisted, _raw, digest = _private_object(
            target,
            field="trust_candidate_import_presentation",
        )
        if persisted != state or not _integrity_verified(persisted):
            raise ValueError(
                "trust_candidate_import_presentation_persistence_mismatch"
            )
    except Exception:
        return {
            "status": "blocked",
            "blocking_reason": (
                "trust_candidate_import_presentation_persistence_failed"
            ),
            "presentation_digest": "",
            "delivery_state_updated": False,
            "provider_quota_consumed": False,
            "trust_registry_modified": False,
            "protected_operation_executed": False,
        }
    return {
        "status": "recorded",
        "presentation_digest": presentation_digest,
        "presentation_receipt_sha256": digest,
        "delivery_state_updated": False,
        "provider_quota_consumed": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
    }


def _build_claim(
    readiness: Mapping[str, Any],
    *,
    operator_id: str,
    import_method: str,
    evidence_ref: str,
    destination: Path,
    now: datetime,
) -> dict[str, Any]:
    normalized_operator = str(operator_id or "").strip()
    normalized_method = str(import_method or "").strip()
    normalized_evidence = str(evidence_ref or "").strip()
    if not (
        readiness.get("status") == "verified"
        and readiness.get("import_state") == "ready_for_manual_import"
        and trust_decision._DECIDER_ID.fullmatch(normalized_operator)
        and normalized_method in authorization.AUTHORIZATION_METHODS
        and trust_decision._EVIDENCE_REF.fullmatch(normalized_evidence)
    ):
        raise ValueError("trust_candidate_import_claim_not_admissible")
    scope = {
        key: readiness[key]
        for key in (
            "request_id",
            "semantic_request_sha256",
            "intake_verification_sha256",
            "source_sha256",
            "candidate_sha256",
            "public_key_sha256",
            "producer_id",
            "key_id",
            "candidate_lanes",
        )
    }
    operator = {
        "operator_id": normalized_operator,
        "import_method": normalized_method,
        "evidence_ref": normalized_evidence,
        "explicit_invocation_recorded": True,
        "source": "governed_operator_cli",
    }
    claim_id = f"pqtrustimport_{_sha256(_canonical({'scope': scope, 'operator': operator}))[:24]}"
    return _with_integrity(
        {
            "schema": CLAIM_SCHEMA,
            "status": "claimed",
            "claim_id": claim_id,
            "claimed_at": now.isoformat(),
            "scope": scope,
            "operator": operator,
            "destination_path": str(Path(destination).absolute()),
            "candidate_import_authorized": True,
            "candidate_import_attempted": False,
            "candidate_imported": False,
            "public_key_candidate_recorded": False,
            **_safety_fields(),
        }
    )


def _verify_claim(claim: Mapping[str, Any]) -> bool:
    scope = claim.get("scope")
    operator = claim.get("operator")
    destination = Path(str(claim.get("destination_path") or ""))
    expected_claim_keys = {
        "schema",
        "status",
        "claim_id",
        "claimed_at",
        "scope",
        "operator",
        "destination_path",
        "candidate_import_authorized",
        "candidate_import_attempted",
        "candidate_imported",
        "public_key_candidate_recorded",
        "integrity",
        *_safety_fields(),
    }
    expected_scope_keys = {
        "request_id",
        "semantic_request_sha256",
        "intake_verification_sha256",
        "source_sha256",
        "candidate_sha256",
        "public_key_sha256",
        "producer_id",
        "key_id",
        "candidate_lanes",
    }
    expected_operator_keys = {
        "operator_id",
        "import_method",
        "evidence_ref",
        "explicit_invocation_recorded",
        "source",
    }
    if not (
        frozenset(claim) == expected_claim_keys
        and claim.get("schema") == CLAIM_SCHEMA
        and claim.get("status") == "claimed"
        and _integrity_verified(claim)
        and _CLAIM_ID.fullmatch(str(claim.get("claim_id") or ""))
        and _timestamp(claim.get("claimed_at")) is not None
        and isinstance(scope, Mapping)
        and frozenset(scope) == expected_scope_keys
        and isinstance(operator, Mapping)
        and frozenset(operator) == expected_operator_keys
        and _REQUEST_ID.fullmatch(str(scope.get("request_id") or ""))
        and all(
            _SHA256.fullmatch(str(scope.get(key) or ""))
            for key in (
                "semantic_request_sha256",
                "intake_verification_sha256",
                "source_sha256",
                "candidate_sha256",
                "public_key_sha256",
            )
        )
        and claims._IDENTIFIER.fullmatch(str(scope.get("producer_id") or ""))
        and claims._IDENTIFIER.fullmatch(str(scope.get("key_id") or ""))
        and isinstance(scope.get("candidate_lanes"), list)
        and scope.get("candidate_lanes")
        == sorted(set(scope.get("candidate_lanes") or []))
        and bool(scope.get("candidate_lanes"))
        and destination.is_absolute()
        and destination.name == f"{scope.get('public_key_sha256')}.json"
        and trust_decision._DECIDER_ID.fullmatch(
            str(operator.get("operator_id") or "")
        )
        and operator.get("import_method") in authorization.AUTHORIZATION_METHODS
        and trust_decision._EVIDENCE_REF.fullmatch(
            str(operator.get("evidence_ref") or "")
        )
        and operator.get("explicit_invocation_recorded") is True
        and operator.get("source") == "governed_operator_cli"
        and claim.get("candidate_import_authorized") is True
        and claim.get("candidate_import_attempted") is False
        and claim.get("candidate_imported") is False
        and claim.get("public_key_candidate_recorded") is False
        and all(claim.get(key) is False for key in _safety_fields())
    ):
        return False
    semantic = {"scope": dict(scope), "operator": dict(operator)}
    return claim.get("claim_id") == (
        f"pqtrustimport_{_sha256(_canonical(semantic))[:24]}"
    )


def _build_result(
    claim: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    attempted: bool,
    imported: bool,
    destination_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    return _with_integrity(
        {
            "schema": RESULT_SCHEMA,
            "status": status,
            "completed_at": now.isoformat(),
            "blocking_reason": reason,
            "claim_id": str(claim.get("claim_id") or ""),
            "claim_sha256": _sha256(_canonical(claim)),
            "request_id": str(dict(claim.get("scope") or {}).get("request_id") or ""),
            "public_key_sha256": str(
                dict(claim.get("scope") or {}).get("public_key_sha256") or ""
            ),
            "destination_path": str(claim.get("destination_path") or ""),
            "destination_sha256": destination_sha256,
            "candidate_import_authorized": False,
            "candidate_import_attempted": attempted,
            "candidate_imported": imported,
            "public_key_candidate_recorded": imported,
            **_safety_fields(),
        }
    )


def _verify_result(result: Mapping[str, Any], *, claim: Mapping[str, Any]) -> bool:
    status = str(result.get("status") or "")
    imported = status == "succeeded"
    scope = dict(claim.get("scope") or {})
    claimed_at = _timestamp(claim.get("claimed_at"))
    completed_at = _timestamp(result.get("completed_at"))
    expected_result_keys = {
        "schema",
        "status",
        "completed_at",
        "blocking_reason",
        "claim_id",
        "claim_sha256",
        "request_id",
        "public_key_sha256",
        "destination_path",
        "destination_sha256",
        "candidate_import_authorized",
        "candidate_import_attempted",
        "candidate_imported",
        "public_key_candidate_recorded",
        "integrity",
        *_safety_fields(),
    }
    return bool(
        frozenset(result) == expected_result_keys
        and result.get("schema") == RESULT_SCHEMA
        and status in {"succeeded", "import_failed"}
        and _integrity_verified(result)
        and completed_at is not None
        and claimed_at is not None
        and claimed_at <= completed_at
        and result.get("claim_id") == claim.get("claim_id")
        and result.get("claim_sha256") == _sha256(_canonical(claim))
        and result.get("request_id") == scope.get("request_id")
        and result.get("public_key_sha256") == scope.get("public_key_sha256")
        and result.get("destination_path") == claim.get("destination_path")
        and (
            result.get("destination_sha256") == scope.get("candidate_sha256")
        )
        is imported
        and result.get("candidate_import_authorized") is False
        and result.get("candidate_import_attempted") is True
        and (result.get("candidate_imported") is True) is imported
        and (result.get("public_key_candidate_recorded") is True) is imported
        and all(result.get(key) is False for key in _safety_fields())
        and bool(not str(result.get("blocking_reason") or "")) is imported
    )


def _paths(
    import_dir: Path,
    *,
    request_id: str,
    public_key_sha256: str,
) -> tuple[Path, Path]:
    if not (
        _REQUEST_ID.fullmatch(request_id)
        and _SHA256.fullmatch(public_key_sha256)
    ):
        raise ValueError("trust_candidate_import_path_binding_invalid")
    target = Path(import_dir).absolute()
    suffix = f"{request_id}--{public_key_sha256}.json"
    return target / f"claim--{suffix}", target / f"result--{suffix}"


def import_current_candidate(
    *,
    source_path: Path,
    expected_request_id: str,
    expected_semantic_request_sha256: str,
    expected_intake_verification_sha256: str,
    expected_source_sha256: str,
    expected_public_key_sha256: str,
    operator_id: str,
    import_method: str,
    evidence_ref: str,
    confirmation: str,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    import_dir: Path = DEFAULT_IMPORT_DIR,
    now: datetime | None = None,
    max_age_seconds: float = trust_intake.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    if confirmation != CONFIRMATION:
        return _blocked(
            "trust_candidate_import_confirmation_missing",
            now=observed_now,
        )
    intake_report = trust_intake.inspect_trust_intake_bundle(
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        receipt_path=intake_receipt_path,
        verification_path=intake_verification_path,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    readiness = inspect_candidate_import_readiness(
        intake_report,
        candidate_dir=candidate_dir,
        import_dir=import_dir,
        source_path=source_path,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    expected = {
        "request_id": expected_request_id,
        "semantic_request_sha256": expected_semantic_request_sha256,
        "intake_verification_sha256": expected_intake_verification_sha256,
        "source_sha256": expected_source_sha256,
        "public_key_sha256": expected_public_key_sha256,
    }
    if not (
        readiness.get("status") == "verified"
        and readiness.get("import_state") == "ready_for_manual_import"
        and all(readiness.get(key) == value for key, value in expected.items())
    ):
        return _blocked(
            "trust_candidate_import_exact_binding_mismatch",
            now=observed_now,
        )
    try:
        source = _source_candidate(
            source_path,
            intake_report=intake_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        destination = (
            Path(candidate_dir).absolute()
            / f"{source['public_key_sha256']}.json"
        )
        _private_directory(import_dir, create=True)
        claim_path, result_path = _paths(
            import_dir,
            request_id=expected_request_id,
            public_key_sha256=expected_public_key_sha256,
        )
        claim = _build_claim(
            readiness,
            operator_id=operator_id,
            import_method=import_method,
            evidence_ref=evidence_ref,
            destination=destination,
            now=observed_now,
        )
        atomic_write_bytes(claim_path, _canonical(claim), overwrite=False)
    except OutputExistsError:
        return _blocked(
            "trust_candidate_import_already_claimed",
            now=observed_now,
        )
    except Exception:
        return _blocked(
            "trust_candidate_import_preclaim_not_admissible",
            now=observed_now,
        )
    imported = False
    destination_sha256 = ""
    reason = ""
    try:
        persisted_claim, _raw, _digest = _private_object(
            claim_path,
            field="trust_candidate_import_claim",
        )
        if persisted_claim != claim or not _verify_claim(persisted_claim):
            raise ValueError("trust_candidate_import_claim_persistence_invalid")
        atomic_write_bytes(
            destination,
            source["canonical"],
            overwrite=False,
        )
        persisted_candidate, candidate_raw, destination_sha256 = (
            claims._read_only_snapshot(
                destination,
                field="source_refresh_trust_candidate",
                maximum_bytes=MAX_SOURCE_BYTES,
            )
        )
        if (
            persisted_candidate != source["candidate"]
            or candidate_raw != source["canonical"]
            or destination_sha256 != source["candidate_sha256"]
        ):
            raise ValueError("trust_candidate_import_postcondition_invalid")
        _source_candidate(
            destination,
            intake_report=intake_report,
            now=observed_now,
            max_age_seconds=max_age_seconds,
        )
        imported = True
    except Exception as exc:
        reason = str(exc) or "trust_candidate_import_failed"
    completed_at = _now(now)
    result = _build_result(
        claim,
        status="succeeded" if imported else "import_failed",
        reason=reason,
        attempted=True,
        imported=imported,
        destination_sha256=destination_sha256,
        now=completed_at,
    )
    try:
        atomic_write_bytes(result_path, _canonical(result), overwrite=False)
        persisted_result, _raw, _digest = _private_object(
            result_path,
            field="trust_candidate_import_result",
        )
        if persisted_result != result or not _verify_result(
            persisted_result,
            claim=claim,
        ):
            raise ValueError("trust_candidate_import_result_persistence_invalid")
    except Exception:
        return {
            **_blocked(
                "trust_candidate_import_result_persistence_failed",
                now=completed_at,
            ),
            "import_state": "recovery_required",
            "request_id": expected_request_id,
            "claim_id": str(claim.get("claim_id") or ""),
            "public_key_sha256": expected_public_key_sha256,
            "destination_path": str(destination),
            "destination_sha256": destination_sha256,
            "candidate_import_attempted": True,
            "candidate_imported": imported,
            "public_key_candidate_recorded": imported,
            "action_required": True,
        }
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "import_state": str(result.get("status") or ""),
        "updated_at": completed_at.isoformat(),
        "blocking_reason": reason,
        "next_action": (
            "run the safe OODA tick to stage out-of-band identity review"
            if imported
            else "inspect the immutable claim and result before retrying"
        ),
        "request_id": expected_request_id,
        "claim_id": str(claim.get("claim_id") or ""),
        "public_key_sha256": expected_public_key_sha256,
        "destination_path": str(destination),
        "destination_sha256": destination_sha256,
        "action_required": True,
        "interrupt_operator": False,
        "operator_review_required": imported,
        "candidate_import_authorized": False,
        "candidate_import_attempted": True,
        "candidate_imported": imported,
        "public_key_candidate_recorded": imported,
        **_safety_fields(),
        "progress": {
            "current_evidence_verified": True,
            "import_claim_integrity_verified": True,
            "import_result_integrity_verified": True,
            "postcondition_verified": imported,
        },
        "actions": [],
    }


def inspect_import_history(
    *,
    import_dir: Path = DEFAULT_IMPORT_DIR,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    target = Path(import_dir).absolute()
    if not target.exists():
        return {
            "schema": HISTORY_SCHEMA,
            "status": "verified",
            "history_state": "no_import_history",
            "updated_at": observed_now.isoformat(),
            "blocking_reason": "",
            "latest_import": {},
            "progress": {"current_evidence_verified": True, "claim_count": 0},
        }
    try:
        _private_directory(target, create=False)
        entries = sorted(target.iterdir(), key=lambda item: item.name)
        if len(entries) > MAX_IMPORT_FILES:
            raise ValueError("trust_candidate_import_history_too_large")
        claims_by_key: dict[
            str, tuple[dict[str, Any], Path, str]
        ] = {}
        results_by_key: dict[
            str, tuple[dict[str, Any], Path, str]
        ] = {}
        pattern = r"(claim|result)--(pqtrustintake_[0-9a-f]{24})--([0-9a-f]{64})\.json"
        for path in entries:
            match = re.fullmatch(pattern, path.name)
            if match is None:
                raise ValueError("trust_candidate_import_history_entry_invalid")
            kind, request_id, public_key_sha256 = match.groups()
            key = f"{request_id}--{public_key_sha256}"
            payload, _raw, digest = _private_object(
                path,
                field=f"trust_candidate_import_historical_{kind}",
            )
            rows = claims_by_key if kind == "claim" else results_by_key
            if key in rows:
                raise ValueError("trust_candidate_import_history_duplicate")
            rows[key] = (payload, path, digest)
        if set(results_by_key) - set(claims_by_key):
            raise ValueError("trust_candidate_import_result_without_claim")
        rows: list[dict[str, Any]] = []
        for key, (claim, claim_path, claim_digest) in claims_by_key.items():
            if not _verify_claim(claim):
                raise ValueError("trust_candidate_import_history_claim_invalid")
            scope = dict(claim.get("scope") or {})
            destination = Path(str(claim.get("destination_path") or "")).absolute()
            expected_destination = (
                Path(candidate_dir).absolute()
                / f"{scope.get('public_key_sha256')}.json"
            )
            if destination != expected_destination:
                raise ValueError("trust_candidate_import_history_destination_invalid")
            row = {
                "import_state": "claimed_without_result",
                "request_id": str(scope.get("request_id") or ""),
                "claim_id": str(claim.get("claim_id") or ""),
                "claim_sha256": claim_digest,
                "result_sha256": "",
                "public_key_sha256": str(
                    scope.get("public_key_sha256") or ""
                ),
                "claimed_at": str(claim.get("claimed_at") or ""),
                "blocking_reason": (
                    "trust_candidate_import_claim_without_result"
                ),
                "candidate_import_attempted": False,
                "candidate_imported": False,
                "public_key_candidate_recorded": False,
                "claim_path": str(claim_path),
                "result_path": "",
            }
            if key in results_by_key:
                result, result_path, result_digest = results_by_key[key]
                if not _verify_result(result, claim=claim):
                    raise ValueError("trust_candidate_import_history_result_invalid")
                imported = result.get("candidate_imported") is True
                if imported:
                    candidate_raw = read_stable_bytes(
                        destination,
                        maximum_bytes=MAX_SOURCE_BYTES,
                    )
                    if _sha256(candidate_raw) != result.get("destination_sha256"):
                        raise ValueError("trust_candidate_import_history_candidate_invalid")
                row.update(
                    {
                        "import_state": str(result.get("status") or ""),
                        "completed_at": str(result.get("completed_at") or ""),
                        "blocking_reason": str(result.get("blocking_reason") or ""),
                        "result_sha256": result_digest,
                        "candidate_import_attempted": True,
                        "candidate_imported": imported,
                        "public_key_candidate_recorded": imported,
                        "destination_path": str(destination),
                        "destination_sha256": str(
                            result.get("destination_sha256") or ""
                        ),
                        "result_path": str(result_path),
                    }
                )
            rows.append(row)
        rows.sort(
            key=lambda row: (
                _timestamp(row.get("claimed_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                str(row.get("claim_id") or ""),
            )
        )
        return {
            "schema": HISTORY_SCHEMA,
            "status": "verified",
            "history_state": "import_history",
            "updated_at": observed_now.isoformat(),
            "blocking_reason": "",
            "latest_import": rows[-1] if rows else {},
            "imports": rows,
            "progress": {
                "current_evidence_verified": True,
                "claim_count": len(claims_by_key),
                "result_count": len(results_by_key),
            },
        }
    except Exception:
        return {
            "schema": HISTORY_SCHEMA,
            "status": "blocked",
            "history_state": "blocked",
            "updated_at": observed_now.isoformat(),
            "blocking_reason": "trust_candidate_import_history_not_admissible",
            "latest_import": {},
            "progress": {"current_evidence_verified": False},
        }


def inspect_current_candidate_import(
    *,
    claim_verification_path: Path = claims.DEFAULT_VERIFICATION_PATH,
    trust_registry_path: Path = claims.DEFAULT_TRUST_REGISTRY_PATH,
    candidate_dir: Path = trust_intake.DEFAULT_CANDIDATE_DIR,
    intake_receipt_path: Path = trust_intake.DEFAULT_RECEIPT_PATH,
    intake_verification_path: Path = trust_intake.DEFAULT_VERIFICATION_PATH,
    import_dir: Path = DEFAULT_IMPORT_DIR,
    source_path: Path | None = None,
    source_discovery_dir: Path | None = DEFAULT_SOURCE_DISCOVERY_DIR,
    now: datetime | None = None,
    max_age_seconds: float = trust_intake.DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    intake_report = trust_intake.inspect_trust_intake_bundle(
        claim_verification_path=claim_verification_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        receipt_path=intake_receipt_path,
        verification_path=intake_verification_path,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    return inspect_candidate_import_readiness(
        intake_report,
        candidate_dir=candidate_dir,
        import_dir=import_dir,
        source_path=source_path,
        source_discovery_dir=source_discovery_dir,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or explicitly import one producer-owned public-key candidate; "
            "this never grants trust or edits the trust registry."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--inspect-source", type=Path)
    mode.add_argument("--import-source", type=Path)
    parser.add_argument(
        "--claim-verification",
        type=Path,
        default=claims.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument(
        "--trust-registry",
        type=Path,
        default=claims.DEFAULT_TRUST_REGISTRY_PATH,
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=trust_intake.DEFAULT_CANDIDATE_DIR,
    )
    parser.add_argument(
        "--intake-receipt",
        type=Path,
        default=trust_intake.DEFAULT_RECEIPT_PATH,
    )
    parser.add_argument(
        "--intake-verification",
        type=Path,
        default=trust_intake.DEFAULT_VERIFICATION_PATH,
    )
    parser.add_argument("--import-dir", type=Path, default=DEFAULT_IMPORT_DIR)
    parser.add_argument(
        "--source-discovery-dir",
        type=Path,
        default=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_SOURCE_DIR"
                )
                or DEFAULT_SOURCE_DISCOVERY_DIR
            ).strip()
        ),
    )
    parser.add_argument("--presentation-state", type=Path)
    parser.add_argument("--record-presentation", action="store_true")
    parser.add_argument("--expected-presentation-digest", default="")
    parser.add_argument(
        "--presentation-reminder-seconds",
        type=float,
        default=DEFAULT_PRESENTATION_REMINDER_SECONDS,
    )
    parser.add_argument("--expected-request-id", default="")
    parser.add_argument("--expected-semantic-request-sha256", default="")
    parser.add_argument("--expected-intake-verification-sha256", default="")
    parser.add_argument("--expected-source-sha256", default="")
    parser.add_argument("--expected-public-key-sha256", default="")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--import-method", default="")
    parser.add_argument("--evidence-ref", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=trust_intake.DEFAULT_MAX_AGE_SECONDS,
    )
    args = parser.parse_args(argv)
    if args.record_presentation and (
        args.presentation_state is None or args.import_source is not None
    ):
        parser.error(
            "--record-presentation requires an inspection mode and --presentation-state"
        )
    common = {
        "claim_verification_path": args.claim_verification,
        "trust_registry_path": args.trust_registry,
        "candidate_dir": args.candidate_dir,
        "intake_receipt_path": args.intake_receipt,
        "intake_verification_path": args.intake_verification,
        "import_dir": args.import_dir,
        "max_age_seconds": float(args.max_age_seconds),
    }
    if args.import_source is not None:
        result = import_current_candidate(
            source_path=args.import_source,
            expected_request_id=str(args.expected_request_id),
            expected_semantic_request_sha256=str(
                args.expected_semantic_request_sha256
            ),
            expected_intake_verification_sha256=str(
                args.expected_intake_verification_sha256
            ),
            expected_source_sha256=str(args.expected_source_sha256),
            expected_public_key_sha256=str(args.expected_public_key_sha256),
            operator_id=str(args.operator_id),
            import_method=str(args.import_method),
            evidence_ref=str(args.evidence_ref),
            confirmation=str(args.confirm),
            **common,
        )
    else:
        result = inspect_current_candidate_import(
            source_path=args.inspect_source,
            source_discovery_dir=args.source_discovery_dir,
            **common,
        )
        if args.presentation_state is not None:
            result = apply_candidate_import_presentation_state(
                result,
                state_path=args.presentation_state,
            )
            if args.record_presentation:
                result["presentation_receipt"] = (
                    record_candidate_import_presentation(
                        result,
                        state_path=args.presentation_state,
                        expected_presentation_digest=str(
                            args.expected_presentation_digest
                        ),
                        reminder_after_seconds=float(
                            args.presentation_reminder_seconds
                        ),
                    )
                )
    print(json.dumps(result, sort_keys=True))
    presentation_receipt = dict(result.get("presentation_receipt") or {})
    return 0 if (
        result.get("status") == "verified"
        and presentation_receipt.get("status") != "blocked"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
