"""Reference contract for controller-owned panorama operation evidence.

This module is not release authority.  The independently signed controller
package must vendor and digest-bind these exact bytes, provision the journal
and lock beneath its root-owned control directory, and invoke the functions in
its separately fenced write phase.  Candidate or application processes do not
gain authority by importing this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Mapping

from app.product import property_tour_ai_panorama_admission as admission_authority


OPERATION_JOURNAL_SCHEMA = (
    "propertyquarry.ai-panorama-install-operation-journal.v1"
)
OPERATION_JOURNAL_AUTHORITY = "propertyquarry-release-control"
OPERATION_JOURNAL_NAME = "operation-journal.v1.json"
OPERATION_JOURNAL_LOCK_NAME = "operation-journal.v1.lock"
OPERATION_JOURNAL_MODE = 0o600
MAX_OPERATION_ENTRIES = 100_000
MAX_OPERATION_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024
_GENESIS_DIGEST = "0" * 64
_EVENTS = frozenset(
    {
        "prepared",
        "committed",
        "failed-clean",
        "rolled-back",
        "recovery-required",
        "consumed-failed-clean",
    }
)
_TERMINAL_EVENTS = _EVENTS - {"prepared"}
_NORMAL_TERMINAL_EVENTS = _TERMINAL_EVENTS - {"consumed-failed-clean"}
_HISTORICAL_RECOVERY_EVIDENCE_SCHEMA = (
    "propertyquarry.prater-ai-panorama-recovery-evidence.v1"
)
_HISTORICAL_RECOVERY_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "version",
        "authority",
        "phase",
        "classification",
        "classification_basis",
        "prepared_entry_sha256",
        "prepared_evidence_sha256",
        "observed_target_manifest",
        "observed_target_manifest_sha256",
        "observed_target_identity",
        "observed_database_record_sha256",
        "observed_publication_binding_exact",
        "publication_binding_expected_before_sha256",
        "publication_binding_expected_after_sha256",
        "publication_binding_plan_status",
        "historical_consumption_binding",
        "database_mutation_performed",
        "public_target_mutation_performed",
        "private_values_redacted",
    }
)
_GOVERNED_RELEASE_CONTRACT = (
    "propertyquarry.prater_ai_panorama_governed_release.v1"
)
_GOVERNED_RELEASE_BASE_EVIDENCE_KEYS = frozenset(
    {
        "contract",
        "phase",
        "slug",
        "listing_url_sha256",
        "source_tree_sha256",
        "tour_sha256",
        "core_manifest_sha256",
        "materialization_receipt_sha256",
        "candidate_marker_sha256",
        "publication_record_sha256",
        "volume_profile_sha256",
        "public_tour_volume_name",
        "public_tour_mount_target",
        "target_manifest",
        "private_values_redacted",
    }
)
_GOVERNED_RELEASE_INSTALL_KEYS = frozenset(
    {
        "status",
        "already_installed",
        "source_tree_sha256",
        "source_tour_sha256",
        "publication_binding_status",
        "publication_binding_before_sha256",
        "publication_binding_after_sha256",
    }
)
_GOVERNED_RELEASE_BINDING_PREPARATION_KEYS = frozenset(
    {
        "status",
        "publication_binding_expected_before_sha256",
        "publication_binding_expected_after_sha256",
        "publication_binding_bound_at",
        "database_mutation_performed",
        "private_values_redacted",
    }
)
_HISTORICAL_CONSUMPTION_BINDING_KEYS = frozenset(
    {
        "ledger_instance_id",
        "ledger_sequence",
        "ledger_entry_sha256",
    }
)


class AiPanoramaOperationJournalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise AiPanoramaOperationJournalError(code)


@dataclass(frozen=True, slots=True)
class AiPanoramaOperationHandle:
    operation_id: str
    permit_sha256: str
    request_id_sha256: str
    nonce_sha256: str
    context_sha256: str
    ledger_instance_id: str
    ledger_sequence: int
    ledger_entry_sha256: str
    prepared_entry_sha256: str
    admission: admission_authority.VerifiedAiPanoramaInstallAdmission


@dataclass(frozen=True, slots=True)
class AiPanoramaRecoveredOperationObservation:
    """Read-only projection of one exact unterminated prepared operation."""

    operation_id: str
    permit_sha256: str
    request_id_sha256: str
    nonce_sha256: str
    context_sha256: str
    prepared_entry_sha256: str
    prepared_evidence_sha256: str
    prepared_evidence: Mapping[str, object]
    recovery: admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence


@dataclass(frozen=True, slots=True)
class AiPanoramaHistoricalOperationObservation:
    """Non-authorizing historical projection for deterministic recovery."""

    state: str
    operation_id: str
    permit_sha256: str
    request_id_sha256: str
    nonce_sha256: str
    context_sha256: str
    prepared_entry_sha256: str
    prepared_evidence_sha256: str
    prepared_evidence: Mapping[str, object]
    terminal_event: str
    terminal_entry_sha256: str
    terminal_evidence_sha256: str
    terminal_evidence: Mapping[str, object]
    historical_consumption: (
        admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AiPanoramaOperationJournalError(
            "ai_panorama_operation_evidence_invalid"
        ) from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(code)
    normalized = value
    if (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        _fail(code)
    return normalized


def _evidence(value: Mapping[str, object]) -> tuple[dict[str, object], str]:
    if type(value) is not dict:
        _fail("ai_panorama_operation_evidence_invalid")
    encoded = _canonical_bytes(value)
    if not encoded or len(encoded) > MAX_EVIDENCE_BYTES:
        _fail("ai_panorama_operation_evidence_invalid")
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise AiPanoramaOperationJournalError(
            "ai_panorama_operation_evidence_invalid"
        ) from exc
    if type(decoded) is not dict:
        _fail("ai_panorama_operation_evidence_invalid")
    return decoded, _sha256(encoded)


def _entry_digest(value: Mapping[str, object]) -> str:
    return _sha256(
        b"propertyquarry.ai-panorama-install-operation-entry.v1\0"
        + _canonical_bytes(value)
    )


def _validate_journal(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "authority",
        "instance_id",
        "sequence",
        "tip_sha256",
        "entries",
    }:
        _fail("ai_panorama_operation_journal_invalid")
    if (
        value["schema"] != OPERATION_JOURNAL_SCHEMA
        or value["authority"] != OPERATION_JOURNAL_AUTHORITY
        or type(value["instance_id"]) is not str
        or len(value["instance_id"]) != 32
        or any(character not in "0123456789abcdef" for character in value["instance_id"])
        or type(value["sequence"]) is not int
        or value["sequence"] < 0
        or value["sequence"] > MAX_OPERATION_ENTRIES
        or type(value["entries"]) is not list
        or len(value["entries"]) != value["sequence"]
    ):
        _fail("ai_panorama_operation_journal_invalid")
    previous = _GENESIS_DIGEST
    operation_states: dict[
        str,
        tuple[str, tuple[str, str, str, str]],
    ] = {}
    for sequence, raw_entry in enumerate(value["entries"], start=1):
        if type(raw_entry) is not dict or set(raw_entry) != {
            "sequence",
            "operation_id",
            "event",
            "permit_sha256",
            "request_id_sha256",
            "nonce_sha256",
            "context_sha256",
            "evidence",
            "evidence_sha256",
            "previous_entry_sha256",
            "entry_sha256",
        }:
            _fail("ai_panorama_operation_journal_invalid")
        event = raw_entry["event"]
        operation_id = _digest(
            raw_entry["operation_id"],
            "ai_panorama_operation_journal_invalid",
        )
        if (
            type(raw_entry["sequence"]) is not int
            or raw_entry["sequence"] != sequence
            or event not in _EVENTS
            or raw_entry["previous_entry_sha256"] != previous
        ):
            _fail("ai_panorama_operation_journal_invalid")
        for key in (
            "permit_sha256",
            "request_id_sha256",
            "nonce_sha256",
            "context_sha256",
            "evidence_sha256",
            "previous_entry_sha256",
            "entry_sha256",
        ):
            _digest(raw_entry[key], "ai_panorama_operation_journal_invalid")
        bindings = (
            str(raw_entry["permit_sha256"]),
            str(raw_entry["request_id_sha256"]),
            str(raw_entry["nonce_sha256"]),
            str(raw_entry["context_sha256"]),
        )
        expected_operation_id = _sha256(
            b"propertyquarry.ai-panorama-install-operation.v1\0"
            + bytes.fromhex(bindings[0])
            + bytes.fromhex(bindings[1])
            + bytes.fromhex(bindings[2])
            + bytes.fromhex(bindings[3])
        )
        if operation_id != expected_operation_id:
            _fail("ai_panorama_operation_journal_invalid")
        evidence, evidence_sha256 = _evidence(raw_entry["evidence"])
        if (
            evidence != raw_entry["evidence"]
            or evidence_sha256 != raw_entry["evidence_sha256"]
        ):
            _fail("ai_panorama_operation_journal_invalid")
        unsigned = dict(raw_entry)
        claimed_entry_sha256 = unsigned.pop("entry_sha256")
        if claimed_entry_sha256 != _entry_digest(unsigned):
            _fail("ai_panorama_operation_journal_invalid")
        previous_state = operation_states.get(operation_id)
        if event == "prepared" and previous_state is not None:
            _fail("ai_panorama_operation_journal_transition_invalid")
        if event in _NORMAL_TERMINAL_EVENTS and (
            previous_state is None
            or previous_state[0] != "prepared"
            or previous_state[1] != bindings
        ):
            _fail("ai_panorama_operation_journal_transition_invalid")
        if (
            event == "consumed-failed-clean"
            and previous_state is not None
        ):
            _fail("ai_panorama_operation_journal_transition_invalid")
        operation_states[operation_id] = (event, bindings)
        previous = claimed_entry_sha256
    if value["tip_sha256"] != previous:
        _fail("ai_panorama_operation_journal_invalid")
    return dict(value)


def _open_locked_journal() -> tuple[
    int,
    int,
    dict[str, object],
    tuple[int, ...],
    int,
    int,
]:
    control_descriptor = admission_authority._open_control_root()
    lock_descriptor = -1
    try:
        control_details = os.fstat(control_descriptor)
        control_device = int(control_details.st_dev)
        control_mount_id = admission_authority._descriptor_mount_id(
            control_descriptor,
            code="ai_panorama_operation_journal_root_invalid",
        )
        lock_stable = admission_authority._read_relative_regular(
            control_descriptor,
            OPERATION_JOURNAL_LOCK_NAME,
            code="ai_panorama_operation_journal_lock_invalid",
            maximum_bytes=64,
            required_uid=admission_authority._CONTROLLER_PATHS.required_uid,
            exact_mode=OPERATION_JOURNAL_MODE,
            required_device=control_device,
            required_mount_id=control_mount_id,
        )
        lock_descriptor = os.open(
            OPERATION_JOURNAL_LOCK_NAME,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=control_descriptor,
        )
        lock_details = os.fstat(lock_descriptor)
        if (
            admission_authority._file_identity(lock_details)
            != lock_stable.identity
            or int(lock_details.st_dev) != control_device
            or admission_authority._descriptor_mount_id(
                lock_descriptor,
                code="ai_panorama_operation_journal_lock_invalid",
            )
            != control_mount_id
        ):
            _fail("ai_panorama_operation_journal_lock_invalid")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        locked_details = os.fstat(lock_descriptor)
        current_lock = admission_authority._read_relative_regular(
            control_descriptor,
            OPERATION_JOURNAL_LOCK_NAME,
            code="ai_panorama_operation_journal_lock_invalid",
            maximum_bytes=64,
            required_uid=admission_authority._CONTROLLER_PATHS.required_uid,
            exact_mode=OPERATION_JOURNAL_MODE,
            required_device=control_device,
            required_mount_id=control_mount_id,
        )
        if (
            admission_authority._file_identity(locked_details)
            != lock_stable.identity
            or current_lock.identity != lock_stable.identity
            or admission_authority._descriptor_mount_id(
                lock_descriptor,
                code="ai_panorama_operation_journal_lock_invalid",
            )
            != control_mount_id
        ):
            _fail("ai_panorama_operation_journal_lock_invalid")
        stable = admission_authority._read_relative_regular(
            control_descriptor,
            OPERATION_JOURNAL_NAME,
            code="ai_panorama_operation_journal_unavailable",
            maximum_bytes=MAX_OPERATION_JOURNAL_BYTES,
            required_uid=admission_authority._CONTROLLER_PATHS.required_uid,
            exact_mode=OPERATION_JOURNAL_MODE,
            required_device=control_device,
            required_mount_id=control_mount_id,
        )
        try:
            payload = json.loads(stable.data.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise AiPanoramaOperationJournalError(
                "ai_panorama_operation_journal_invalid"
            ) from exc
        return (
            control_descriptor,
            lock_descriptor,
            _validate_journal(payload),
            stable.identity,
            control_device,
            control_mount_id,
        )
    except Exception:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        os.close(control_descriptor)
        raise


def _require_relative_identity(
    control_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, ...],
    required_device: int,
    required_mount_id: int,
    code: str,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=control_descriptor,
        )
        descriptor_details = os.fstat(descriptor)
        path_details = os.stat(
            name,
            dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        if (
            admission_authority._file_identity(descriptor_details)
            != expected_identity
            or admission_authority._file_identity(path_details)
            != expected_identity
            or int(descriptor_details.st_dev) != required_device
            or admission_authority._descriptor_mount_id(
                descriptor,
                code=code,
            )
            != required_mount_id
        ):
            _fail(code)
    except AiPanoramaOperationJournalError:
        raise
    except OSError as exc:
        raise AiPanoramaOperationJournalError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_replaced_file_identity(
    control_descriptor: int,
    name: str,
    *,
    expected_file_key: tuple[int, int],
    expected_size: int,
    required_device: int,
    required_mount_id: int,
    code: str,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=control_descriptor,
        )
        descriptor_details = os.fstat(descriptor)
        path_details = os.stat(
            name,
            dir_fd=control_descriptor,
            follow_symlinks=False,
        )
        descriptor_identity = admission_authority._file_identity(
            descriptor_details
        )
        if (
            descriptor_identity
            != admission_authority._file_identity(path_details)
            or (int(descriptor_details.st_dev), int(descriptor_details.st_ino))
            != expected_file_key
            or int(descriptor_details.st_dev) != required_device
            or not stat.S_ISREG(descriptor_details.st_mode)
            or descriptor_details.st_nlink != 1
            or descriptor_details.st_uid
            != admission_authority._CONTROLLER_PATHS.required_uid
            or stat.S_IMODE(descriptor_details.st_mode)
            != OPERATION_JOURNAL_MODE
            or int(descriptor_details.st_size) != expected_size
            or admission_authority._descriptor_mount_id(
                descriptor,
                code=code,
            )
            != required_mount_id
        ):
            _fail(code)
    except AiPanoramaOperationJournalError:
        raise
    except OSError as exc:
        raise AiPanoramaOperationJournalError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_journal(
    control_descriptor: int,
    value: Mapping[str, object],
    expected_identity: tuple[int, ...],
    *,
    required_device: int,
    required_mount_id: int,
) -> None:
    encoded = _canonical_bytes(value) + b"\n"
    temporary_name = ""
    temporary_descriptor = -1
    replacement_file_key: tuple[int, int] | None = None
    try:
        if (
            int(os.fstat(control_descriptor).st_dev) != required_device
            or admission_authority._descriptor_mount_id(
                control_descriptor,
                code="ai_panorama_operation_journal_changed",
            )
            != required_mount_id
        ):
            _fail("ai_panorama_operation_journal_changed")
        _require_relative_identity(
            control_descriptor,
            OPERATION_JOURNAL_NAME,
            expected_identity=expected_identity,
            required_device=required_device,
            required_mount_id=required_mount_id,
            code="ai_panorama_operation_journal_changed",
        )
        for _ in range(32):
            temporary_name = (
                f".{OPERATION_JOURNAL_NAME}.tmp-{secrets.token_hex(8)}"
            )
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    OPERATION_JOURNAL_MODE,
                    dir_fd=control_descriptor,
                )
            except FileExistsError:
                temporary_name = ""
                continue
            break
        if temporary_descriptor < 0 or not temporary_name:
            _fail("ai_panorama_operation_journal_write_failed")
        offset = 0
        while offset < len(encoded):
            written = os.write(temporary_descriptor, encoded[offset:])
            if written <= 0:
                _fail("ai_panorama_operation_journal_write_failed")
            offset += written
        os.fchmod(temporary_descriptor, OPERATION_JOURNAL_MODE)
        temporary_details = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(temporary_details.st_mode)
            or temporary_details.st_nlink != 1
            or temporary_details.st_uid
            != admission_authority._CONTROLLER_PATHS.required_uid
            or stat.S_IMODE(temporary_details.st_mode)
            != OPERATION_JOURNAL_MODE
            or int(temporary_details.st_dev) != required_device
            or admission_authority._descriptor_mount_id(
                temporary_descriptor,
                code="ai_panorama_operation_journal_write_failed",
            )
            != required_mount_id
        ):
            _fail("ai_panorama_operation_journal_write_failed")
        replacement_file_key = (
            int(temporary_details.st_dev),
            int(temporary_details.st_ino),
        )
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        _require_relative_identity(
            control_descriptor,
            OPERATION_JOURNAL_NAME,
            expected_identity=expected_identity,
            required_device=required_device,
            required_mount_id=required_mount_id,
            code="ai_panorama_operation_journal_changed",
        )
        os.replace(
            temporary_name,
            OPERATION_JOURNAL_NAME,
            src_dir_fd=control_descriptor,
            dst_dir_fd=control_descriptor,
        )
        temporary_name = ""
        if replacement_file_key is None:
            _fail("ai_panorama_operation_journal_write_failed")
        _require_replaced_file_identity(
            control_descriptor,
            OPERATION_JOURNAL_NAME,
            expected_file_key=replacement_file_key,
            expected_size=len(encoded),
            required_device=required_device,
            required_mount_id=required_mount_id,
            code="ai_panorama_operation_journal_write_failed",
        )
        os.fsync(control_descriptor)
    except AiPanoramaOperationJournalError:
        raise
    except OSError as exc:
        raise AiPanoramaOperationJournalError(
            "ai_panorama_operation_journal_write_failed"
        ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=control_descriptor)
            except FileNotFoundError:
                pass


def _append_locked(
    *,
    control_descriptor: int,
    journal: Mapping[str, object],
    journal_identity: tuple[int, ...],
    control_device: int,
    control_mount_id: int,
    operation_id: str,
    event: str,
    permit_sha256: str,
    request_id_sha256: str,
    nonce_sha256: str,
    context_sha256: str,
    evidence: Mapping[str, object],
) -> str:
    evidence_value, evidence_sha256 = _evidence(evidence)
    unsigned = {
        "sequence": int(journal["sequence"]) + 1,
        "operation_id": operation_id,
        "event": event,
        "permit_sha256": permit_sha256,
        "request_id_sha256": request_id_sha256,
        "nonce_sha256": nonce_sha256,
        "context_sha256": context_sha256,
        "evidence": evidence_value,
        "evidence_sha256": evidence_sha256,
        "previous_entry_sha256": journal["tip_sha256"],
    }
    entry = {**unsigned, "entry_sha256": _entry_digest(unsigned)}
    updated = {
        **journal,
        "sequence": entry["sequence"],
        "tip_sha256": entry["entry_sha256"],
        "entries": [*journal["entries"], entry],
    }
    _validate_journal(updated)
    _replace_journal(
        control_descriptor,
        updated,
        journal_identity,
        required_device=control_device,
        required_mount_id=control_mount_id,
    )
    return str(entry["entry_sha256"])


def _append(
    *,
    operation_id: str,
    event: str,
    permit_sha256: str,
    request_id_sha256: str,
    nonce_sha256: str,
    context_sha256: str,
    evidence: Mapping[str, object],
) -> str:
    (
        control_descriptor,
        lock_descriptor,
        journal,
        journal_identity,
        control_device,
        control_mount_id,
    ) = _open_locked_journal()
    try:
        return _append_locked(
            control_descriptor=control_descriptor,
            journal=journal,
            journal_identity=journal_identity,
            control_device=control_device,
            control_mount_id=control_mount_id,
            operation_id=operation_id,
            event=event,
            permit_sha256=permit_sha256,
            request_id_sha256=request_id_sha256,
            nonce_sha256=nonce_sha256,
            context_sha256=context_sha256,
            evidence=evidence,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def begin_ai_panorama_install_operation(
    verified_admission: admission_authority.VerifiedAiPanoramaInstallAdmission,
    *,
    evidence: Mapping[str, object],
) -> AiPanoramaOperationHandle:
    verified = admission_authority.revalidate_ai_panorama_install_admission(
        verified_admission,
        require_consumed=True,
    )
    request_id_sha256 = _sha256(verified.request_id.encode("utf-8"))
    nonce_sha256 = _sha256(verified.nonce.encode("ascii"))
    operation_id = _sha256(
        b"propertyquarry.ai-panorama-install-operation.v1\0"
        + bytes.fromhex(verified.permit_sha256)
        + bytes.fromhex(request_id_sha256)
        + bytes.fromhex(nonce_sha256)
        + bytes.fromhex(verified._context_sha256)
    )
    prepared_evidence = dict(evidence)
    if "admission_recovery_binding" in prepared_evidence:
        _fail("ai_panorama_operation_evidence_invalid")
    prepared_evidence["admission_recovery_binding"] = {
        "ledger_instance_id": verified._ledger_instance_id,
        "ledger_sequence": verified._ledger_sequence,
        "ledger_entry_sha256": verified._ledger_entry_sha256,
    }
    entry_sha256 = _append(
        operation_id=operation_id,
        event="prepared",
        permit_sha256=verified.permit_sha256,
        request_id_sha256=request_id_sha256,
        nonce_sha256=nonce_sha256,
        context_sha256=verified._context_sha256,
        evidence=prepared_evidence,
    )
    return AiPanoramaOperationHandle(
        operation_id=operation_id,
        permit_sha256=verified.permit_sha256,
        request_id_sha256=request_id_sha256,
        nonce_sha256=nonce_sha256,
        context_sha256=verified._context_sha256,
        ledger_instance_id=verified._ledger_instance_id,
        ledger_sequence=verified._ledger_sequence,
        ledger_entry_sha256=verified._ledger_entry_sha256,
        prepared_entry_sha256=entry_sha256,
        admission=verified,
    )


def finish_ai_panorama_install_operation(
    handle: AiPanoramaOperationHandle,
    *,
    event: str,
    evidence: Mapping[str, object],
) -> str:
    if (
        type(handle) is not AiPanoramaOperationHandle
        or event not in _NORMAL_TERMINAL_EVENTS
    ):
        _fail("ai_panorama_operation_handle_invalid")
    recovery = admission_authority.revalidate_ai_panorama_install_recovery(
        handle.admission
    )
    if (
        recovery.permit_sha256 != handle.permit_sha256
        or recovery.request_id_sha256 != handle.request_id_sha256
        or recovery.nonce_sha256 != handle.nonce_sha256
        or recovery.context_sha256 != handle.context_sha256
        or recovery.ledger_instance_id != handle.ledger_instance_id
        or recovery.ledger_sequence != handle.ledger_sequence
        or recovery.ledger_entry_sha256 != handle.ledger_entry_sha256
    ):
        _fail("ai_panorama_operation_handle_invalid")
    return _append(
        operation_id=handle.operation_id,
        event=event,
        permit_sha256=handle.permit_sha256,
        request_id_sha256=handle.request_id_sha256,
        nonce_sha256=handle.nonce_sha256,
        context_sha256=handle.context_sha256,
        evidence=evidence,
    )


def finish_recovered_ai_panorama_install_operation(
    permit_relpath: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    *,
    event: str,
    evidence: Mapping[str, object],
) -> str:
    """Terminalize one durable prepared operation after process loss."""

    if (
        type(permit_relpath) is not str
        or event not in _NORMAL_TERMINAL_EVENTS
    ):
        _fail("ai_panorama_operation_recovery_invalid")
    recovery = admission_authority.recover_ai_panorama_install_consumption(
        permit_relpath,
        expected,
    )
    if (
        type(recovery)
        is not admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence
    ):
        _fail("ai_panorama_operation_recovery_invalid")
    (
        control_descriptor,
        lock_descriptor,
        journal,
        journal_identity,
        control_device,
        control_mount_id,
    ) = _open_locked_journal()
    try:
        prepared = _matching_recovered_prepared_entry(
            journal,
            recovery,
        )
        return _append_locked(
            control_descriptor=control_descriptor,
            journal=journal,
            journal_identity=journal_identity,
            control_device=control_device,
            control_mount_id=control_mount_id,
            operation_id=str(prepared["operation_id"]),
            event=event,
            permit_sha256=recovery.permit_sha256,
            request_id_sha256=recovery.request_id_sha256,
            nonce_sha256=recovery.nonce_sha256,
            context_sha256=recovery.context_sha256,
            evidence=evidence,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def _matching_recovered_prepared_entry(
    journal: Mapping[str, object],
    recovery: admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence,
) -> Mapping[str, object]:
    matches: list[Mapping[str, object]] = []
    for entry in journal["entries"]:  # type: ignore[index]
        if (
            entry["event"] != "prepared"
            or entry["permit_sha256"] != recovery.permit_sha256
            or entry["request_id_sha256"] != recovery.request_id_sha256
            or entry["nonce_sha256"] != recovery.nonce_sha256
            or entry["context_sha256"] != recovery.context_sha256
        ):
            continue
        prepared_evidence = entry.get("evidence")
        if not isinstance(prepared_evidence, Mapping):
            continue
        if prepared_evidence.get("admission_recovery_binding") != {
            "ledger_instance_id": recovery.ledger_instance_id,
            "ledger_sequence": recovery.ledger_sequence,
            "ledger_entry_sha256": recovery.ledger_entry_sha256,
        }:
            continue
        operation_id = str(entry["operation_id"])
        if any(
            later["operation_id"] == operation_id
            and later["event"] in _TERMINAL_EVENTS
            for later in journal["entries"]  # type: ignore[index]
        ):
            continue
        matches.append(entry)
    if len(matches) != 1:
        _fail("ai_panorama_operation_recovery_invalid")
    return matches[0]


def load_recovered_ai_panorama_install_operation(
    permit_relpath: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
) -> AiPanoramaRecoveredOperationObservation:
    """Load one exact prepared operation without accepting caller evidence."""

    recovery = admission_authority.recover_ai_panorama_install_consumption(
        permit_relpath,
        expected,
    )
    if (
        type(recovery)
        is not admission_authority.VerifiedAiPanoramaInstallRecoveryEvidence
    ):
        _fail("ai_panorama_operation_recovery_invalid")
    (
        control_descriptor,
        lock_descriptor,
        journal,
        _journal_identity,
        _control_device,
        _control_mount_id,
    ) = _open_locked_journal()
    try:
        prepared = _matching_recovered_prepared_entry(
            journal,
            recovery,
        )
        evidence = prepared["evidence"]
        if not isinstance(evidence, Mapping):
            _fail("ai_panorama_operation_recovery_invalid")
        return AiPanoramaRecoveredOperationObservation(
            operation_id=str(prepared["operation_id"]),
            permit_sha256=recovery.permit_sha256,
            request_id_sha256=recovery.request_id_sha256,
            nonce_sha256=recovery.nonce_sha256,
            context_sha256=recovery.context_sha256,
            prepared_entry_sha256=str(prepared["entry_sha256"]),
            prepared_evidence_sha256=str(prepared["evidence_sha256"]),
            prepared_evidence=dict(evidence),
            recovery=recovery,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def _historical_operation_id(
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
) -> str:
    if (
        type(proof)
        is not admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof
    ):
        _fail("ai_panorama_operation_historical_proof_invalid")
    return _sha256(
        b"propertyquarry.ai-panorama-install-operation.v1\0"
        + bytes.fromhex(proof.permit_sha256)
        + bytes.fromhex(proof.request_id_sha256)
        + bytes.fromhex(proof.nonce_sha256)
        + bytes.fromhex(proof.context_sha256)
    )


def _historical_consumption_binding(
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
) -> dict[str, object]:
    binding = {
        "ledger_instance_id": proof.ledger_instance_id,
        "ledger_sequence": proof.ledger_sequence,
        "ledger_entry_sha256": proof.ledger_entry_sha256,
    }
    if (
        set(binding) != set(_HISTORICAL_CONSUMPTION_BINDING_KEYS)
        or type(binding["ledger_instance_id"]) is not str
        or len(binding["ledger_instance_id"]) != 32
        or any(
            character not in "0123456789abcdef"
            for character in binding["ledger_instance_id"]
        )
        or type(binding["ledger_sequence"]) is not int
        or int(binding["ledger_sequence"]) < 1
    ):
        _fail("ai_panorama_operation_historical_proof_invalid")
    _digest(
        binding["ledger_entry_sha256"],
        "ai_panorama_operation_historical_proof_invalid",
    )
    return binding


def _validated_target_manifest(
    value: object,
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    if type(value) is not dict:
        _fail("ai_panorama_operation_evidence_invalid")
    state = value.get("state")
    absent_keys = {
        "state",
        "target_relpath",
        "public_root_device",
        "public_root_inode",
        "reserved_entry_count",
        "reserved_entries_sha256",
    }
    present_keys = absent_keys | {
        "target_device",
        "target_inode",
        "tree_sha256",
        "tour_private_sha256",
        "file_count",
        "directory_count",
        "total_bytes",
    }
    if (
        state not in {"absent", "present"}
        or set(value)
        != (absent_keys if state == "absent" else present_keys)
        or value.get("target_relpath") != expected.expected_slug
        or type(value.get("public_root_device")) is not int
        or type(value.get("public_root_inode")) is not int
        or value["public_root_device"]
        != expected.public_tour_root_device
        or value["public_root_inode"]
        != expected.public_tour_root_inode
        or type(value.get("reserved_entry_count")) is not int
        or int(value["reserved_entry_count"]) < 0
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    _digest(
        value.get("reserved_entries_sha256"),
        "ai_panorama_operation_evidence_invalid",
    )
    if state == "present":
        for field in ("target_device", "target_inode"):
            if (
                type(value.get(field)) is not int
                or int(value[field]) < 1
            ):
                _fail("ai_panorama_operation_evidence_invalid")
        for field in ("file_count", "total_bytes"):
            if (
                type(value.get(field)) is not int
                or int(value[field]) < 0
            ):
                _fail("ai_panorama_operation_evidence_invalid")
        if (
            type(value.get("directory_count")) is not int
            or int(value["directory_count"]) < 1
        ):
            _fail("ai_panorama_operation_evidence_invalid")
        _digest(
            value.get("tree_sha256"),
            "ai_panorama_operation_evidence_invalid",
        )
        private_sha256 = value.get("tour_private_sha256")
        if private_sha256 != "":
            _digest(
                private_sha256,
                "ai_panorama_operation_evidence_invalid",
            )
    return dict(value)


def _validated_target_identity(
    value: object,
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value)
        != {
            "state",
            "source_tree_sha256",
            "source_tour_sha256",
            "core_manifest_sha256",
            "public_root_device",
            "public_root_inode",
            "private_values_redacted",
        }
        or value.get("state") not in {"absent", "exact"}
        or (
            value.get("state") == "absent"
            and manifest.get("state") != "absent"
        )
        or (
            value.get("state") == "exact"
            and manifest.get("state") != "present"
        )
        or value.get("source_tree_sha256")
        != expected.expected_source_tree_sha256
        or value.get("source_tour_sha256")
        != expected.expected_tour_sha256
        or value.get("core_manifest_sha256")
        != expected.expected_core_manifest_sha256
        or value.get("public_root_device")
        != expected.public_tour_root_device
        or value.get("public_root_inode")
        != expected.public_tour_root_inode
        or value.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    if (
        value["state"] == "exact"
        and not manifest.get("tour_private_sha256")
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    return dict(value)


def _validated_governed_release_base(
    evidence: Mapping[str, object],
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    phase: str,
    extra_keys: set[str],
) -> dict[str, object]:
    if (
        type(evidence) is not dict
        or set(evidence)
        != set(_GOVERNED_RELEASE_BASE_EVIDENCE_KEYS) | extra_keys
        or evidence.get("contract") != _GOVERNED_RELEASE_CONTRACT
        or evidence.get("phase") != phase
        or evidence.get("slug") != expected.expected_slug
        or evidence.get("listing_url_sha256")
        != _sha256(expected.listing_url.encode("utf-8"))
        or evidence.get("source_tree_sha256")
        != expected.expected_source_tree_sha256
        or evidence.get("tour_sha256")
        != expected.expected_tour_sha256
        or evidence.get("core_manifest_sha256")
        != expected.expected_core_manifest_sha256
        or evidence.get("materialization_receipt_sha256")
        != expected.expected_materialization_receipt_sha256
        or evidence.get("candidate_marker_sha256")
        != expected.expected_candidate_marker_sha256
        or evidence.get("publication_record_sha256")
        != expected.expected_publication_record_sha256
        or evidence.get("volume_profile_sha256")
        != expected.volume_profile_sha256
        or evidence.get("public_tour_volume_name")
        != admission_authority.CANONICAL_PUBLIC_TOUR_VOLUME_NAME
        or evidence.get("public_tour_mount_target")
        != admission_authority.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        or evidence.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    _validated_target_manifest(
        evidence.get("target_manifest"),
        expected=expected,
    )
    return dict(evidence)


def _validated_prepared_governed_release_evidence(
    evidence: Mapping[str, object],
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
) -> tuple[dict[str, object], dict[str, object]]:
    value = _validated_governed_release_base(
        evidence,
        expected=expected,
        phase="prepared",
        extra_keys={
            "publication_binding_preparation",
            "admission_recovery_binding",
        },
    )
    preparation = value.get("publication_binding_preparation")
    if (
        type(preparation) is not dict
        or set(preparation)
        != set(_GOVERNED_RELEASE_BINDING_PREPARATION_KEYS)
        or preparation.get("status")
        not in {"change-required", "already-bound"}
        or preparation.get(
            "publication_binding_expected_before_sha256"
        )
        != expected.expected_publication_record_sha256
        or type(preparation.get("publication_binding_bound_at"))
        is not str
        or not preparation["publication_binding_bound_at"]
        or preparation.get("database_mutation_performed") is not False
        or preparation.get("private_values_redacted") is not True
        or value.get("admission_recovery_binding")
        != _historical_consumption_binding(proof)
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    after = preparation.get(
        "publication_binding_expected_after_sha256"
    )
    _digest(after, "ai_panorama_operation_evidence_invalid")
    changed = not secrets.compare_digest(
        expected.expected_publication_record_sha256,
        str(after),
    )
    if (preparation["status"] == "change-required") is not changed:
        _fail("ai_panorama_operation_evidence_invalid")
    return (
        dict(value["target_manifest"]),  # type: ignore[arg-type]
        dict(preparation),
    )


def _validated_governed_release_install(
    value: object,
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    preparation: Mapping[str, object],
) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != set(_GOVERNED_RELEASE_INSTALL_KEYS)
        or value.get("status") not in {"installed", "already_installed"}
        or type(value.get("already_installed")) is not bool
        or (
            value.get("status") == "already_installed"
        )
        is not value.get("already_installed")
        or value.get("source_tree_sha256")
        != expected.expected_source_tree_sha256
        or value.get("source_tour_sha256")
        != expected.expected_tour_sha256
        or value.get("publication_binding_status")
        not in {"applied", "already_bound"}
        or value.get("publication_binding_before_sha256")
        != preparation.get(
            "publication_binding_expected_before_sha256"
        )
        or value.get("publication_binding_after_sha256")
        != preparation.get(
            "publication_binding_expected_after_sha256"
        )
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    return dict(value)


def _validated_standard_terminal_evidence(
    evidence: Mapping[str, object],
    *,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
    operation: AiPanoramaHistoricalOperationObservation,
    event: str,
) -> dict[str, object]:
    if operation.prepared_evidence_sha256 == "":
        _fail("ai_panorama_operation_historical_state_invalid")
    baseline, preparation = _validated_prepared_governed_release_evidence(
        operation.prepared_evidence,
        expected=expected,
        proof=proof,
    )
    if event == "committed":
        extras = {"install"}
    elif event in {"failed-clean", "rolled-back"}:
        extras = {"error_code", "publication_outcome"}
    elif event == "recovery-required":
        if type(evidence) is not dict:
            _fail("ai_panorama_operation_evidence_invalid")
        keys = set(evidence)
        with_outcome = set(_GOVERNED_RELEASE_BASE_EVIDENCE_KEYS) | {
            "error_code",
            "publication_outcome",
        }
        with_install = set(_GOVERNED_RELEASE_BASE_EVIDENCE_KEYS) | {
            "install",
            "error_code",
        }
        if keys == with_outcome:
            extras = {"error_code", "publication_outcome"}
        elif keys == with_install:
            extras = {"install", "error_code"}
        else:
            _fail("ai_panorama_operation_evidence_invalid")
    else:
        _fail("ai_panorama_operation_historical_state_invalid")
    value = _validated_governed_release_base(
        evidence,
        expected=expected,
        phase=event,
        extra_keys=extras,
    )
    manifest = value["target_manifest"]
    if not isinstance(manifest, Mapping):
        _fail("ai_panorama_operation_evidence_invalid")
    if "install" in extras:
        _validated_governed_release_install(
            value.get("install"),
            expected=expected,
            preparation=preparation,
        )
    if "error_code" in extras and (
        type(value.get("error_code")) is not str
        or not value["error_code"]
        or len(str(value["error_code"])) > 128
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    if "publication_outcome" in extras and value.get(
        "publication_outcome"
    ) not in {"uncommitted", "ambiguous", "unknown"}:
        _fail("ai_panorama_operation_evidence_invalid")
    if event == "committed" and (
        manifest.get("state") != "present"
        or manifest.get("reserved_entry_count") != 0
        or not manifest.get("tour_private_sha256")
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    if event in {"failed-clean", "rolled-back"} and (
        value.get("publication_outcome") != "uncommitted"
        or manifest != baseline
        or manifest.get("reserved_entry_count") != 0
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    return value


def _validated_historical_recovery_evidence(
    evidence: Mapping[str, object],
    *,
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
    operation: AiPanoramaHistoricalOperationObservation,
    event: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    if type(evidence) is not dict or set(evidence) != set(
        _HISTORICAL_RECOVERY_EVIDENCE_KEYS
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    expected_classification = (
        "failed-clean" if event == "consumed-failed-clean" else event
    )
    expected_prepared_entry = (
        operation.prepared_entry_sha256 or "genesis"
    )
    expected_prepared_evidence = (
        operation.prepared_evidence_sha256 or "genesis"
    )
    historical_binding = _historical_consumption_binding(proof)
    target_manifest = _validated_target_manifest(
        evidence.get("observed_target_manifest"),
        expected=expected,
    )
    target_identity = _validated_target_identity(
        evidence.get("observed_target_identity"),
        expected=expected,
        manifest=target_manifest,
    )
    if event not in {
        "consumed-failed-clean",
        "committed",
        "failed-clean",
        "recovery-required",
    }:
        _fail("ai_panorama_operation_evidence_invalid")
    if (
        evidence.get("schema") != _HISTORICAL_RECOVERY_EVIDENCE_SCHEMA
        or evidence.get("version") != 1
        or evidence.get("authority") != "propertyquarry-release-control"
        or evidence.get("phase") != event
        or evidence.get("classification") != expected_classification
        or type(evidence.get("classification_basis")) is not str
        or not str(evidence["classification_basis"])
        or evidence.get("prepared_entry_sha256")
        != expected_prepared_entry
        or evidence.get("prepared_evidence_sha256")
        != expected_prepared_evidence
        or evidence.get("historical_consumption_binding")
        != historical_binding
        or type(evidence.get("observed_publication_binding_exact")) is not bool
        or evidence.get("database_mutation_performed") is not False
        or evidence.get("public_target_mutation_performed") is not False
        or evidence.get("private_values_redacted") is not True
    ):
        _fail("ai_panorama_operation_evidence_invalid")
    for field in (
        "observed_target_manifest_sha256",
        "observed_database_record_sha256",
        "publication_binding_expected_before_sha256",
        "publication_binding_expected_after_sha256",
    ):
        _digest(
            evidence.get(field),
            "ai_panorama_operation_evidence_invalid",
        )
    expected_manifest_sha256 = _sha256(
        _canonical_bytes(target_manifest)
    )
    if evidence["observed_target_manifest_sha256"] != expected_manifest_sha256:
        _fail("ai_panorama_operation_evidence_invalid")
    plan_status = evidence.get("publication_binding_plan_status")
    if event == "consumed-failed-clean":
        if (
            operation.state not in {"consumed-no-operation", "terminal"}
            or operation.prepared_entry_sha256
            or operation.prepared_evidence_sha256
            or plan_status != "not-prepared"
            or evidence.get("classification_basis")
            != "consumed-before-operation-preparation"
            or evidence["publication_binding_expected_before_sha256"]
            != evidence["publication_binding_expected_after_sha256"]
            or evidence["observed_database_record_sha256"]
            != evidence["publication_binding_expected_before_sha256"]
            or evidence["publication_binding_expected_before_sha256"]
            != expected.expected_publication_record_sha256
            or target_manifest["reserved_entry_count"] != 0
        ):
            _fail("ai_panorama_operation_evidence_invalid")
    else:
        baseline, preparation = (
            _validated_prepared_governed_release_evidence(
                operation.prepared_evidence,
                expected=expected,
                proof=proof,
            )
        )
        expected_basis = {
            "committed": "prepared-after-binding-and-exact-target",
            "failed-clean": "prepared-before-binding-and-baseline-target",
            "recovery-required": "prepared-observation-contradiction",
        }[expected_classification]
        if (
            operation.state not in {"prepared", "terminal"}
            or not operation.prepared_entry_sha256
            or not operation.prepared_evidence_sha256
            or plan_status not in {"change-required", "already-bound"}
            or evidence.get("classification_basis") != expected_basis
        ):
            _fail("ai_panorama_operation_evidence_invalid")
        if (
            evidence["publication_binding_expected_before_sha256"]
            != preparation.get(
                "publication_binding_expected_before_sha256"
            )
            or evidence["publication_binding_expected_after_sha256"]
            != preparation.get(
                "publication_binding_expected_after_sha256"
            )
            or plan_status != preparation.get("status")
        ):
            _fail("ai_panorama_operation_evidence_invalid")
        committed_observation = (
            evidence["observed_database_record_sha256"]
            == evidence["publication_binding_expected_after_sha256"]
            and target_manifest["state"] == "present"
            and target_identity["state"] == "exact"
            and target_manifest["reserved_entry_count"] == 0
            and evidence["observed_publication_binding_exact"] is True
        )
        failed_clean_observation = (
            evidence["observed_database_record_sha256"]
            == evidence["publication_binding_expected_before_sha256"]
            and target_manifest == baseline
            and target_manifest["reserved_entry_count"] == 0
        )
        if (
            expected_classification == "committed"
            and not committed_observation
        ):
            _fail("ai_panorama_operation_evidence_invalid")
        if expected_classification == "failed-clean" and (
            committed_observation or not failed_clean_observation
        ):
            _fail("ai_panorama_operation_evidence_invalid")
        if expected_classification == "recovery-required" and (
            committed_observation or failed_clean_observation
        ):
            _fail("ai_panorama_operation_evidence_invalid")
    return dict(evidence)


def _historical_observation_locked(
    journal: Mapping[str, object],
    proof: admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
) -> AiPanoramaHistoricalOperationObservation:
    operation_id = _historical_operation_id(proof)
    entries = [
        entry
        for entry in journal["entries"]  # type: ignore[index]
        if entry["operation_id"] == operation_id
    ]
    expected_bindings = (
        proof.permit_sha256,
        proof.request_id_sha256,
        proof.nonce_sha256,
        proof.context_sha256,
    )
    if any(
        (
            entry["permit_sha256"],
            entry["request_id_sha256"],
            entry["nonce_sha256"],
            entry["context_sha256"],
        )
        != expected_bindings
        for entry in entries
    ):
        _fail("ai_panorama_operation_historical_proof_invalid")
    prepared: Mapping[str, object] | None = None
    terminal: Mapping[str, object] | None = None
    if entries and entries[0]["event"] == "prepared":
        prepared = entries[0]
        if len(entries) == 2:
            terminal = entries[1]
        elif len(entries) != 1:
            _fail("ai_panorama_operation_historical_state_invalid")
    elif (
        len(entries) == 1
        and entries[0]["event"] == "consumed-failed-clean"
    ):
        terminal = entries[0]
    elif entries:
        _fail("ai_panorama_operation_historical_state_invalid")

    historical_binding = _historical_consumption_binding(proof)
    prepared_evidence: Mapping[str, object] = {}
    if prepared is not None:
        raw_prepared_evidence = prepared.get("evidence")
        if (
            type(raw_prepared_evidence) is not dict
            or raw_prepared_evidence.get("admission_recovery_binding")
            != historical_binding
        ):
            _fail("ai_panorama_operation_historical_proof_invalid")
        prepared_evidence = dict(raw_prepared_evidence)
        _validated_prepared_governed_release_evidence(
            prepared_evidence,
            expected=expected,
            proof=proof,
        )
    terminal_evidence: Mapping[str, object] = {}
    if terminal is not None:
        raw_terminal_evidence = terminal.get("evidence")
        if type(raw_terminal_evidence) is not dict:
            _fail("ai_panorama_operation_historical_state_invalid")
        terminal_evidence = dict(raw_terminal_evidence)
        if (
            terminal["event"] == "consumed-failed-clean"
            and terminal_evidence.get("historical_consumption_binding")
            != historical_binding
        ):
            _fail("ai_panorama_operation_historical_proof_invalid")
    state = (
        "terminal"
        if terminal is not None
        else "prepared"
        if prepared is not None
        else "consumed-no-operation"
    )
    observation = AiPanoramaHistoricalOperationObservation(
        state=state,
        operation_id=operation_id,
        permit_sha256=proof.permit_sha256,
        request_id_sha256=proof.request_id_sha256,
        nonce_sha256=proof.nonce_sha256,
        context_sha256=proof.context_sha256,
        prepared_entry_sha256=(
            str(prepared["entry_sha256"]) if prepared is not None else ""
        ),
        prepared_evidence_sha256=(
            str(prepared["evidence_sha256"]) if prepared is not None else ""
        ),
        prepared_evidence=prepared_evidence,
        terminal_event=(
            str(terminal["event"]) if terminal is not None else ""
        ),
        terminal_entry_sha256=(
            str(terminal["entry_sha256"]) if terminal is not None else ""
        ),
        terminal_evidence_sha256=(
            str(terminal["evidence_sha256"]) if terminal is not None else ""
        ),
        terminal_evidence=terminal_evidence,
        historical_consumption=proof,
    )
    if terminal is not None and terminal_evidence.get(
        "schema"
    ) == _HISTORICAL_RECOVERY_EVIDENCE_SCHEMA:
        _validated_historical_recovery_evidence(
            terminal_evidence,
            proof=proof,
            operation=observation,
            event=str(terminal["event"]),
            expected=expected,
        )
    elif terminal is not None:
        if terminal["event"] == "consumed-failed-clean":
            _fail("ai_panorama_operation_historical_state_invalid")
        _validated_standard_terminal_evidence(
            terminal_evidence,
            expected=expected,
            proof=proof,
            operation=observation,
            event=str(terminal["event"]),
        )
    return observation


def load_historical_ai_panorama_install_operation(
    permit_relpath: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
) -> AiPanoramaHistoricalOperationObservation:
    """Load consumed/no-op, prepared, or terminal state without live authority."""

    proof = admission_authority.load_ai_panorama_install_historical_consumption(
        permit_relpath,
        expected,
    )
    (
        control_descriptor,
        lock_descriptor,
        journal,
        _journal_identity,
        _control_device,
        _control_mount_id,
    ) = _open_locked_journal()
    try:
        return _historical_observation_locked(journal, proof, expected)
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def _same_historical_operation_basis(
    stale: AiPanoramaHistoricalOperationObservation,
    current: AiPanoramaHistoricalOperationObservation,
    *,
    expected_stale_state: str,
) -> bool:
    if (
        type(stale) is not AiPanoramaHistoricalOperationObservation
        or type(current) is not AiPanoramaHistoricalOperationObservation
        or type(stale.prepared_evidence) is not dict
        or type(stale.terminal_evidence) is not dict
        or type(current.prepared_evidence) is not dict
        or type(current.terminal_evidence) is not dict
        or type(stale.historical_consumption)
        is not admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof
        or type(current.historical_consumption)
        is not admission_authority.VerifiedAiPanoramaHistoricalConsumptionProof
        or stale.state != expected_stale_state
        or current.state not in {expected_stale_state, "terminal"}
    ):
        return False
    for field in (
        "operation_id",
        "permit_sha256",
        "request_id_sha256",
        "nonce_sha256",
        "context_sha256",
        "prepared_entry_sha256",
        "prepared_evidence_sha256",
        "prepared_evidence",
        "historical_consumption",
    ):
        if getattr(stale, field) != getattr(current, field):
            return False
    return True


def _record_consumed_without_operation_failed_clean(
    permit_relpath: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    observation: AiPanoramaHistoricalOperationObservation,
    *,
    evidence: Mapping[str, object],
) -> str:
    """Record the truthful no-operation terminal; normal finish APIs cannot."""

    if (
        type(observation)
        is not AiPanoramaHistoricalOperationObservation
        or observation.state != "consumed-no-operation"
    ):
        _fail("ai_panorama_operation_historical_state_invalid")
    (
        control_descriptor,
        lock_descriptor,
        journal,
        journal_identity,
        control_device,
        control_mount_id,
    ) = _open_locked_journal()
    try:
        proof = (
            admission_authority.load_ai_panorama_install_historical_consumption(
                permit_relpath,
                expected,
            )
        )
        operation_id = _historical_operation_id(proof)
        observed = _historical_observation_locked(
            journal,
            proof,
            expected,
        )
        if not _same_historical_operation_basis(
            observation,
            observed,
            expected_stale_state="consumed-no-operation",
        ):
            _fail("ai_panorama_operation_historical_state_invalid")
        if observed.state == "terminal":
            evidence_value, _evidence_sha256 = _evidence(evidence)
            if (
                observed.terminal_event == "consumed-failed-clean"
                and observed.terminal_evidence == evidence_value
            ):
                return observed.terminal_entry_sha256
            _fail("ai_panorama_operation_historical_state_invalid")
        if observed.state != "consumed-no-operation":
            _fail("ai_panorama_operation_historical_state_invalid")
        evidence_value = _validated_historical_recovery_evidence(
            evidence,
            proof=proof,
            operation=observed,
            event="consumed-failed-clean",
            expected=expected,
        )
        return _append_locked(
            control_descriptor=control_descriptor,
            journal=journal,
            journal_identity=journal_identity,
            control_device=control_device,
            control_mount_id=control_mount_id,
            operation_id=operation_id,
            event="consumed-failed-clean",
            permit_sha256=proof.permit_sha256,
            request_id_sha256=proof.request_id_sha256,
            nonce_sha256=proof.nonce_sha256,
            context_sha256=proof.context_sha256,
            evidence=evidence_value,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


def _finish_historical_ai_panorama_install_operation(
    permit_relpath: str,
    expected: admission_authority.AiPanoramaInstallExpectedBindings,
    observation: AiPanoramaHistoricalOperationObservation,
    *,
    event: str,
    evidence: Mapping[str, object],
) -> str:
    """Terminalize one historical prepared operation without install authority."""

    if (
        type(observation) is not AiPanoramaHistoricalOperationObservation
        or event not in _NORMAL_TERMINAL_EVENTS
        or type(evidence) is not dict
    ):
        _fail("ai_panorama_operation_historical_state_invalid")
    (
        control_descriptor,
        lock_descriptor,
        journal,
        journal_identity,
        control_device,
        control_mount_id,
    ) = _open_locked_journal()
    try:
        proof = (
            admission_authority.load_ai_panorama_install_historical_consumption(
                permit_relpath,
                expected,
            )
        )
        current = _historical_observation_locked(
            journal,
            proof,
            expected,
        )
        if not _same_historical_operation_basis(
            observation,
            current,
            expected_stale_state="prepared",
        ):
            _fail("ai_panorama_operation_historical_state_invalid")
        if current.state == "terminal":
            evidence_value, _evidence_sha256 = _evidence(evidence)
            if (
                current.terminal_event == event
                and current.terminal_evidence == evidence_value
            ):
                return current.terminal_entry_sha256
            _fail("ai_panorama_operation_historical_state_invalid")
        if (
            current.state != "prepared"
        ):
            _fail("ai_panorama_operation_historical_state_invalid")
        evidence_value = _validated_historical_recovery_evidence(
            evidence,
            proof=proof,
            operation=current,
            event=event,
            expected=expected,
        )
        return _append_locked(
            control_descriptor=control_descriptor,
            journal=journal,
            journal_identity=journal_identity,
            control_device=control_device,
            control_mount_id=control_mount_id,
            operation_id=current.operation_id,
            event=event,
            permit_sha256=proof.permit_sha256,
            request_id_sha256=proof.request_id_sha256,
            nonce_sha256=proof.nonce_sha256,
            context_sha256=proof.context_sha256,
            evidence=evidence_value,
        )
    finally:
        os.close(lock_descriptor)
        os.close(control_descriptor)


__all__ = [
    "AiPanoramaOperationHandle",
    "AiPanoramaHistoricalOperationObservation",
    "AiPanoramaRecoveredOperationObservation",
    "AiPanoramaOperationJournalError",
    "OPERATION_JOURNAL_AUTHORITY",
    "OPERATION_JOURNAL_LOCK_NAME",
    "OPERATION_JOURNAL_NAME",
    "OPERATION_JOURNAL_SCHEMA",
    "begin_ai_panorama_install_operation",
    "finish_ai_panorama_install_operation",
    "finish_recovered_ai_panorama_install_operation",
    "load_historical_ai_panorama_install_operation",
    "load_recovered_ai_panorama_install_operation",
]
