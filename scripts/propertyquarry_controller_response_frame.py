#!/usr/bin/env python3
"""Fail-closed framing for PropertyQuarry controller lifecycle responses.

This module deliberately does not read stdout or stderr.  A controller response is
eligible for further verification only when it arrives on a separately inherited
file descriptor as one length-prefixed frame and the writer closes that descriptor
after the frame.

The controller must call :func:`adopt_fd` at process entry, before any fork or
exec, and close the response FD in every child.  Adoption cannot repair a writer
that an earlier child already inherited.  Supervisors must treat an EOF timeout
caused by such a leak as an incomplete lifecycle, never as success.

The lifecycle discriminator checks below establish transport/process consistency
only.  They do not validate the complete lifecycle protocol, authenticate a
controller, or verify a signature.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import select
import stat
import time
from enum import IntEnum
from typing import Any, NoReturn


FRAME_HEADER_BYTES = 4
MIN_PAYLOAD_BYTES = 1
MAX_PAYLOAD_BYTES = 1_048_576
MAX_FRAME_BYTES = FRAME_HEADER_BYTES + MAX_PAYLOAD_BYTES
MAX_JSON_NESTING = 64
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0
MAX_READ_TIMEOUT_SECONDS = 300.0
MAX_WRITE_TIMEOUT_SECONDS = 300.0
MIN_JSON_INTEGER = -(2**63)
MAX_JSON_INTEGER = 2**63 - 1

LIFECYCLE_RESPONSE_SCHEMA = "propertyquarry.release.lifecycle-response"
LIFECYCLE_RESPONSE_VERSION = 2

PREFLIGHT_CLASSES = frozenset({"ready", "not-ready", "indeterminate"})
RUN_CLASSES = frozenset(
    {"sealed-final", "rejected", "rolled-back", "contained-failed", "conflict"}
)
RESPONSE_CLASSES = PREFLIGHT_CLASSES | RUN_CLASSES
OPERATIONS = frozenset({"release-preflight", "release-run", "reconcile-run"})


class ControllerExit(IntEnum):
    """Stable controller process exit classes."""

    SUCCESS = 0
    NON_AUTHORIZING_PREFLIGHT = 10
    REJECTED_BEFORE_ADMISSION = 20
    ROLLED_BACK = 30
    CONTAINED_FAILED_OR_RECONCILIATION_REQUIRED = 31
    CAS_OR_REPLAY_CONFLICT = 40
    PROTOCOL_OR_AUTH_FAILURE = 50


EXPECTED_CLASSES_BY_EXIT = {
    ControllerExit.SUCCESS: frozenset({"ready", "sealed-final"}),
    ControllerExit.NON_AUTHORIZING_PREFLIGHT: frozenset(
        {"not-ready", "indeterminate"}
    ),
    ControllerExit.REJECTED_BEFORE_ADMISSION: frozenset({"rejected"}),
    ControllerExit.ROLLED_BACK: frozenset({"rolled-back"}),
    ControllerExit.CONTAINED_FAILED_OR_RECONCILIATION_REQUIRED: frozenset(
        {"contained-failed"}
    ),
    ControllerExit.CAS_OR_REPLAY_CONFLICT: frozenset({"conflict"}),
}


class ControllerResponseFrameError(ValueError):
    """A deterministic, payload-redacted response transport failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"controller response rejected: {code}")


# Short alias for callers that do not need the controller-specific class name.
ResponseFrameError = ControllerResponseFrameError


def _reject(code: str) -> NoReturn:
    raise ControllerResponseFrameError(code)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("duplicate-object-key")
        result[key] = value
    return result


def _reject_nonstandard_number(_token: str) -> NoReturn:
    _reject("non-finite-number")


def _validate_json_tree(value: Any) -> None:
    """Validate strict JSON types, finite numbers, cycles, and bounded depth."""

    stack: list[tuple[Any, int, bool]] = [(value, 1, False)]
    active_containers: set[int] = set()

    while stack:
        current, depth, leaving = stack.pop()
        current_type = type(current)

        if leaving:
            active_containers.remove(id(current))
            continue

        if current_type in {dict, list}:
            if depth > MAX_JSON_NESTING:
                _reject("excessive-json-nesting")
            identity = id(current)
            if identity in active_containers:
                _reject("circular-json-value")
            active_containers.add(identity)
            stack.append((current, depth, True))

            if current_type is dict:
                children: list[Any] = []
                for key, child in current.items():
                    if type(key) is not str:
                        _reject("non-string-object-key")
                    if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                        _reject("unicode-surrogate-forbidden")
                    children.append(child)
            else:
                children = list(current)

            for child in reversed(children):
                stack.append((child, depth + 1, False))
            continue

        if current_type is str:
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                _reject("unicode-surrogate-forbidden")
            continue
        if current is None or current_type is bool:
            continue
        if current_type is int:
            if current < MIN_JSON_INTEGER or current > MAX_JSON_INTEGER:
                _reject("json-integer-out-of-range")
            continue
        if current_type is float:
            if not math.isfinite(current):
                _reject("non-finite-number")
            continue
        _reject("non-json-value")


def _decode_payload(payload: bytes) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        _reject("utf-8-bom-forbidden")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ControllerResponseFrameError("invalid-utf-8") from None

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonstandard_number,
        )
    except ControllerResponseFrameError:
        raise
    except RecursionError:
        raise ControllerResponseFrameError("excessive-json-nesting") from None
    except json.JSONDecodeError:
        raise ControllerResponseFrameError("invalid-json") from None
    except ValueError:
        raise ControllerResponseFrameError("invalid-json-number") from None

    if type(value) is not dict:
        _reject("top-level-object-required")
    _validate_json_tree(value)
    return value


def encode_frame(document: dict[str, Any]) -> bytes:
    """Encode a strict JSON object as one deterministic response frame."""

    if type(document) is not dict:
        _reject("top-level-object-required")
    _validate_json_tree(document)
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ControllerResponseFrameError("non-json-value") from None
    except RecursionError:
        raise ControllerResponseFrameError("excessive-json-nesting") from None

    if len(payload) < MIN_PAYLOAD_BYTES:
        _reject("empty-payload")
    if len(payload) > MAX_PAYLOAD_BYTES:
        _reject("oversize-payload")

    # Round-trip through the same strict decoder so emitted bytes satisfy every
    # decoder-side invariant too.
    _decode_payload(payload)
    return len(payload).to_bytes(FRAME_HEADER_BYTES, "big", signed=False) + payload


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode exactly one complete frame already held in bounded memory."""

    if type(frame) not in {bytes, bytearray, memoryview}:
        _reject("frame-bytes-required")
    frame_size = frame.nbytes if type(frame) is memoryview else len(frame)
    if not frame_size:
        _reject("missing-frame")
    if frame_size < FRAME_HEADER_BYTES:
        _reject("truncated-header")
    if frame_size > MAX_FRAME_BYTES:
        _reject("oversize-frame")
    try:
        raw = bytes(frame)
    except (TypeError, ValueError):
        raise ControllerResponseFrameError("frame-bytes-required") from None

    payload_length = int.from_bytes(
        raw[:FRAME_HEADER_BYTES], "big", signed=False
    )
    if payload_length < MIN_PAYLOAD_BYTES:
        _reject("empty-payload")
    if payload_length > MAX_PAYLOAD_BYTES:
        _reject("oversize-payload")

    expected_length = FRAME_HEADER_BYTES + payload_length
    if len(raw) < expected_length:
        _reject("truncated-payload")
    if len(raw) > expected_length:
        _reject("trailing-data")
    return _decode_payload(raw[FRAME_HEADER_BYTES:])


def _validate_dedicated_fd(fd: int, *, write_end: bool) -> None:
    # bool is an int subclass and must never redirect authority to fd 0 or 1.
    if type(fd) is not int or fd < 3:
        _reject("dedicated-fd-required")

    try:
        descriptor_stat = os.fstat(fd)
        descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        descriptor_target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise ControllerResponseFrameError("dedicated-fd-required") from None

    expected_access = os.O_WRONLY if write_end else os.O_RDONLY
    if (
        not stat.S_ISFIFO(descriptor_stat.st_mode)
        or descriptor_flags & os.O_ACCMODE != expected_access
        or not descriptor_target.startswith("pipe:[")
        or not descriptor_target.endswith("]")
    ):
        _reject("dedicated-fd-required")

    descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
    for standard_fd in (0, 1, 2):
        try:
            standard_stat = os.fstat(standard_fd)
        except OSError:
            continue
        if descriptor_identity == (standard_stat.st_dev, standard_stat.st_ino):
            _reject("dedicated-fd-required")


def adopt_fd(fd: int, *, write_end: bool) -> int:
    """Adopt an anonymous response pipe end and immediately make it CLOEXEC.

    Call this at controller/supervisor entry before any helper process can be
    created.  Once pipe validation succeeds this function takes ownership; an
    adoption failure closes the descriptor so unsafe execution cannot continue
    with an inheritable authority channel.
    """

    if type(write_end) is not bool:
        _reject("fd-direction-invalid")
    _validate_dedicated_fd(fd, write_end=write_end)
    try:
        os.set_inheritable(fd, False)
        inheritable = os.get_inheritable(fd)
    except OSError:
        _close_owned_fd(fd, suppress_error=True)
        raise ControllerResponseFrameError("fd-adoption-failed") from None
    if type(inheritable) is not bool or inheritable:
        _close_owned_fd(fd, suppress_error=True)
        _reject("fd-adoption-failed")
    return fd


def _deadline(timeout_seconds: int | float, *, maximum: float) -> float:
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > maximum
    ):
        _reject("invalid-io-timeout")
    return time.monotonic() + timeout_seconds


def _wait_readable(fd: int, deadline: float) -> None:
    poller = select.poll()
    try:
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    except (OSError, ValueError):
        raise ControllerResponseFrameError("fd-read-failed") from None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reject("fd-read-timeout")
        try:
            events = poller.poll(max(1, math.ceil(remaining * 1000)))
        except InterruptedError:
            continue
        except OSError:
            raise ControllerResponseFrameError("fd-read-failed") from None
        if not events:
            _reject("fd-read-timeout")
        if any(event & select.POLLNVAL for _event_fd, event in events):
            _reject("fd-read-failed")
        return


def _wait_writable(fd: int, deadline: float) -> None:
    poller = select.poll()
    try:
        poller.register(fd, select.POLLOUT | select.POLLHUP | select.POLLERR)
    except (OSError, ValueError):
        raise ControllerResponseFrameError("fd-write-failed") from None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reject("fd-write-timeout")
        try:
            events = poller.poll(max(1, math.ceil(remaining * 1000)))
        except InterruptedError:
            continue
        except OSError:
            raise ControllerResponseFrameError("fd-write-failed") from None
        if not events:
            _reject("fd-write-timeout")
        if any(event & select.POLLNVAL for _event_fd, event in events):
            _reject("fd-write-failed")
        return


def _read_once(fd: int, maximum: int, deadline: float) -> bytes:
    if type(maximum) is not int or maximum < 1:
        _reject("invalid-read-bound")
    while True:
        _wait_readable(fd, deadline)
        try:
            chunk = os.read(fd, maximum)
        except InterruptedError:
            continue
        except OSError:
            raise ControllerResponseFrameError("fd-read-failed") from None
        if type(chunk) is not bytes or len(chunk) > maximum:
            _reject("invalid-fd-read-result")
        return chunk


def _read_at_most(fd: int, size: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = _read_once(fd, remaining, deadline)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _close_owned_fd(fd: int, *, suppress_error: bool) -> None:
    try:
        os.close(fd)
    except OSError:
        if not suppress_error:
            raise ControllerResponseFrameError("fd-close-failed") from None


def read_fd(
    fd: int,
    *,
    timeout_seconds: int | float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read and decode one frame from an owned dedicated FD."""

    document, _exact_frame = read_fd_frame(fd, timeout_seconds=timeout_seconds)
    return document


def read_fd_frame(
    fd: int,
    *,
    timeout_seconds: int | float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], bytes]:
    """Read one frame, require writer EOF, and retain its exact transport bytes.

    Reads are individually bounded by the four-byte header, the declared payload
    size, and a final one-byte EOF probe.  This function always closes the reader's
    descriptor before returning or raising.  The exact frame is returned so an
    installed supervisor can persist/publish the controller's signed bytes
    byte-for-byte instead of manufacturing a re-encoded representation.
    """

    deadline = _deadline(timeout_seconds, maximum=MAX_READ_TIMEOUT_SECONDS)
    adopt_fd(fd, write_end=False)
    failed = False
    try:
        header = _read_at_most(fd, FRAME_HEADER_BYTES, deadline)
        if not header:
            _reject("missing-frame")
        if len(header) < FRAME_HEADER_BYTES:
            _reject("truncated-header")

        payload_length = int.from_bytes(header, "big", signed=False)
        if payload_length < MIN_PAYLOAD_BYTES:
            _reject("empty-payload")
        if payload_length > MAX_PAYLOAD_BYTES:
            _reject("oversize-payload")

        payload = _read_at_most(fd, payload_length, deadline)
        if len(payload) != payload_length:
            _reject("truncated-payload")
        if _read_once(fd, 1, deadline):
            _reject("trailing-data")
        exact_frame = header + payload
        return _decode_payload(payload), exact_frame
    except BaseException:
        failed = True
        raise
    finally:
        _close_owned_fd(fd, suppress_error=failed)


def _write_exact_bytes(fd: int, exact_frame: bytes, deadline: float) -> None:
    try:
        os.set_blocking(fd, False)
    except OSError:
        raise ControllerResponseFrameError("fd-write-failed") from None
    view = memoryview(exact_frame)
    offset = 0
    while offset < len(view):
        _wait_writable(fd, deadline)
        try:
            written = os.write(fd, view[offset:])
        except InterruptedError:
            continue
        except BlockingIOError:
            continue
        except OSError:
            raise ControllerResponseFrameError("fd-write-failed") from None
        if type(written) is not int or written < 1 or written > len(view) - offset:
            _reject("invalid-fd-write-result")
        offset += written


def write_fd(
    fd: int,
    document: dict[str, Any],
    *,
    timeout_seconds: int | float = DEFAULT_WRITE_TIMEOUT_SECONDS,
) -> None:
    """Encode and write one frame to an owned dedicated FD, then close it."""

    deadline = _deadline(timeout_seconds, maximum=MAX_WRITE_TIMEOUT_SECONDS)
    adopt_fd(fd, write_end=True)
    failed = False
    try:
        _write_exact_bytes(fd, encode_frame(document), deadline)
    except BaseException:
        failed = True
        raise
    finally:
        _close_owned_fd(fd, suppress_error=failed)


def write_fd_frame(
    fd: int,
    exact_frame: bytes,
    *,
    timeout_seconds: int | float = DEFAULT_WRITE_TIMEOUT_SECONDS,
) -> None:
    """Validate and write one exact frame without JSON re-encoding.

    This helper closes the dedicated pipe write end after the exact bytes are
    written. A server broker that couples EOF to its own raw exit must instead
    use :func:`write_fd_frame_for_exit`.
    """

    deadline = _deadline(timeout_seconds, maximum=MAX_WRITE_TIMEOUT_SECONDS)
    adopt_fd(fd, write_end=True)
    failed = False
    try:
        if type(exact_frame) is not bytes:
            _reject("frame-bytes-required")
        decode_frame(exact_frame)
        _write_exact_bytes(fd, exact_frame, deadline)
    except BaseException:
        failed = True
        raise
    finally:
        _close_owned_fd(fd, suppress_error=failed)


def write_fd_frame_for_exit(
    fd: int,
    exact_frame: bytes,
    *,
    timeout_seconds: int | float = DEFAULT_WRITE_TIMEOUT_SECONDS,
) -> int:
    """Write an exact frame while retaining the writer for immediate raw exit.

    On success the caller still owns the returned descriptor and must perform no
    fallible work: the production broker exits via ``_exit(mapped_status)`` so
    kernel descriptor teardown supplies EOF. On every failure this function
    closes the descriptor.
    """

    deadline = _deadline(timeout_seconds, maximum=MAX_WRITE_TIMEOUT_SECONDS)
    adopt_fd(fd, write_end=True)
    try:
        if type(exact_frame) is not bytes:
            _reject("frame-bytes-required")
        decode_frame(exact_frame)
        _write_exact_bytes(fd, exact_frame, deadline)
        return fd
    except BaseException:
        _close_owned_fd(fd, suppress_error=True)
        raise


def lifecycle_response_discriminators(document: dict[str, Any]) -> tuple[str, str]:
    """Return ``(class, operation)`` after discriminator-only shape checks.

    The presence of a signature object is required, but its fields and
    cryptographic trust are intentionally outside this transport module.
    """

    if type(document) is not dict:
        _reject("lifecycle-response-object-required")
    if document.get("schema") != LIFECYCLE_RESPONSE_SCHEMA:
        _reject("lifecycle-response-schema-mismatch")
    if (
        type(document.get("version")) is not int
        or document["version"] != LIFECYCLE_RESPONSE_VERSION
    ):
        _reject("lifecycle-response-version-mismatch")
    response = document.get("response")
    if type(response) is not dict:
        _reject("lifecycle-response-body-required")
    if type(document.get("signature")) is not dict:
        _reject("lifecycle-response-signature-required")

    response_class = response.get("class")
    operation = response.get("operation")
    if type(response_class) is not str or response_class not in RESPONSE_CLASSES:
        _reject("lifecycle-response-class-invalid")
    if type(operation) is not str or operation not in OPERATIONS:
        _reject("lifecycle-response-operation-invalid")

    if response_class in PREFLIGHT_CLASSES:
        if operation != "release-preflight":
            _reject("lifecycle-response-operation-class-mismatch")
    else:
        if operation not in {"release-run", "reconcile-run"}:
            _reject("lifecycle-response-operation-class-mismatch")
        if response_class == "sealed-final" and operation == "reconcile-run":
            _reject("lifecycle-response-operation-class-mismatch")
    return response_class, operation


def validate_exit_response(
    exit_code: int | None,
    document: dict[str, Any] | None,
    *,
    expected_operation: str,
    signaled: bool = False,
    timed_out: bool = False,
) -> bool:
    """Validate process exit against a signed-response discriminator.

    Returns ``True`` only for the two structurally eligible combinations:
    clean exit 0 with ``ready`` preflight or ``sealed-final`` release-run.  A
    caller must still verify the full protocol, signature, trust root, request
    binding, and CAS state before granting authority.  Preflight readiness alone
    never authorizes mutation.  Valid terminal failures
    return ``False``; malformed or contradictory combinations raise.
    """

    if type(signaled) is not bool or type(timed_out) is not bool:
        _reject("invalid-process-status")
    if signaled or timed_out:
        _reject("unclean-process-termination")
    if type(exit_code) is not int:
        _reject("invalid-controller-exit")
    if type(expected_operation) is not str or expected_operation not in OPERATIONS:
        _reject("expected-operation-invalid")

    try:
        exit_class = ControllerExit(exit_code)
    except ValueError:
        raise ControllerResponseFrameError("invalid-controller-exit") from None

    if exit_class is ControllerExit.PROTOCOL_OR_AUTH_FAILURE:
        if document is not None:
            _reject("exit-response-mismatch")
        return False
    if document is None:
        _reject("signed-response-required")

    response_class, operation = lifecycle_response_discriminators(document)
    if operation != expected_operation:
        _reject("exit-response-operation-mismatch")
    if response_class not in EXPECTED_CLASSES_BY_EXIT[exit_class]:
        _reject("exit-response-mismatch")
    return exit_class is ControllerExit.SUCCESS


__all__ = [
    "ControllerExit",
    "ControllerResponseFrameError",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_WRITE_TIMEOUT_SECONDS",
    "EXPECTED_CLASSES_BY_EXIT",
    "FRAME_HEADER_BYTES",
    "LIFECYCLE_RESPONSE_SCHEMA",
    "LIFECYCLE_RESPONSE_VERSION",
    "MAX_JSON_NESTING",
    "MAX_FRAME_BYTES",
    "MAX_JSON_INTEGER",
    "MAX_PAYLOAD_BYTES",
    "MAX_READ_TIMEOUT_SECONDS",
    "MAX_WRITE_TIMEOUT_SECONDS",
    "MIN_JSON_INTEGER",
    "MIN_PAYLOAD_BYTES",
    "ResponseFrameError",
    "adopt_fd",
    "decode_frame",
    "encode_frame",
    "lifecycle_response_discriminators",
    "read_fd",
    "read_fd_frame",
    "validate_exit_response",
    "write_fd",
]
