#!/usr/bin/env python3
"""Non-authoritative Linux transport model for release-control requests.

This module models only the outer supervisor-to-systemd-broker transport.  It
does not authenticate a request, verify a signature, grant release authority,
or mutate lifecycle state.  One connected AF_UNIX/SOCK_STREAM carries one
length-prefixed request.  The supervisor passes exactly one anonymous response
pipe write end with SCM_RIGHTS on the first byte; the systemd-side broker
forwards the controller's exact verified lifecycle response through that pipe
using the separate response-frame contract.

Both public I/O functions take ownership of their socket. ``send_request`` also
takes ownership of the passed response-pipe descriptor. ``receive_request``
returns ownership of the received descriptor in a context-managed result.
"""

from __future__ import annotations

import array
import dataclasses
import fcntl
import math
import os
import re
import select
import socket
import stat
import struct
import time
from typing import Any, NoReturn, Sequence

try:  # Support both repo imports and direct execution from scripts/.
    from scripts import propertyquarry_release_request_authority_model as request_authority
except ImportError:  # pragma: no cover - direct script import compatibility
    import propertyquarry_release_request_authority_model as request_authority  # type: ignore[no-redef]


FRAME_HEADER_BYTES = 4
MAX_PAYLOAD_BYTES = request_authority.MAX_TRANSPORT_BYTES
MAX_FRAME_BYTES = FRAME_HEADER_BYTES + MAX_PAYLOAD_BYTES
INSTALLED_SOCKET_PATH = "/run/propertyquarry-release-control-v2/request.sock"
DEFAULT_IO_TIMEOUT_SECONDS = 10.0
MAX_IO_TIMEOUT_SECONDS = 60.0

# Linux SCM_MAX_FD.  A bounded buffer large enough for the kernel maximum makes
# "more than one" observable instead of accepting a silently truncated list.
SCM_MAX_FD = 253
_FD_ARRAY_TYPE = "i"
_FD_ITEMSIZE = array.array(_FD_ARRAY_TYPE).itemsize
_UCRED = struct.Struct("=iII")
_PIPE_TARGET = re.compile(r"pipe:\[[0-9]+\]")


class SocketTransportError(ValueError):
    """Deterministic, payload-redacted transport rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"release socket transport rejected: {code}")


def _reject(code: str) -> NoReturn:
    raise SocketTransportError(code)


@dataclasses.dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclasses.dataclass(frozen=True)
class DescriptorMetadata:
    fd: int
    mode: int
    device: int
    inode: int
    access_mode: int
    target: str
    close_on_exec: bool


@dataclasses.dataclass(frozen=True)
class AncillaryBatch:
    rights: tuple[int, ...]
    credentials: tuple[PeerCredentials, ...]
    message_flags: int


@dataclasses.dataclass(frozen=True)
class SentRequest:
    """A transport receipt, never an authorization receipt."""

    exact_frame: bytes
    raw_digest: str
    parsed_transport: Any


@dataclasses.dataclass
class ReceivedRequest:
    """A parsed request candidate that owns one response-pipe write end."""

    exact_frame: bytes
    exact_payload: bytes
    raw_digest: str
    peer_credentials: PeerCredentials
    parsed_transport: Any
    response_fd: int = dataclasses.field(repr=False)

    def detach_response_fd(self) -> int:
        if type(self.response_fd) is not int or self.response_fd < 0:
            _reject("response-fd-not-owned")
        descriptor = self.response_fd
        self.response_fd = -1
        return descriptor

    def close(self) -> None:
        if type(self.response_fd) is int and self.response_fd >= 0:
            descriptor = self.response_fd
            self.response_fd = -1
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "ReceivedRequest":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _linux_constant(name: str) -> int:
    value = getattr(socket, name, None)
    if not isinstance(value, int) or isinstance(value, bool):
        _reject("linux-ancillary-contract-unavailable")
    return int(value)


def _deadline(timeout_seconds: int | float) -> float:
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_IO_TIMEOUT_SECONDS
    ):
        _reject("invalid-io-timeout")
    return time.monotonic() + float(timeout_seconds)


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _close_fds(descriptors: Sequence[int]) -> None:
    # Kernel-created receiver descriptors are unique.  Deduplication also makes
    # mocked hostile ancillary input safe from double-close/reuse accidents.
    for descriptor in sorted(set(descriptors)):
        if type(descriptor) is int:
            _close_fd(descriptor)


def _whole_rights(ancillary: Sequence[tuple[int, int, bytes]]) -> tuple[int, ...]:
    rights: list[int] = []
    scm_rights = getattr(socket, "SCM_RIGHTS", None)
    for item in ancillary:
        if type(item) is not tuple or len(item) != 3:
            continue
        level, kind, data = item
        if level != socket.SOL_SOCKET or kind != scm_rights or type(data) is not bytes:
            continue
        usable = len(data) - (len(data) % _FD_ITEMSIZE)
        if not usable:
            continue
        decoded = array.array(_FD_ARRAY_TYPE)
        decoded.frombytes(data[:usable])
        rights.extend(int(value) for value in decoded)
    return tuple(rights)


def decode_ancillary(
    ancillary: Sequence[tuple[int, int, bytes]], message_flags: int
) -> AncillaryBatch:
    """Purely decode one recvmsg ancillary batch.

    Descriptor ownership is not changed here.  The recvmsg adapter inventories
    SCM_RIGHTS first and closes every delivered descriptor if this parser rejects.
    """

    if (
        type(ancillary) not in {list, tuple}
        or not isinstance(message_flags, int)
        or isinstance(message_flags, bool)
    ):
        _reject("ancillary-shape-invalid")
    message_flags = int(message_flags)
    cloexec_flag = _linux_constant("MSG_CMSG_CLOEXEC")
    ctrunc_flag = _linux_constant("MSG_CTRUNC")
    if message_flags & ctrunc_flag:
        _reject("ancillary-truncated")
    if not message_flags & cloexec_flag or message_flags & ~cloexec_flag:
        _reject("ancillary-flags-invalid")

    scm_rights = _linux_constant("SCM_RIGHTS")
    scm_credentials = _linux_constant("SCM_CREDENTIALS")
    rights: list[int] = []
    credentials: list[PeerCredentials] = []
    rights_messages = 0

    for item in ancillary:
        if type(item) is not tuple or len(item) != 3:
            _reject("ancillary-shape-invalid")
        level, kind, data = item
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or not isinstance(kind, int)
            or isinstance(kind, bool)
            or type(data) is not bytes
        ):
            _reject("ancillary-shape-invalid")
        level = int(level)
        kind = int(kind)
        if level != socket.SOL_SOCKET:
            _reject("ancillary-type-invalid")
        if kind == scm_rights:
            rights_messages += 1
            if rights_messages > 1 or not data or len(data) % _FD_ITEMSIZE:
                _reject("response-fd-count-invalid")
            decoded = array.array(_FD_ARRAY_TYPE)
            decoded.frombytes(data)
            rights.extend(int(value) for value in decoded)
            continue
        if kind == scm_credentials:
            if len(data) != _UCRED.size:
                _reject("credentials-shape-invalid")
            if credentials:
                _reject("credentials-duplicate")
            pid, uid, gid = _UCRED.unpack(data)
            # Linux may attach a zero-PID synthetic credential record to the
            # orderly EOF recvmsg when SO_PASSCRED is enabled.  The I/O layer
            # accepts that record only on an empty EOF read; every data-bearing
            # credential still requires a positive PID and exact peer match.
            if pid < 0:
                _reject("credentials-shape-invalid")
            credentials.append(PeerCredentials(pid, uid, gid))
            continue
        _reject("ancillary-type-invalid")

    return AncillaryBatch(tuple(rights), tuple(credentials), message_flags)


def encode_request_frame(raw_transport: bytes) -> bytes:
    """Length-prefix exact request-authority bytes without re-encoding them."""

    if type(raw_transport) is not bytes:
        _reject("request-bytes-required")
    if not raw_transport:
        _reject("request-empty-payload")
    if len(raw_transport) > MAX_PAYLOAD_BYTES:
        _reject("request-oversize-payload")
    return len(raw_transport).to_bytes(FRAME_HEADER_BYTES, "big") + raw_transport


def _parse_exact_request(raw_transport: bytes) -> Any:
    expected_digest = request_authority.sha256_digest(raw_transport)
    try:
        parsed = request_authority.parse_transport(raw_transport)
    except request_authority.AuthorityModelError as error:
        raise SocketTransportError(f"request-invalid:{error.code}") from None
    except Exception:
        raise SocketTransportError("request-parse-failed") from None
    if (
        getattr(parsed, "raw_bytes", None) != raw_transport
        or getattr(parsed, "raw_digest", None) != expected_digest
    ):
        _reject("request-parser-binding-invalid")
    return parsed


def _validate_socket(sock: socket.socket, *, require_passcred: bool) -> None:
    if type(sock) is not socket.socket:
        _reject("unix-stream-required")
    try:
        if sock.family != socket.AF_UNIX:
            _reject("unix-stream-required")
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            _reject("unix-stream-required")
        sock.getpeername()
        if require_passcred and sock.getsockopt(
            socket.SOL_SOCKET, _linux_constant("SO_PASSCRED")
        ) != 1:
            _reject("passcred-required")
    except SocketTransportError:
        raise
    except OSError:
        raise SocketTransportError("connected-socket-required") from None


def _peer_credentials(sock: socket.socket) -> PeerCredentials:
    so_peercred = _linux_constant("SO_PEERCRED")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, _UCRED.size)
    except OSError:
        raise SocketTransportError("peer-credentials-invalid") from None
    if type(raw) is not bytes or len(raw) != _UCRED.size:
        _reject("peer-credentials-invalid")
    pid, uid, gid = _UCRED.unpack(raw)
    if pid <= 0:
        _reject("peer-credentials-invalid")
    return PeerCredentials(pid, uid, gid)


def inspect_response_pipe(fd: int, *, request_socket_fd: int) -> DescriptorMetadata:
    """Inspect an anonymous write pipe without taking descriptor ownership."""

    if type(fd) is not int or fd < 3 or type(request_socket_fd) is not int:
        _reject("response-pipe-invalid")
    try:
        descriptor_stat = os.fstat(fd)
        status_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise SocketTransportError("response-pipe-invalid") from None

    access_mode = status_flags & os.O_ACCMODE
    if (
        not stat.S_ISFIFO(descriptor_stat.st_mode)
        or access_mode != os.O_WRONLY
        or _PIPE_TARGET.fullmatch(target) is None
    ):
        _reject("response-pipe-invalid")
    if not descriptor_flags & fcntl.FD_CLOEXEC:
        _reject("response-pipe-inheritable")

    identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
    for other_fd in (0, 1, 2, request_socket_fd):
        if other_fd == fd:
            _reject("response-pipe-stdio-alias")
        try:
            other_stat = os.fstat(other_fd)
        except OSError:
            continue
        if identity == (other_stat.st_dev, other_stat.st_ino):
            _reject("response-pipe-stdio-alias")

    return DescriptorMetadata(
        fd=fd,
        mode=descriptor_stat.st_mode,
        device=descriptor_stat.st_dev,
        inode=descriptor_stat.st_ino,
        access_mode=access_mode,
        target=target,
        close_on_exec=True,
    )


def _wait_socket(
    sock: socket.socket,
    event: int,
    deadline: float,
    *,
    timeout_code: str,
    failure_code: str,
) -> None:
    poller = select.poll()
    try:
        poller.register(sock.fileno(), event | select.POLLERR | select.POLLHUP)
    except (OSError, ValueError):
        raise SocketTransportError(failure_code) from None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reject(timeout_code)
        try:
            events = poller.poll(max(1, math.ceil(remaining * 1000)))
        except InterruptedError:
            continue
        except OSError:
            raise SocketTransportError(failure_code) from None
        if not events:
            _reject(timeout_code)
        if any(flags & select.POLLNVAL for _fd, flags in events):
            _reject(failure_code)
        return


def _ancillary_buffer_size() -> int:
    try:
        return socket.CMSG_SPACE(SCM_MAX_FD * _FD_ITEMSIZE) + socket.CMSG_SPACE(
            _UCRED.size
        )
    except (AttributeError, OverflowError, OSError):
        _reject("linux-ancillary-contract-unavailable")


def _recv_batch(
    sock: socket.socket, maximum: int, deadline: float
) -> tuple[bytes, AncillaryBatch]:
    if type(maximum) is not int or maximum < 1:
        _reject("request-read-failed")
    recv_flags = _linux_constant("MSG_CMSG_CLOEXEC")
    while True:
        _wait_socket(
            sock,
            select.POLLIN,
            deadline,
            timeout_code="request-read-timeout",
            failure_code="request-read-failed",
        )
        try:
            data, ancillary, message_flags, _address = sock.recvmsg(
                maximum, _ancillary_buffer_size(), recv_flags
            )
        except InterruptedError:
            continue
        except BlockingIOError:
            continue
        except OSError:
            raise SocketTransportError("request-read-failed") from None
        delivered = _whole_rights(ancillary)
        try:
            batch = decode_ancillary(ancillary, message_flags)
        except BaseException:
            _close_fds(delivered)
            raise
        if type(data) is not bytes or len(data) > maximum:
            _close_fds(batch.rights)
            _reject("request-read-failed")
        return data, batch


def _validate_credentials(
    batch: AncillaryBatch,
    expected: PeerCredentials,
    *,
    first_byte: bool,
    eof: bool = False,
) -> None:
    if first_byte and len(batch.credentials) != 1:
        _reject("credentials-required")
    if len(batch.credentials) > 1:
        _reject("credentials-duplicate")
    if batch.credentials:
        observed = batch.credentials[0]
        if eof and observed.pid == 0:
            return
        if observed.pid <= 0 or observed != expected:
            _reject("credentials-mismatch")


def receive_request(
    sock: socket.socket,
    *,
    timeout_seconds: int | float = DEFAULT_IO_TIMEOUT_SECONDS,
) -> ReceivedRequest:
    """Consume one broker connection and return a non-authorizing candidate."""

    response_fd = -1
    try:
        deadline = _deadline(timeout_seconds)
        _validate_socket(sock, require_passcred=True)
        peer = _peer_credentials(sock)
        try:
            sock.setblocking(False)
        except OSError:
            _reject("request-read-failed")

        # A one-byte first recvmsg is an essential security invariant.  Linux
        # ancillary data is a stream barrier; a larger read could combine bytes
        # preceding a maliciously late SCM_RIGHTS message with that message.
        first_data, first_batch = _recv_batch(sock, 1, deadline)
        if not first_data:
            _close_fds(first_batch.rights)
            _reject("request-missing-frame")
        if len(first_batch.rights) != 1:
            _close_fds(first_batch.rights)
            _reject("response-fd-count-invalid")
        response_fd = first_batch.rights[0]
        _validate_credentials(first_batch, peer, first_byte=True)
        original_metadata = inspect_response_pipe(
            response_fd, request_socket_fd=sock.fileno()
        )

        header = bytearray(first_data)

        def next_bytes(maximum: int) -> bytes:
            data, batch = _recv_batch(sock, maximum, deadline)
            if batch.rights:
                _close_fds(batch.rights)
                _reject("response-fd-repeated")
            _validate_credentials(batch, peer, first_byte=False, eof=not data)
            return data

        while len(header) < FRAME_HEADER_BYTES:
            chunk = next_bytes(FRAME_HEADER_BYTES - len(header))
            if not chunk:
                _reject("request-truncated-header")
            header.extend(chunk)

        payload_length = int.from_bytes(header, "big", signed=False)
        if payload_length < 1:
            _reject("request-empty-payload")
        if payload_length > MAX_PAYLOAD_BYTES:
            _reject("request-oversize-payload")

        payload = bytearray()
        while len(payload) < payload_length:
            chunk = next_bytes(payload_length - len(payload))
            if not chunk:
                _reject("request-truncated-payload")
            payload.extend(chunk)

        trailing = next_bytes(1)
        if trailing:
            _reject("request-trailing-data")

        exact_payload = bytes(payload)
        exact_frame = bytes(header) + exact_payload
        parsed = _parse_exact_request(exact_payload)
        final_metadata = inspect_response_pipe(
            response_fd, request_socket_fd=sock.fileno()
        )
        if final_metadata != original_metadata:
            _reject("response-pipe-raced")

        result = ReceivedRequest(
            exact_frame=exact_frame,
            exact_payload=exact_payload,
            raw_digest=request_authority.sha256_digest(exact_payload),
            peer_credentials=peer,
            parsed_transport=parsed,
            response_fd=response_fd,
        )
        response_fd = -1
        return result
    finally:
        if response_fd >= 0:
            _close_fd(response_fd)
        if type(sock) is socket.socket:
            try:
                sock.close()
            except OSError:
                pass


def send_request(
    sock: socket.socket,
    raw_transport: bytes,
    response_fd: int,
    *,
    timeout_seconds: int | float = DEFAULT_IO_TIMEOUT_SECONDS,
) -> SentRequest:
    """Send one request and one pipe descriptor, then half-close and close."""

    # Invalid fd 0/1/2 values are never adopted or closed: rejecting hostile
    # caller input must not destroy the supervisor's standard descriptors.
    owned_response_fd = (
        response_fd if type(response_fd) is int and response_fd >= 3 else -1
    )
    try:
        deadline = _deadline(timeout_seconds)
        _validate_socket(sock, require_passcred=False)
        exact_frame = encode_request_frame(raw_transport)
        parsed = _parse_exact_request(raw_transport)
        inspect_response_pipe(response_fd, request_socket_fd=sock.fileno())
        try:
            sock.setblocking(False)
        except OSError:
            _reject("request-write-failed")

        rights = array.array(_FD_ARRAY_TYPE, [response_fd]).tobytes()
        ancillary = [(socket.SOL_SOCKET, _linux_constant("SCM_RIGHTS"), rights)]
        no_signal = _linux_constant("MSG_NOSIGNAL")
        offset = 0
        while offset == 0:
            _wait_socket(
                sock,
                select.POLLOUT,
                deadline,
                timeout_code="request-write-timeout",
                failure_code="request-write-failed",
            )
            try:
                written = sock.sendmsg([exact_frame[:1]], ancillary, no_signal)
            except InterruptedError:
                continue
            except BlockingIOError:
                continue
            except OSError:
                raise SocketTransportError("request-write-failed") from None
            if type(written) is not int or written != 1:
                _reject("request-write-failed")
            offset = written
            # A positive sendmsg transferred the open-file-description reference.
            # Close the sender copy now so it cannot delay response-pipe EOF.
            _close_fd(response_fd)
            owned_response_fd = -1

        view = memoryview(exact_frame)
        while offset < len(view):
            _wait_socket(
                sock,
                select.POLLOUT,
                deadline,
                timeout_code="request-write-timeout",
                failure_code="request-write-failed",
            )
            try:
                written = sock.send(view[offset:], no_signal)
            except InterruptedError:
                continue
            except BlockingIOError:
                continue
            except OSError:
                raise SocketTransportError("request-write-failed") from None
            if type(written) is not int or written < 1 or written > len(view) - offset:
                _reject("request-write-failed")
            offset += written

        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            raise SocketTransportError("request-shutdown-failed") from None
        return SentRequest(
            exact_frame=exact_frame,
            raw_digest=request_authority.sha256_digest(raw_transport),
            parsed_transport=parsed,
        )
    finally:
        if owned_response_fd >= 0:
            _close_fd(owned_response_fd)
        if type(sock) is socket.socket:
            try:
                sock.close()
            except OSError:
                pass


def describe_contract() -> dict[str, Any]:
    return {
        "schema": "propertyquarry.release.socket-transport-model.v2",
        "version": 2,
        "authoritative": False,
        "platform": "linux-af-unix-stream",
        "installed_socket_path": INSTALLED_SOCKET_PATH,
        "request_frame": "uint32-be-length-plus-exact-request-authority-bytes",
        "response_channel": "one-anonymous-write-pipe-via-scm-rights-on-byte-zero",
        "credentials": "scm-credentials-exactly-bound-to-so-peercred",
        "descriptor_adoption": "recvmsg-msg-cmsg-cloexec-and-metadata-revalidation",
        "connection_cardinality": "one-request-then-required-write-half-close",
        "authority": "none-transport-candidate-only",
    }


__all__ = [
    "AncillaryBatch",
    "DescriptorMetadata",
    "FRAME_HEADER_BYTES",
    "INSTALLED_SOCKET_PATH",
    "MAX_FRAME_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PeerCredentials",
    "ReceivedRequest",
    "SentRequest",
    "SocketTransportError",
    "decode_ancillary",
    "describe_contract",
    "encode_request_frame",
    "inspect_response_pipe",
    "receive_request",
    "send_request",
]
