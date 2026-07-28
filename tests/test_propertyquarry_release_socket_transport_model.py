from __future__ import annotations

import array
import dataclasses
import os
import socket
import threading
import time
from types import SimpleNamespace
from typing import Callable

import pytest

from scripts import propertyquarry_release_socket_transport_model as transport


RAW_REQUEST = b'{ "request" : "exact bytes" }\n'


def _valid_request() -> bytes:
    authority = transport.request_authority
    identity = {
        "audience": "propertyquarry-release-v2",
        "repository": "owner/property",
        "ref": "refs/heads/main",
        "candidate_sha": "a" * 40,
        "workflow_ref": (
            "owner/property/.github/workflows/"
            "propertyquarry-release-v2.yml@refs/heads/main"
        ),
        "workflow_sha": "b" * 40,
        "run_id": "424242",
        "run_attempt": 1,
        "job": "propertyquarry-release-v2",
        "environment": "propertyquarry-production",
    }
    envelope = {
        "operation": "release-preflight",
        "request_id": "socket-request-1",
        "nonce": "nonce-socket-request-1",
        "issued_at": 1_000,
        "expires_at": 1_100,
        "identity": identity,
    }
    digest = authority.sha256_digest(authority.canonical_bytes(envelope))
    return authority.canonical_bytes(
        {
            "schema": authority.REQUEST_SCHEMA,
            "envelope": envelope,
            "envelope_digest": digest,
            "request_signature": "sig:transport-conformance-test",
        }
    )


def _fake_parse(raw: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        raw_bytes=raw,
        raw_digest=transport.request_authority.sha256_digest(raw),
    )


@pytest.fixture
def fake_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.request_authority, "parse_transport", _fake_parse)


def _stream_pair(*, passcred: bool = True) -> tuple[socket.socket, socket.socket]:
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    if passcred:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    return client, server


def _pipe() -> tuple[int, int]:
    return os.pipe2(os.O_CLOEXEC)


def _rights(*descriptors: int) -> list[tuple[int, int, bytes]]:
    encoded = array.array("i", descriptors).tobytes()
    return [(socket.SOL_SOCKET, socket.SCM_RIGHTS, encoded)]


def _send_wire(
    client: socket.socket,
    wire: bytes,
    descriptors: tuple[int, ...],
    *,
    shutdown: bool = True,
) -> None:
    try:
        written = client.sendmsg([wire], _rights(*descriptors), socket.MSG_NOSIGNAL)
        assert written == len(wire)
        if shutdown:
            client.shutdown(socket.SHUT_WR)
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if shutdown:
            client.close()


def _assert_pipe_eof(reader_fd: int) -> None:
    os.set_blocking(reader_fd, False)
    deadline = time.monotonic() + 1
    while True:
        try:
            assert os.read(reader_fd, 1) == b""
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                pytest.fail("a rejected transport leaked a response-pipe writer")
            time.sleep(0.005)


def _run_roundtrip(
    raw: bytes = RAW_REQUEST,
) -> tuple[transport.SentRequest, transport.ReceivedRequest, int]:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    sent: list[transport.SentRequest] = []
    failures: list[BaseException] = []

    def sender() -> None:
        try:
            sent.append(transport.send_request(client, raw, writer_fd))
        except BaseException as error:  # pragma: no cover - asserted by caller
            failures.append(error)

    thread = threading.Thread(target=sender)
    thread.start()
    received = transport.receive_request(server)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert failures == []
    assert len(sent) == 1
    return sent[0], received, reader_fd


def test_contract_is_explicitly_non_authoritative() -> None:
    contract = transport.describe_contract()
    assert contract["schema"] == "propertyquarry.release.socket-transport-model.v2"
    assert contract["version"] == 2
    assert contract["authoritative"] is False
    assert contract["installed_socket_path"] == transport.INSTALLED_SOCKET_PATH
    assert transport.INSTALLED_SOCKET_PATH == (
        "/run/propertyquarry-release-control-v2/request.sock"
    )
    assert contract["authority"] == "none-transport-candidate-only"
    assert contract["response_channel"].endswith("scm-rights-on-byte-zero")


def test_frame_preserves_exact_bytes_and_enforces_bounds() -> None:
    frame = transport.encode_request_frame(RAW_REQUEST)
    assert frame[:4] == len(RAW_REQUEST).to_bytes(4, "big")
    assert frame[4:] == RAW_REQUEST

    for value, code in (
        (b"", "request-empty-payload"),
        (bytearray(b"{}"), "request-bytes-required"),
        (b"x" * (transport.MAX_PAYLOAD_BYTES + 1), "request-oversize-payload"),
    ):
        with pytest.raises(transport.SocketTransportError) as error:
            transport.encode_request_frame(value)  # type: ignore[arg-type]
        assert error.value.code == code


def test_decode_ancillary_accepts_one_right_and_credentials() -> None:
    credentials = transport._UCRED.pack(os.getpid(), os.getuid(), os.getgid())
    batch = transport.decode_ancillary(
        _rights(123)
        + [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credentials)],
        socket.MSG_CMSG_CLOEXEC,
    )
    assert batch.rights == (123,)
    assert batch.credentials == (
        transport.PeerCredentials(os.getpid(), os.getuid(), os.getgid()),
    )


@pytest.mark.parametrize("size", [0, 1, 2, 3, 5])
def test_decode_ancillary_rejects_malformed_rights(size: int) -> None:
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, b"x" * size)]
    with pytest.raises(transport.SocketTransportError) as error:
        transport.decode_ancillary(ancillary, socket.MSG_CMSG_CLOEXEC)
    assert error.value.code == "response-fd-count-invalid"


@pytest.mark.parametrize(
    ("ancillary", "flags", "code"),
    [
        ([], 0, "ancillary-flags-invalid"),
        ([], socket.MSG_CMSG_CLOEXEC | socket.MSG_CTRUNC, "ancillary-truncated"),
        ([(1, 999, b"")], socket.MSG_CMSG_CLOEXEC, "ancillary-type-invalid"),
        (
            [(socket.SOL_SOCKET, 999, b"")],
            socket.MSG_CMSG_CLOEXEC,
            "ancillary-type-invalid",
        ),
    ],
)
def test_decode_ancillary_rejects_flags_and_unknown_control(
    ancillary: list[tuple[int, int, bytes]], flags: int, code: str
) -> None:
    with pytest.raises(transport.SocketTransportError) as error:
        transport.decode_ancillary(ancillary, flags)
    assert error.value.code == code


@pytest.mark.parametrize("size", [0, 4, 8, 11, 13])
def test_decode_ancillary_rejects_malformed_credentials(size: int) -> None:
    ancillary = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"x" * size)]
    with pytest.raises(transport.SocketTransportError) as error:
        transport.decode_ancillary(ancillary, socket.MSG_CMSG_CLOEXEC)
    assert error.value.code == "credentials-shape-invalid"


def test_decode_ancillary_rejects_duplicate_control_messages() -> None:
    credential = transport._UCRED.pack(os.getpid(), os.getuid(), os.getgid())
    with pytest.raises(transport.SocketTransportError) as error:
        transport.decode_ancillary(
            _rights(10) + _rights(11), socket.MSG_CMSG_CLOEXEC
        )
    assert error.value.code == "response-fd-count-invalid"

    with pytest.raises(transport.SocketTransportError) as error:
        transport.decode_ancillary(
            [
                (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credential),
                (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credential),
            ],
            socket.MSG_CMSG_CLOEXEC,
        )
    assert error.value.code == "credentials-duplicate"


def test_response_pipe_metadata_requires_anonymous_cloexec_write_end(
    tmp_path,
) -> None:
    client, peer = _stream_pair()
    reader_fd, writer_fd = _pipe()
    try:
        metadata = transport.inspect_response_pipe(
            writer_fd, request_socket_fd=client.fileno()
        )
        assert metadata.access_mode == os.O_WRONLY
        assert metadata.target.startswith("pipe:[")

        with pytest.raises(transport.SocketTransportError) as error:
            transport.inspect_response_pipe(reader_fd, request_socket_fd=client.fileno())
        assert error.value.code == "response-pipe-invalid"

        regular_fd = os.open(tmp_path / "regular", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with pytest.raises(transport.SocketTransportError) as error:
                transport.inspect_response_pipe(
                    regular_fd, request_socket_fd=client.fileno()
                )
            assert error.value.code == "response-pipe-invalid"
        finally:
            os.close(regular_fd)

        os.set_inheritable(writer_fd, True)
        with pytest.raises(transport.SocketTransportError) as error:
            transport.inspect_response_pipe(writer_fd, request_socket_fd=client.fileno())
        assert error.value.code == "response-pipe-inheritable"
    finally:
        os.close(reader_fd)
        os.close(writer_fd)
        client.close()
        peer.close()


def test_live_roundtrip_preserves_digest_and_transfers_one_owned_pipe(
    fake_parser: None,
) -> None:
    sent, received, reader_fd = _run_roundtrip()
    try:
        assert sent.exact_frame == received.exact_frame
        assert received.exact_payload == RAW_REQUEST
        assert sent.raw_digest == received.raw_digest
        assert received.parsed_transport.raw_bytes == RAW_REQUEST
        os.write(received.response_fd, b"response")
        received.close()
        assert os.read(reader_fd, 8) == b"response"
        assert os.read(reader_fd, 1) == b""
    finally:
        received.close()
        os.close(reader_fd)


def test_live_roundtrip_uses_real_strict_request_authority_parser() -> None:
    raw = _valid_request()
    sent, received, reader_fd = _run_roundtrip(raw)
    try:
        assert received.exact_payload == raw
        assert received.parsed_transport.envelope.request_id == "socket-request-1"
        assert received.raw_digest == transport.request_authority.sha256_digest(raw)
        assert sent.parsed_transport == received.parsed_transport
    finally:
        received.close()
        os.close(reader_fd)


def test_received_request_can_detach_ownership(fake_parser: None) -> None:
    _sent, received, reader_fd = _run_roundtrip()
    writer_fd = received.detach_response_fd()
    try:
        assert received.response_fd == -1
        with pytest.raises(transport.SocketTransportError) as error:
            received.detach_response_fd()
        assert error.value.code == "response-fd-not-owned"
        os.write(writer_fd, b"x")
        assert os.read(reader_fd, 1) == b"x"
    finally:
        os.close(writer_fd)
        os.close(reader_fd)


def test_receiver_accepts_every_byte_segmented_after_first_rights_byte(
    fake_parser: None,
) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    wire = transport.encode_request_frame(RAW_REQUEST)
    failures: list[BaseException] = []

    def sender() -> None:
        try:
            assert client.sendmsg([wire[:1]], _rights(writer_fd)) == 1
            os.close(writer_fd)
            for byte in wire[1:]:
                client.sendall(bytes([byte]))
            client.shutdown(socket.SHUT_WR)
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)
        finally:
            client.close()

    thread = threading.Thread(target=sender)
    thread.start()
    received = transport.receive_request(server)
    thread.join(timeout=2)
    try:
        assert failures == []
        assert received.exact_payload == RAW_REQUEST
    finally:
        received.close()
        os.close(reader_fd)


def test_rights_attached_after_byte_zero_are_rejected(fake_parser: None) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    wire = transport.encode_request_frame(RAW_REQUEST)
    assert client.send(wire[:1]) == 1
    assert client.sendmsg([wire[1:]], _rights(writer_fd)) == len(wire) - 1
    os.close(writer_fd)
    client.shutdown(socket.SHUT_WR)
    client.close()
    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server)
    assert error.value.code == "response-fd-count-invalid"
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_first_batch_requires_exactly_one_response_fd(fake_parser: None) -> None:
    for count in (0, 2):
        client, server = _stream_pair()
        pipes = [_pipe() for _index in range(max(1, count))]
        readers = [pair[0] for pair in pipes]
        writers = tuple(pair[1] for pair in pipes[:count])
        wire = transport.encode_request_frame(RAW_REQUEST)
        if count:
            _send_wire(client, wire, writers)
        else:
            client.sendall(wire)
            client.shutdown(socket.SHUT_WR)
            client.close()
            os.close(pipes[0][1])
        with pytest.raises(transport.SocketTransportError) as error:
            transport.receive_request(server)
        assert error.value.code == "response-fd-count-invalid"
        for reader_fd in readers:
            _assert_pipe_eof(reader_fd)
            os.close(reader_fd)


def test_repeated_rights_during_payload_close_all_writers(fake_parser: None) -> None:
    client, server = _stream_pair()
    first_reader, first_writer = _pipe()
    second_reader, second_writer = _pipe()
    wire = transport.encode_request_frame(RAW_REQUEST)
    assert client.sendmsg([wire[:1]], _rights(first_writer)) == 1
    os.close(first_writer)
    client.sendmsg([wire[1:]], _rights(second_writer))
    os.close(second_writer)
    client.shutdown(socket.SHUT_WR)
    client.close()

    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server)
    assert error.value.code == "response-fd-repeated"
    for reader_fd in (first_reader, second_reader):
        _assert_pipe_eof(reader_fd)
        os.close(reader_fd)


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        (b"\x00", "request-truncated-header"),
        ((3).to_bytes(4, "big") + b"x", "request-truncated-payload"),
        ((1).to_bytes(4, "big") + b"xy", "request-trailing-data"),
        ((0).to_bytes(4, "big"), "request-empty-payload"),
        (
            (transport.MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big"),
            "request-oversize-payload",
        ),
    ],
)
def test_receiver_rejects_frame_boundaries_and_closes_pipe(
    fake_parser: None, wire: bytes, code: str
) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    _send_wire(client, wire, (writer_fd,))
    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server)
    assert error.value.code == code
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_receiver_requires_orderly_half_close(fake_parser: None) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    wire = transport.encode_request_frame(RAW_REQUEST)
    assert client.sendmsg([wire], _rights(writer_fd)) == len(wire)
    os.close(writer_fd)
    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server, timeout_seconds=0.03)
    assert error.value.code == "request-read-timeout"
    client.close()
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_receiver_requires_passcred_and_closes_received_writer(fake_parser: None) -> None:
    client, server = _stream_pair(passcred=False)
    reader_fd, writer_fd = _pipe()
    # The receiver rejects before recvmsg; the sender still owns the only writer.
    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server)
    assert error.value.code == "passcred-required"
    client.close()
    os.close(writer_fd)
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_receiver_revalidates_descriptor_identity_before_handoff(
    fake_parser: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = transport.inspect_response_pipe
    calls = 0

    def raced(fd: int, *, request_socket_fd: int) -> transport.DescriptorMetadata:
        nonlocal calls
        calls += 1
        metadata = original(fd, request_socket_fd=request_socket_fd)
        if calls == 2:
            return dataclasses.replace(metadata, inode=metadata.inode + 1)
        return metadata

    monkeypatch.setattr(transport, "inspect_response_pipe", raced)
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    _send_wire(client, transport.encode_request_frame(RAW_REQUEST), (writer_fd,))
    with pytest.raises(transport.SocketTransportError) as error:
        transport.receive_request(server)
    assert error.value.code == "response-pipe-raced"
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_request_parser_failure_is_typed_and_payload_redacted() -> None:
    with pytest.raises(transport.SocketTransportError) as error:
        transport._parse_exact_request(b"{secret")
    assert error.value.code.startswith("request-invalid:")
    assert "secret" not in str(error.value)


def test_request_parser_must_return_exact_bytes_and_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transport.request_authority,
        "parse_transport",
        lambda raw: SimpleNamespace(raw_bytes=raw + b"x", raw_digest="sha256:" + "0" * 64),
    )
    with pytest.raises(transport.SocketTransportError) as error:
        transport._parse_exact_request(RAW_REQUEST)
    assert error.value.code == "request-parser-binding-invalid"


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), 61])
def test_io_timeout_is_strict_and_bounded(timeout: object) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    with pytest.raises(transport.SocketTransportError) as error:
        transport.send_request(client, RAW_REQUEST, writer_fd, timeout_seconds=timeout)  # type: ignore[arg-type]
    assert error.value.code == "invalid-io-timeout"
    server.close()
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


def test_non_stream_socket_is_rejected_and_owned_descriptors_close(
    fake_parser: None,
) -> None:
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    reader_fd, writer_fd = _pipe()
    with pytest.raises(transport.SocketTransportError) as error:
        transport.send_request(client, RAW_REQUEST, writer_fd)
    assert error.value.code == "unix-stream-required"
    server.close()
    _assert_pipe_eof(reader_fd)
    os.close(reader_fd)


@pytest.mark.parametrize("invalid_fd", [1, True])
def test_invalid_stdio_descriptor_is_rejected_without_closing_stdout(
    fake_parser: None, invalid_fd: object
) -> None:
    before = os.fstat(1)
    client, server = _stream_pair()
    with pytest.raises(transport.SocketTransportError) as error:
        transport.send_request(client, RAW_REQUEST, invalid_fd)  # type: ignore[arg-type]
    assert error.value.code == "response-pipe-invalid"
    after = os.fstat(1)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    server.close()


def test_peer_credentials_must_match_every_observed_batch() -> None:
    expected = transport.PeerCredentials(100, 200, 300)
    mismatched = transport.AncillaryBatch(
        (), (transport.PeerCredentials(101, 200, 300),), socket.MSG_CMSG_CLOEXEC
    )
    with pytest.raises(transport.SocketTransportError) as error:
        transport._validate_credentials(mismatched, expected, first_byte=False)
    assert error.value.code == "credentials-mismatch"

    missing = transport.AncillaryBatch((), (), socket.MSG_CMSG_CLOEXEC)
    with pytest.raises(transport.SocketTransportError) as error:
        transport._validate_credentials(missing, expected, first_byte=True)
    assert error.value.code == "credentials-required"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux fork semantics required")
def test_inherited_socket_child_sender_is_rejected_by_credentials(
    fake_parser: None,
) -> None:
    client, server = _stream_pair()
    reader_fd, writer_fd = _pipe()
    wire = transport.encode_request_frame(RAW_REQUEST)
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions run in parent
        try:
            server.close()
            client.sendmsg([wire], _rights(writer_fd))
            client.shutdown(socket.SHUT_WR)
        finally:
            os._exit(0)

    client.close()
    os.close(writer_fd)
    try:
        with pytest.raises(transport.SocketTransportError) as error:
            transport.receive_request(server)
        assert error.value.code == "credentials-mismatch"
    finally:
        os.waitpid(child, 0)
        _assert_pipe_eof(reader_fd)
        os.close(reader_fd)
