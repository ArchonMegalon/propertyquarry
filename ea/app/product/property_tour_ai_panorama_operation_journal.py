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
    }
)
_TERMINAL_EVENTS = _EVENTS - {"prepared"}


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
        if (
            (event == "prepared" and previous_state is not None)
            or (
                event in _TERMINAL_EVENTS
                and (
                    previous_state is None
                    or previous_state[0] != "prepared"
                    or previous_state[1] != bindings
                )
            )
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
    if type(handle) is not AiPanoramaOperationHandle or event not in _TERMINAL_EVENTS:
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

    if type(permit_relpath) is not str or event not in _TERMINAL_EVENTS:
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
        matches: list[Mapping[str, object]] = []
        for entry in journal["entries"]:
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
                for later in journal["entries"]
            ):
                continue
            matches.append(entry)
        if len(matches) != 1:
            _fail("ai_panorama_operation_recovery_invalid")
        prepared = matches[0]
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


__all__ = [
    "AiPanoramaOperationHandle",
    "AiPanoramaOperationJournalError",
    "OPERATION_JOURNAL_AUTHORITY",
    "OPERATION_JOURNAL_LOCK_NAME",
    "OPERATION_JOURNAL_NAME",
    "OPERATION_JOURNAL_SCHEMA",
    "begin_ai_panorama_install_operation",
    "finish_ai_panorama_install_operation",
    "finish_recovered_ai_panorama_install_operation",
]
