#!/usr/bin/env python3
"""Non-authoritative process harness for PropertyQuarry release lifecycle v2.

This module models the systemd-side supervisor broker's *inner controller-child*
boundary.  It is not the workflow client's outer Unix-socket transport; that
separate boundary is modeled by ``propertyquarry_release_socket_transport_model``.
The module deliberately does not grant release authority: even a clean, fully
verified response is only a candidate for a separate installed authority
decision.  The broker launches one fixed absolute controller executable with one
fixed absolute configuration path.  Candidate repository code cannot select
either path.  It also requires the exact digest of the installed verification
policy as a trusted broker input; that digest must come from root-owned installed
state, never from the workflow/client request.

Controller stdout and stderr are drained only as bounded, fully redacted
diagnostics.  The sole response channel is an anonymous pipe consumed by
``propertyquarry_controller_response_frame.read_fd_frame`` so its exact signed
transport bytes are retained without re-encoding.

Process-group cleanup in this Python harness is reference behavior only.  A
descendant can leave that group, and timed-out Python callback threads cannot be
forcibly cancelled.  The installed supervisor therefore requires a root-owned
cgroup or PID namespace with escape-proof termination/empty proof, plus
kernel-deadline clients or killable isolated helpers for verifier and ledger
calls.  This module cannot supply either production guarantee.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

try:  # Installed beside the framing module, imported as a script or namespace.
    from scripts import propertyquarry_controller_response_frame as response_frame
except ModuleNotFoundError:  # pragma: no cover - exercised by installed layout.
    import propertyquarry_controller_response_frame as response_frame


INSTALLED_CONTROLLER_EXECUTABLE = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-controller-v2"
)
INSTALLED_CONTROLLER_CONFIG = (
    "/etc/propertyquarry-release-control/controller-v2.json"
)
INSTALLED_CONTRACT_ID = "propertyquarry.release.installed-controller-v2"

DEFAULT_PROCESS_TIMEOUT_SECONDS = 120.0
DEFAULT_EOF_TIMEOUT_SECONDS = 125.0
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 15.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 2.0
DEFAULT_DIAGNOSTIC_LIMIT_BYTES = 16_384
MAX_TIMEOUT_SECONDS = 300.0
MAX_CLEANUP_TIMEOUT_SECONDS = 10.0
MAX_DIAGNOSTIC_LIMIT_BYTES = 65_536

_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_FIXED_ENVIRONMENT = {
    "HOME": "/var/empty",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
}

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_SUBREAPER_LOCK = threading.Lock()


@dataclass(frozen=True)
class _ControllerContract:
    """Immutable launch locations; only the module's private test seam varies."""

    executable: str
    config_path: str
    contract_id: str

    def __post_init__(self) -> None:
        for value in (self.executable, self.config_path):
            if (
                type(value) is not str
                or not value.startswith("/")
                or os.path.normpath(value) != value
                or "\x00" in value
            ):
                raise ValueError("controller contract paths must be canonical absolute paths")
        if type(self.contract_id) is not str or not _IDENTIFIER.fullmatch(
            self.contract_id
        ):
            raise ValueError("controller contract id is invalid")


_INSTALLED_CONTRACT = _ControllerContract(
    executable=INSTALLED_CONTROLLER_EXECUTABLE,
    config_path=INSTALLED_CONTROLLER_CONFIG,
    contract_id=INSTALLED_CONTRACT_ID,
)


@dataclass(frozen=True)
class DiagnosticSummary:
    """Content-free accounting for a concurrently drained diagnostic stream."""

    byte_count: int
    omitted_byte_count: int
    eof_observed: bool
    content: Literal["", "<redacted>"]


@dataclass(frozen=True)
class LedgerLookupQuery:
    """Closed, digest-bound query supplied to the external ledger callback."""

    schema: str
    version: int
    event_id: str
    request_transport_digest: str
    expected_operation: str
    incident_code: str
    query_digest: str


@dataclass(frozen=True)
class AuthenticatedLedgerReceipt:
    """Result type required from the independently authenticated ledger reader."""

    schema: str
    version: int
    query_digest: str
    authenticated: bool
    signature_verified: bool
    status: Literal["absent", "recorded", "indeterminate"]
    record_digest: str | None


@dataclass(frozen=True)
class FullVerificationContext:
    """Exact request/frame/installed-policy bindings supplied to the verifier."""

    schema: str
    version: int
    expected_operation: str
    event_id: str
    request_transport_digest: str
    exact_frame_digest: str
    expected_policy_digest: str
    context_digest: str


@dataclass(frozen=True)
class FullVerificationReceipt:
    """Closed result required from the independently authenticated verifier."""

    schema: str
    version: int
    context_digest: str
    event_id: str
    request_transport_digest: str
    exact_frame_digest: str
    policy_digest: str
    signature_verified: bool
    accepted: bool


@dataclass(frozen=True)
class CleanupSummary:
    term_sent: bool
    kill_sent: bool
    direct_child_reaped: bool
    adopted_children_reaped: int
    process_group_gone: bool


@dataclass(frozen=True)
class SupervisorResult:
    """A receipt from the harness; ``authorizes_release`` is invariantly false."""

    disposition: Literal[
        "verified-success-candidate",
        "verified-non-authorizing-terminal",
        "reconciliation-required",
    ]
    reason_code: str
    expected_operation: str
    exit_code: int | None
    protocol_eligible: bool
    authorizes_release: Literal[False]
    reconciliation_required: bool
    response_document: Mapping[str, Any] | None
    exact_response_frame: bytes | None
    full_verifier_accepted: bool
    full_verification_receipt: FullVerificationReceipt | None
    ledger_lookup_attempted: bool
    ledger_lookup_authenticated: bool
    ledger_receipt: AuthenticatedLedgerReceipt | None
    stdout: DiagnosticSummary
    stderr: DiagnosticSummary
    cleanup: CleanupSummary


@dataclass
class _ThreadOutcome:
    done: threading.Event
    value: Any = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _ResponseRead:
    document: dict[str, Any]
    exact_frame: bytes


def _validated_timeout(value: int | float, *, maximum: float) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value <= 0
        or value > maximum
    ):
        raise ValueError("invalid supervisor timeout")
    return float(value)


def _validated_inputs(
    *,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
    expected_policy_digest: str,
    diagnostic_limit_bytes: int,
    full_verifier: Callable[..., Any],
    ledger_lookup: Callable[..., Any],
) -> None:
    if (
        type(expected_operation) is not str
        or expected_operation not in response_frame.OPERATIONS
    ):
        raise ValueError("expected operation is invalid")
    if type(event_id) is not str or not _IDENTIFIER.fullmatch(event_id):
        raise ValueError("event id is invalid")
    if (
        type(request_transport_digest) is not str
        or not _DIGEST.fullmatch(request_transport_digest)
    ):
        raise ValueError("request transport digest is invalid")
    if (
        type(expected_policy_digest) is not str
        or not _DIGEST.fullmatch(expected_policy_digest)
    ):
        raise ValueError("expected installed policy digest is invalid")
    if (
        type(diagnostic_limit_bytes) is not int
        or diagnostic_limit_bytes < 0
        or diagnostic_limit_bytes > MAX_DIAGNOSTIC_LIMIT_BYTES
    ):
        raise ValueError("diagnostic bound is invalid")
    if not callable(full_verifier) or not callable(ledger_lookup):
        raise TypeError("verifier and ledger lookup callbacks are required")


def _canonical_digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _ledger_query(
    *,
    event_id: str,
    request_transport_digest: str,
    expected_operation: str,
    incident_code: str,
) -> LedgerLookupQuery:
    unsigned = {
        "event_id": event_id,
        "expected_operation": expected_operation,
        "incident_code": incident_code,
        "request_transport_digest": request_transport_digest,
        "schema": "propertyquarry.release.ledger-lookup-query",
        "version": 2,
    }
    return LedgerLookupQuery(
        **unsigned,
        query_digest=_canonical_digest(unsigned),
    )


def _valid_ledger_receipt(
    receipt: Any, query: LedgerLookupQuery
) -> AuthenticatedLedgerReceipt | None:
    # Exact type and singleton booleans prevent truthy proxy values from passing.
    if type(receipt) is not AuthenticatedLedgerReceipt:
        return None
    if (
        type(receipt.schema) is not str
        or receipt.schema != "propertyquarry.release.ledger-lookup-receipt"
        or type(receipt.version) is not int
        or receipt.version != 2
        or type(receipt.query_digest) is not str
        or receipt.query_digest != query.query_digest
        or receipt.authenticated is not True
        or receipt.signature_verified is not True
        or type(receipt.status) is not str
        or receipt.status not in {"absent", "recorded", "indeterminate"}
    ):
        return None
    if receipt.status == "recorded":
        if type(receipt.record_digest) is not str or not _DIGEST.fullmatch(
            receipt.record_digest
        ):
            return None
    elif receipt.record_digest is not None:
        return None
    return receipt


def _verification_context(
    *,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
    expected_policy_digest: str,
    exact_frame: bytes,
) -> FullVerificationContext:
    material = {
        "event_id": event_id,
        "exact_frame_digest": _bytes_digest(exact_frame),
        "expected_operation": expected_operation,
        "expected_policy_digest": expected_policy_digest,
        "request_transport_digest": request_transport_digest,
        "schema": "propertyquarry.release.full-verification-context",
        "version": 2,
    }
    return FullVerificationContext(
        **material,
        context_digest=_canonical_digest(material),
    )


def _valid_full_verification_receipt(
    receipt: Any,
    context: FullVerificationContext,
) -> FullVerificationReceipt | None:
    if type(receipt) is not FullVerificationReceipt:
        return None
    if (
        type(receipt.schema) is not str
        or receipt.schema != "propertyquarry.release.full-verification-receipt"
        or type(receipt.version) is not int
        or receipt.version != 2
        or type(receipt.context_digest) is not str
        or receipt.context_digest != context.context_digest
        or type(receipt.event_id) is not str
        or receipt.event_id != context.event_id
        or type(receipt.request_transport_digest) is not str
        or receipt.request_transport_digest != context.request_transport_digest
        or type(receipt.exact_frame_digest) is not str
        or receipt.exact_frame_digest != context.exact_frame_digest
        or type(receipt.policy_digest) is not str
        or _DIGEST.fullmatch(receipt.policy_digest) is None
        or receipt.policy_digest != context.expected_policy_digest
        or receipt.signature_verified is not True
        or receipt.accepted is not True
    ):
        return None
    return receipt


def _run_bounded_callback(
    callback: Callable[[], Any], timeout_seconds: float
) -> tuple[bool, Any]:
    outcome = _ThreadOutcome(threading.Event())

    def invoke() -> None:
        try:
            outcome.value = callback()
        except BaseException as error:  # Callback details are never surfaced.
            outcome.error = error
        finally:
            outcome.done.set()

    threading.Thread(target=invoke, daemon=True).start()
    if not outcome.done.wait(timeout_seconds):
        return False, None
    if outcome.error is not None:
        return False, None
    return True, outcome.value


def _response_reader(fd: int, deadline: float, outcome: _ThreadOutcome) -> None:
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise response_frame.ControllerResponseFrameError("fd-read-timeout")
        document, exact_frame = response_frame.read_fd_frame(
            fd, timeout_seconds=remaining
        )
        outcome.value = _ResponseRead(document, exact_frame)
    except BaseException as error:
        outcome.error = error
    finally:
        outcome.done.set()


def _diagnostic_reader(
    stream: Any,
    deadline: float,
    limit: int,
    outcome: _ThreadOutcome,
) -> None:
    byte_count = 0
    eof_observed = False
    try:
        fd = stream.fileno()
        os.set_blocking(fd, False)
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                events = poller.poll(max(1, math.ceil(remaining * 1000)))
            except InterruptedError:
                continue
            if not events:
                break
            while True:
                try:
                    chunk = os.read(fd, 65_536)
                except BlockingIOError:
                    break
                except InterruptedError:
                    continue
                if not chunk:
                    eof_observed = True
                    break
                byte_count += len(chunk)
            if eof_observed:
                break
        outcome.value = DiagnosticSummary(
            byte_count=byte_count,
            omitted_byte_count=max(0, byte_count - limit),
            eof_observed=eof_observed,
            content="<redacted>" if byte_count else "",
        )
    except BaseException as error:
        outcome.error = error
    finally:
        try:
            stream.close()
        except BaseException:
            pass
        outcome.done.set()


def _empty_diagnostic() -> DiagnosticSummary:
    return DiagnosticSummary(0, 0, True, "")


def _diagnostic_value(outcome: _ThreadOutcome) -> DiagnosticSummary:
    if type(outcome.value) is DiagnosticSummary:
        return outcome.value
    return DiagnosticSummary(0, 0, False, "")


def _subreaper_state() -> tuple[bool, bool]:
    """Return ``(supported, previously_enabled)`` and enable when possible."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if libc.prctl(
            _PR_GET_CHILD_SUBREAPER,
            ctypes.byref(previous),
            0,
            0,
            0,
        ) != 0:
            return False, False
        if previous.value == 0 and libc.prctl(
            _PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0
        ) != 0:
            return False, False
        return True, previous.value == 1
    except (AttributeError, OSError):
        return False, False


def _restore_subreaper(supported: bool, previously_enabled: bool) -> None:
    if not supported or previously_enabled:
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(
            _PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0
        )
    except (AttributeError, OSError):
        pass


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _reap_adopted_group_children(pgid: int, deadline: float) -> int:
    reaped = 0
    while time.monotonic() < deadline:
        made_progress = False
        while True:
            try:
                child_pid, _status = os.waitpid(-pgid, os.WNOHANG)
            except ChildProcessError:
                break
            except InterruptedError:
                continue
            if child_pid <= 0:
                break
            reaped += 1
            made_progress = True
        if not _process_group_exists(pgid):
            break
        if not made_progress:
            time.sleep(0.005)
    return reaped


def _cleanup_process_group(
    process: subprocess.Popen[bytes],
    *,
    cleanup_timeout_seconds: float,
    force_signal: bool,
) -> CleanupSummary:
    pgid = process.pid
    deadline = time.monotonic() + cleanup_timeout_seconds
    term_sent = False
    kill_sent = False

    group_present = _process_group_exists(pgid)
    if force_signal or group_present:
        term_sent = _signal_group(pgid, signal.SIGTERM)

    direct_reaped = process.poll() is not None
    if not direct_reaped:
        try:
            process.wait(timeout=max(0.001, min(0.2, deadline - time.monotonic())))
            direct_reaped = True
        except (subprocess.TimeoutExpired, ValueError):
            pass

    if _process_group_exists(pgid):
        kill_sent = _signal_group(pgid, signal.SIGKILL)
    if not direct_reaped:
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            direct_reaped = True
        except (subprocess.TimeoutExpired, ValueError):
            pass

    adopted_reaped = _reap_adopted_group_children(pgid, deadline)
    return CleanupSummary(
        term_sent=term_sent,
        kill_sent=kill_sent,
        direct_child_reaped=direct_reaped,
        adopted_children_reaped=adopted_reaped,
        process_group_gone=not _process_group_exists(pgid),
    )


def _lookup_after_failure(
    *,
    callback: Callable[[LedgerLookupQuery], Any],
    callback_timeout_seconds: float,
    event_id: str,
    request_transport_digest: str,
    expected_operation: str,
    incident_code: str,
) -> tuple[bool, AuthenticatedLedgerReceipt | None]:
    query = _ledger_query(
        event_id=event_id,
        request_transport_digest=request_transport_digest,
        expected_operation=expected_operation,
        incident_code=incident_code,
    )
    completed, candidate = _run_bounded_callback(
        lambda: callback(query), callback_timeout_seconds
    )
    if not completed:
        return False, None
    try:
        receipt = _valid_ledger_receipt(candidate, query)
    except BaseException:
        return False, None
    return receipt is not None, receipt


def _failure_result(
    *,
    reason_code: str,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
    exit_code: int | None,
    callback: Callable[[LedgerLookupQuery], Any],
    callback_timeout_seconds: float,
    stdout: DiagnosticSummary,
    stderr: DiagnosticSummary,
    cleanup: CleanupSummary,
    response: _ResponseRead | None = None,
) -> SupervisorResult:
    authenticated, receipt = _lookup_after_failure(
        callback=callback,
        callback_timeout_seconds=callback_timeout_seconds,
        event_id=event_id,
        request_transport_digest=request_transport_digest,
        expected_operation=expected_operation,
        incident_code=reason_code,
    )
    return SupervisorResult(
        disposition="reconciliation-required",
        reason_code=reason_code,
        expected_operation=expected_operation,
        exit_code=exit_code,
        protocol_eligible=False,
        authorizes_release=False,
        reconciliation_required=True,
        response_document=(
            _freeze_json(copy.deepcopy(response.document))
            if response is not None
            else None
        ),
        exact_response_frame=response.exact_frame if response is not None else None,
        full_verifier_accepted=False,
        full_verification_receipt=None,
        ledger_lookup_attempted=True,
        ledger_lookup_authenticated=authenticated,
        ledger_receipt=receipt,
        stdout=stdout,
        stderr=stderr,
        cleanup=cleanup,
    )


def _controller_argv(
    contract: _ControllerContract,
    *,
    response_fd: int,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
) -> tuple[str, ...]:
    return (
        contract.executable,
        "--config",
        contract.config_path,
        "--operation",
        expected_operation,
        "--response-fd",
        str(response_fd),
        "--event-id",
        event_id,
        "--request-transport-digest",
        request_transport_digest,
    )


def _run_supervisor_with_contract(
    *,
    contract: _ControllerContract,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
    expected_policy_digest: str,
    full_verifier: Callable[
        [dict[str, Any], bytes, FullVerificationContext], Any
    ],
    ledger_lookup: Callable[[LedgerLookupQuery], Any],
    process_timeout_seconds: int | float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    eof_timeout_seconds: int | float = DEFAULT_EOF_TIMEOUT_SECONDS,
    callback_timeout_seconds: int | float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    cleanup_timeout_seconds: int | float = DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    diagnostic_limit_bytes: int = DEFAULT_DIAGNOSTIC_LIMIT_BYTES,
) -> SupervisorResult:
    """Private composition seam used by production and hostile subprocess tests."""

    _validated_inputs(
        expected_operation=expected_operation,
        event_id=event_id,
        request_transport_digest=request_transport_digest,
        expected_policy_digest=expected_policy_digest,
        diagnostic_limit_bytes=diagnostic_limit_bytes,
        full_verifier=full_verifier,
        ledger_lookup=ledger_lookup,
    )
    process_timeout = _validated_timeout(
        process_timeout_seconds, maximum=MAX_TIMEOUT_SECONDS
    )
    eof_timeout = _validated_timeout(eof_timeout_seconds, maximum=MAX_TIMEOUT_SECONDS)
    callback_timeout = _validated_timeout(
        callback_timeout_seconds, maximum=MAX_TIMEOUT_SECONDS
    )
    cleanup_timeout = _validated_timeout(
        cleanup_timeout_seconds, maximum=MAX_CLEANUP_TIMEOUT_SECONDS
    )

    with _SUBREAPER_LOCK:
        subreaper_supported, subreaper_previously_enabled = _subreaper_state()
        process: subprocess.Popen[bytes] | None = None
        response_read_fd: int | None = None
        response_write_fd: int | None = None
        stdout_outcome = _ThreadOutcome(threading.Event())
        stderr_outcome = _ThreadOutcome(threading.Event())
        response_outcome = _ThreadOutcome(threading.Event())
        cleanup = CleanupSummary(False, False, False, 0, True)
        started = time.monotonic()
        process_deadline = started + process_timeout
        eof_deadline = started + eof_timeout

        try:
            response_read_fd, response_write_fd = os.pipe2(os.O_CLOEXEC)
            argv = _controller_argv(
                contract,
                response_fd=response_write_fd,
                expected_operation=expected_operation,
                event_id=event_id,
                request_transport_digest=request_transport_digest,
            )
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    pass_fds=(response_write_fd,),
                    cwd="/",
                    env=dict(_FIXED_ENVIRONMENT),
                    start_new_session=True,
                    shell=False,
                )
            except BaseException:
                os.close(response_read_fd)
                os.close(response_write_fd)
                response_read_fd = None
                response_write_fd = None
                return _failure_result(
                    reason_code="controller-spawn-failed",
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=None,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_empty_diagnostic(),
                    stderr=_empty_diagnostic(),
                    cleanup=cleanup,
                )

            os.close(response_write_fd)
            response_write_fd = None
            assert process.stdout is not None and process.stderr is not None
            threading.Thread(
                target=_response_reader,
                args=(response_read_fd, eof_deadline, response_outcome),
                daemon=True,
            ).start()
            response_read_fd = None  # Reader thread owns and closes it.
            threading.Thread(
                target=_diagnostic_reader,
                args=(process.stdout, eof_deadline, diagnostic_limit_bytes, stdout_outcome),
                daemon=True,
            ).start()
            threading.Thread(
                target=_diagnostic_reader,
                args=(process.stderr, eof_deadline, diagnostic_limit_bytes, stderr_outcome),
                daemon=True,
            ).start()

            incident: str | None = None
            while True:
                exit_code = process.poll()
                now = time.monotonic()

                if exit_code is not None and exit_code < 0:
                    incident = "controller-signaled"
                    break
                if exit_code is None and now >= process_deadline:
                    incident = "controller-process-timeout"
                    break
                if response_outcome.done.is_set() and response_outcome.error is not None:
                    if isinstance(
                        response_outcome.error,
                        response_frame.ControllerResponseFrameError,
                    ):
                        if response_outcome.error.code == "fd-read-timeout":
                            incident = (
                                "response-eof-leaked-child"
                                if exit_code is not None
                                else "response-eof-timeout"
                            )
                        else:
                            incident = "response-" + response_outcome.error.code
                    else:
                        incident = "response-reader-failed"
                    break
                if stdout_outcome.done.is_set() and stdout_outcome.error is not None:
                    incident = "stdout-drain-failed"
                    break
                if stderr_outcome.done.is_set() and stderr_outcome.error is not None:
                    incident = "stderr-drain-failed"
                    break
                if (
                    stdout_outcome.done.is_set()
                    and type(stdout_outcome.value) is DiagnosticSummary
                    and not stdout_outcome.value.eof_observed
                ) or (
                    stderr_outcome.done.is_set()
                    and type(stderr_outcome.value) is DiagnosticSummary
                    and not stderr_outcome.value.eof_observed
                ):
                    incident = "diagnostic-eof-timeout"
                    break
                if now >= eof_deadline and not response_outcome.done.is_set():
                    incident = (
                        "response-eof-leaked-child"
                        if exit_code is not None
                        else "response-eof-timeout"
                    )
                    break
                if now >= eof_deadline and (
                    not stdout_outcome.done.is_set() or not stderr_outcome.done.is_set()
                ):
                    incident = "diagnostic-eof-timeout"
                    break
                if (
                    exit_code is not None
                    and response_outcome.done.is_set()
                    and stdout_outcome.done.is_set()
                    and stderr_outcome.done.is_set()
                ):
                    break
                time.sleep(0.002)

            if incident is not None:
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                response_outcome.done.wait(cleanup_timeout)
                stdout_outcome.done.wait(cleanup_timeout)
                stderr_outcome.done.wait(cleanup_timeout)
                response = (
                    response_outcome.value
                    if type(response_outcome.value) is _ResponseRead
                    else None
                )
                return _failure_result(
                    reason_code=incident,
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=process.returncode,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_diagnostic_value(stdout_outcome),
                    stderr=_diagnostic_value(stderr_outcome),
                    cleanup=cleanup,
                    response=response,
                )

            response = (
                response_outcome.value
                if type(response_outcome.value) is _ResponseRead
                else None
            )
            if response is None:
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                return _failure_result(
                    reason_code="response-missing-frame",
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=process.returncode,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_diagnostic_value(stdout_outcome),
                    stderr=_diagnostic_value(stderr_outcome),
                    cleanup=cleanup,
                )

            try:
                eligible = response_frame.validate_exit_response(
                    process.returncode,
                    response.document,
                    expected_operation=expected_operation,
                    signaled=False,
                    timed_out=False,
                )
            except response_frame.ControllerResponseFrameError:
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                return _failure_result(
                    reason_code="exit-response-mismatch",
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=process.returncode,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_diagnostic_value(stdout_outcome),
                    stderr=_diagnostic_value(stderr_outcome),
                    cleanup=cleanup,
                    response=response,
                )

            verification_context = _verification_context(
                expected_operation=expected_operation,
                event_id=event_id,
                request_transport_digest=request_transport_digest,
                expected_policy_digest=expected_policy_digest,
                exact_frame=response.exact_frame,
            )
            verifier_completed, verifier_value = _run_bounded_callback(
                lambda: full_verifier(
                    copy.deepcopy(response.document),
                    response.exact_frame,
                    verification_context,
                ),
                callback_timeout,
            )
            verification_receipt = None
            if verifier_completed:
                try:
                    verification_receipt = _valid_full_verification_receipt(
                        verifier_value,
                        verification_context,
                    )
                except BaseException:
                    verification_receipt = None
            if verification_receipt is None:
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                return _failure_result(
                    reason_code="full-verifier-rejected",
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=process.returncode,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_diagnostic_value(stdout_outcome),
                    stderr=_diagnostic_value(stderr_outcome),
                    cleanup=cleanup,
                    response=response,
                )

            # A response/diagnostic EOF does not prove the process group is gone.
            # A surviving descendant is always reconciliation-required.
            if _process_group_exists(process.pid):
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                return _failure_result(
                    reason_code="process-group-leak",
                    expected_operation=expected_operation,
                    event_id=event_id,
                    request_transport_digest=request_transport_digest,
                    exit_code=process.returncode,
                    callback=ledger_lookup,
                    callback_timeout_seconds=callback_timeout,
                    stdout=_diagnostic_value(stdout_outcome),
                    stderr=_diagnostic_value(stderr_outcome),
                    cleanup=cleanup,
                    response=response,
                )

            cleanup = CleanupSummary(False, False, True, 0, True)
            return SupervisorResult(
                disposition=(
                    "verified-success-candidate"
                    if eligible
                    else "verified-non-authorizing-terminal"
                ),
                reason_code="verified-response",
                expected_operation=expected_operation,
                exit_code=process.returncode,
                protocol_eligible=eligible,
                authorizes_release=False,
                reconciliation_required=False,
                response_document=_freeze_json(copy.deepcopy(response.document)),
                exact_response_frame=response.exact_frame,
                full_verifier_accepted=True,
                full_verification_receipt=verification_receipt,
                ledger_lookup_attempted=False,
                ledger_lookup_authenticated=False,
                ledger_receipt=None,
                stdout=_diagnostic_value(stdout_outcome),
                stderr=_diagnostic_value(stderr_outcome),
                cleanup=cleanup,
            )
        except BaseException:
            # No unexpected local failure may strand an installed controller or
            # accidentally bypass reconciliation.  Exception details are kept
            # out of the receipt because they can contain controller-controlled
            # data.
            if process is not None:
                cleanup = _cleanup_process_group(
                    process,
                    cleanup_timeout_seconds=cleanup_timeout,
                    force_signal=True,
                )
                response_outcome.done.wait(cleanup_timeout)
                stdout_outcome.done.wait(cleanup_timeout)
                stderr_outcome.done.wait(cleanup_timeout)
            return _failure_result(
                reason_code="supervisor-internal-failure",
                expected_operation=expected_operation,
                event_id=event_id,
                request_transport_digest=request_transport_digest,
                exit_code=process.returncode if process is not None else None,
                callback=ledger_lookup,
                callback_timeout_seconds=callback_timeout,
                stdout=_diagnostic_value(stdout_outcome),
                stderr=_diagnostic_value(stderr_outcome),
                cleanup=cleanup,
                response=(
                    response_outcome.value
                    if type(response_outcome.value) is _ResponseRead
                    else None
                ),
            )
        finally:
            if response_read_fd is not None:
                try:
                    os.close(response_read_fd)
                except OSError:
                    pass
            if response_write_fd is not None:
                try:
                    os.close(response_write_fd)
                except OSError:
                    pass
            _restore_subreaper(subreaper_supported, subreaper_previously_enabled)


def run_installed_controller(
    *,
    expected_operation: str,
    event_id: str,
    request_transport_digest: str,
    expected_policy_digest: str,
    full_verifier: Callable[
        [dict[str, Any], bytes, FullVerificationContext], Any
    ],
    ledger_lookup: Callable[[LedgerLookupQuery], Any],
    process_timeout_seconds: int | float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    eof_timeout_seconds: int | float = DEFAULT_EOF_TIMEOUT_SECONDS,
    callback_timeout_seconds: int | float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    cleanup_timeout_seconds: int | float = DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    diagnostic_limit_bytes: int = DEFAULT_DIAGNOSTIC_LIMIT_BYTES,
) -> SupervisorResult:
    """Model the server-side broker launching its one controller child.

    No executable, configuration, environment, working directory, or extra file
    descriptor is caller-selectable. ``expected_policy_digest`` is mandatory
    trusted input from root-owned installed broker state, not a workflow/client
    request field; a verifier receipt must match it exactly. This does not model
    the unprivileged workflow client's socket path. The returned object never
    authorizes a release; an installed authority must consume a verified
    candidate separately.
    """

    return _run_supervisor_with_contract(
        contract=_INSTALLED_CONTRACT,
        expected_operation=expected_operation,
        event_id=event_id,
        request_transport_digest=request_transport_digest,
        expected_policy_digest=expected_policy_digest,
        full_verifier=full_verifier,
        ledger_lookup=ledger_lookup,
        process_timeout_seconds=process_timeout_seconds,
        eof_timeout_seconds=eof_timeout_seconds,
        callback_timeout_seconds=callback_timeout_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
        diagnostic_limit_bytes=diagnostic_limit_bytes,
    )


def describe_contract() -> dict[str, Any]:
    """Describe the reference boundary without implying installed authority."""

    return {
        "contract_id": INSTALLED_CONTRACT_ID,
        "authoritative": False,
        "modeled_role": "systemd-supervisor-broker-inner-controller-child",
        "workflow_client_transport": "separate-unix-socket-model",
        "request_transport_digest": "sha256-prefixed-lowercase-hex",
        "expected_policy_digest": (
            "required-trusted-root-owned-installed-input-not-request-derived"
        ),
        "full_verification": (
            "typed-event-request-frame-exact-installed-policy-bound-receipt"
        ),
        "process_group_cleanup": "reference-only",
        "production_containment_required": (
            "root-owned-cgroup-or-pid-namespace-with-escape-proof-kill-and-empty-proof"
        ),
        "callback_timeout_cancellation": "not-provided-by-python-thread-model",
        "production_callback_requirement": (
            "kernel-deadline-client-or-killable-isolated-helper"
        ),
    }


__all__ = [
    "AuthenticatedLedgerReceipt",
    "CleanupSummary",
    "DEFAULT_CALLBACK_TIMEOUT_SECONDS",
    "DEFAULT_CLEANUP_TIMEOUT_SECONDS",
    "DEFAULT_DIAGNOSTIC_LIMIT_BYTES",
    "DEFAULT_EOF_TIMEOUT_SECONDS",
    "DEFAULT_PROCESS_TIMEOUT_SECONDS",
    "DiagnosticSummary",
    "FullVerificationContext",
    "FullVerificationReceipt",
    "INSTALLED_CONTRACT_ID",
    "INSTALLED_CONTROLLER_CONFIG",
    "INSTALLED_CONTROLLER_EXECUTABLE",
    "LedgerLookupQuery",
    "SupervisorResult",
    "describe_contract",
    "run_installed_controller",
]
