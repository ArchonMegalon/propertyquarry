from __future__ import annotations

import importlib.util
import math
import os
import threading
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "propertyquarry_controller_response_frame.py"
SPEC = importlib.util.spec_from_file_location("propertyquarry_controller_response_frame", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
frame = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frame)


def _raw_frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _response(response_class: str, operation: str) -> dict[str, Any]:
    return {
        "schema": frame.LIFECYCLE_RESPONSE_SCHEMA,
        "version": frame.LIFECYCLE_RESPONSE_VERSION,
        "response": {"class": response_class, "operation": operation},
        "signature": {},
    }


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def test_encode_is_deterministic_and_decode_round_trips_strict_json() -> None:
    document = {
        "z": [True, None, 1.5],
        "a": "Grüß Gott",
        "integer_bounds": [frame.MIN_JSON_INTEGER, frame.MAX_JSON_INTEGER],
    }
    encoded = frame.encode_frame(document)

    assert int.from_bytes(encoded[:4], "big") == len(encoded) - 4
    assert encoded[4:] == (
        '{"a":"Grüß Gott","integer_bounds":'
        '[-9223372036854775808,9223372036854775807],'
        '"z":[true,null,1.5]}'
    ).encode()
    assert frame.decode_frame(encoded) == document
    assert frame.decode_frame(bytearray(encoded)) == document
    assert frame.decode_frame(memoryview(encoded)) == document


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "missing-frame"),
        (b"\x00", "truncated-header"),
        (b"\x00\x00\x00", "truncated-header"),
        ((0).to_bytes(4, "big"), "empty-payload"),
        ((frame.MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big"), "oversize-payload"),
        ((3).to_bytes(4, "big") + b"{}", "truncated-payload"),
        (_raw_frame(b"{}") + b"x", "trailing-data"),
        (_raw_frame(b"{}") + _raw_frame(b"{}"), "trailing-data"),
        (_raw_frame(b"\xef\xbb\xbf{}"), "utf-8-bom-forbidden"),
        (_raw_frame(b'{"x":"\xff"}'), "invalid-utf-8"),
        (_raw_frame(b'{"x":'), "invalid-json"),
        (_raw_frame(b"[]"), "top-level-object-required"),
        (_raw_frame(b"true"), "top-level-object-required"),
        (_raw_frame(b'{"x":1,"x":2}'), "duplicate-object-key"),
        (_raw_frame(b'{"x":{"secret":1,"secret":2}}'), "duplicate-object-key"),
        (_raw_frame(b'{"x":NaN}'), "non-finite-number"),
        (_raw_frame(b'{"x":Infinity}'), "non-finite-number"),
        (_raw_frame(b'{"x":-Infinity}'), "non-finite-number"),
        (_raw_frame(b'{"x":1e1000000}'), "non-finite-number"),
        (_raw_frame(b'{"x":"\\ud800"}'), "unicode-surrogate-forbidden"),
        (_raw_frame(b'{"x":9223372036854775808}'), "json-integer-out-of-range"),
        (_raw_frame(b'{"x":-9223372036854775809}'), "json-integer-out-of-range"),
    ],
)
def test_decode_rejects_hostile_or_ambiguous_frames(raw: bytes, code: str) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.decode_frame(raw)
    assert error.value.code == code
    assert str(error.value) == f"controller response rejected: {code}"


def test_decode_rejects_excessive_nesting_before_structural_validation() -> None:
    nesting = frame.MAX_JSON_NESTING + 1
    payload = (b'{"x":' * nesting) + b"0" + (b"}" * nesting)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.decode_frame(_raw_frame(payload))
    assert error.value.code == "excessive-json-nesting"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        ([], "top-level-object-required"),
        ({"x": math.nan}, "non-finite-number"),
        ({"x": math.inf}, "non-finite-number"),
        ({1: "not a JSON object key"}, "non-string-object-key"),
        ({"x": object()}, "non-json-value"),
        ({"x": "\ud800"}, "unicode-surrogate-forbidden"),
        ({"x": 2**63}, "json-integer-out-of-range"),
        ({"x": -(2**63) - 1}, "json-integer-out-of-range"),
    ],
)
def test_encode_rejects_values_that_cannot_form_strict_json(
    document: Any, code: str
) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.encode_frame(document)
    assert error.value.code == code


def test_encode_rejects_cycles_and_excessive_payloads() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.encode_frame(cyclic)
    assert error.value.code == "circular-json-value"

    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.encode_frame({"payload": "x" * frame.MAX_PAYLOAD_BYTES})
    assert error.value.code == "oversize-payload"


def test_fd_reader_handles_partial_reads_and_waits_for_eof() -> None:
    expected = _response("ready", "release-preflight")
    encoded = frame.encode_frame(expected)
    reader_fd, writer_fd = os.pipe()
    result: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def reader() -> None:
        try:
            result.append(frame.read_fd(reader_fd))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    for byte in encoded:
        os.write(writer_fd, bytes([byte]))

    thread.join(timeout=0.05)
    assert thread.is_alive(), "reader accepted a frame before the dedicated FD reached EOF"
    os.close(writer_fd)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert result == [expected]


def test_fd_reader_preserves_exact_signed_transport_bytes_without_reencoding() -> None:
    payload = (
        b'{ "version" : 2, "schema" : '
        b'"propertyquarry.release.lifecycle-response", '
        b'"response" : {"operation":"release-preflight","class":"ready"}, '
        b'"signature" : {} }'
    )
    exact_frame = _raw_frame(payload)
    reader_fd, writer_fd = os.pipe()
    os.write(writer_fd, exact_frame)
    os.close(writer_fd)

    document, retained = frame.read_fd_frame(reader_fd)

    assert document == _response("ready", "release-preflight")
    assert retained == exact_frame
    assert retained != frame.encode_frame(document)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "missing-frame"),
        (b"\x00\x00", "truncated-header"),
        ((5).to_bytes(4, "big") + b"{}", "truncated-payload"),
        (_raw_frame(b"{}") + b"x", "trailing-data"),
    ],
)
def test_fd_reader_rejects_zero_truncated_and_trailing_frames(
    raw: bytes, code: str
) -> None:
    reader_fd, writer_fd = os.pipe()
    os.write(writer_fd, raw)
    os.close(writer_fd)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(reader_fd)
    assert error.value.code == code


def test_fd_reader_rejects_oversize_from_header_without_unbounded_read() -> None:
    reader_fd, writer_fd = os.pipe()
    os.write(writer_fd, (frame.MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big"))
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(reader_fd)
    assert error.value.code == "oversize-payload"
    os.close(writer_fd)


@pytest.mark.parametrize("fd", [True, False, -1, 0, 1, 2, "3"])
def test_fd_helpers_refuse_stdio_invalid_and_boolean_descriptors(fd: Any) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as read_error:
        frame.read_fd(fd)
    assert read_error.value.code == "dedicated-fd-required"

    with pytest.raises(frame.ControllerResponseFrameError) as write_error:
        frame.write_fd(fd, {})
    assert write_error.value.code == "dedicated-fd-required"


def test_fd_reader_redacts_operating_system_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    reader_fd, writer_fd = os.pipe()
    os.write(writer_fd, b"x")

    def fail_read(_fd: int, _maximum: int) -> bytes:
        raise OSError("secret controller path and credential")

    monkeypatch.setattr(frame.os, "read", fail_read)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(reader_fd)
    assert str(error.value) == "controller response rejected: fd-read-failed"
    os.close(writer_fd)


def test_fd_reader_has_a_bounded_deadline_while_waiting_for_required_eof() -> None:
    reader_fd, writer_fd = os.pipe()
    os.write(writer_fd, _raw_frame(b"{}"))

    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(reader_fd, timeout_seconds=0.02)
    assert error.value.code == "fd-read-timeout"
    os.close(writer_fd)


@pytest.mark.parametrize("timeout", [True, False, 0, -1, math.inf, math.nan, 301, "1"])
def test_fd_reader_rejects_boolean_or_unbounded_deadlines(timeout: Any) -> None:
    reader_fd, writer_fd = os.pipe()
    try:
        with pytest.raises(frame.ControllerResponseFrameError) as error:
            frame.read_fd(reader_fd, timeout_seconds=timeout)
        assert error.value.code == "invalid-io-timeout"
    finally:
        # Invalid timeout is rejected before read_fd takes ownership of the FD.
        os.close(reader_fd)
        os.close(writer_fd)


def test_fd_helpers_require_anonymous_pipe_end_in_the_correct_direction(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "not-a-response-pipe"
    regular.write_bytes(b"{}")

    read_regular = os.open(regular, os.O_RDONLY)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(read_regular)
    assert error.value.code == "dedicated-fd-required"
    os.close(read_regular)

    write_regular = os.open(regular, os.O_WRONLY)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.write_fd(write_regular, {})
    assert error.value.code == "dedicated-fd-required"
    os.close(write_regular)

    reader_fd, writer_fd = os.pipe()
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.read_fd(writer_fd)
    assert error.value.code == "dedicated-fd-required"
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.write_fd(reader_fd, {})
    assert error.value.code == "dedicated-fd-required"
    os.close(reader_fd)
    os.close(writer_fd)


@pytest.mark.parametrize("write_end", [False, True])
def test_adopt_fd_immediately_sets_and_verifies_cloexec(write_end: bool) -> None:
    reader_fd, writer_fd = os.pipe()
    adopted_fd = writer_fd if write_end else reader_fd
    other_fd = reader_fd if write_end else writer_fd
    os.set_inheritable(adopted_fd, True)
    assert os.get_inheritable(adopted_fd) is True

    assert frame.adopt_fd(adopted_fd, write_end=write_end) == adopted_fd
    assert os.get_inheritable(adopted_fd) is False
    os.close(adopted_fd)
    os.close(other_fd)


def test_adopt_fd_closes_pipe_and_redacts_set_inheritable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()

    def fail_set_inheritable(_fd: int, _inheritable: bool) -> None:
        raise OSError("secret inherited controller path")

    monkeypatch.setattr(frame.os, "set_inheritable", fail_set_inheritable)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.adopt_fd(writer_fd, write_end=True)
    assert str(error.value) == "controller response rejected: fd-adoption-failed"
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)


def test_adopt_fd_closes_pipe_when_cloexec_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()
    monkeypatch.setattr(frame.os, "get_inheritable", lambda _fd: True)

    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.adopt_fd(writer_fd, write_end=True)
    assert error.value.code == "fd-adoption-failed"
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)


def test_adopt_fd_rejects_boolean_direction_before_taking_ownership() -> None:
    reader_fd, writer_fd = os.pipe()
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.adopt_fd(writer_fd, write_end=1)
    assert error.value.code == "fd-direction-invalid"
    os.close(reader_fd)
    os.close(writer_fd)


def test_fd_writer_rejects_a_duplicate_of_stdout_as_authority() -> None:
    duplicate_stdout = os.dup(1)
    try:
        with pytest.raises(frame.ControllerResponseFrameError) as error:
            frame.write_fd(duplicate_stdout, {})
        assert error.value.code == "dedicated-fd-required"
    finally:
        os.close(duplicate_stdout)


def test_fd_writer_retries_interrupts_and_partial_writes_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _response("sealed-final", "release-run")
    encoded = frame.encode_frame(expected)
    reader_fd, writer_fd = os.pipe()
    real_write = os.write
    calls = 0

    def partial_write(fd: int, payload: memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError
        return real_write(fd, payload[:3])

    monkeypatch.setattr(frame.os, "write", partial_write)
    frame.write_fd(writer_fd, expected)
    received = _read_all(reader_fd)

    assert calls > 2
    assert received == encoded
    assert frame.decode_frame(received) == expected


def test_fd_frame_forwarder_preserves_exact_noncanonical_transport_bytes() -> None:
    payload = (
        b'{ "signature" : {}, "response" : {'
        b'"operation":"release-run","class":"sealed-final"},'
        b'"version":2,"schema":"propertyquarry.release.lifecycle-response" }'
    )
    exact = _raw_frame(payload)
    reader_fd, writer_fd = os.pipe()

    frame.write_fd_frame(writer_fd, exact)

    received = _read_all(reader_fd)
    assert received == exact
    assert frame.decode_frame(received) == _response("sealed-final", "release-run")


def test_exit_coupled_forwarder_withholds_eof_until_broker_exit_teardown() -> None:
    exact = frame.encode_frame(_response("sealed-final", "release-run"))
    reader_fd, writer_fd = os.pipe()
    os.set_blocking(reader_fd, False)

    retained = frame.write_fd_frame_for_exit(writer_fd, exact)

    assert retained == writer_fd
    assert os.read(reader_fd, len(exact)) == exact
    with pytest.raises(BlockingIOError):
        os.read(reader_fd, 1)
    os.close(retained)  # Models kernel close-on-_exit after no further work.
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)


@pytest.mark.parametrize(
    ("invalid", "code"),
    [
        (bytearray(b"{}"), "frame-bytes-required"),
        (b"", "missing-frame"),
        (_raw_frame(b"{}") + b"x", "trailing-data"),
    ],
)
def test_fd_frame_forwarder_rejects_invalid_exact_bytes_and_closes(
    invalid: object, code: str
) -> None:
    reader_fd, writer_fd = os.pipe()
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.write_fd_frame(writer_fd, invalid)
    assert error.value.code == code
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)


def test_fd_writer_closes_on_encode_and_write_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_fd, writer_fd = os.pipe()
    with pytest.raises(frame.ControllerResponseFrameError):
        frame.write_fd(writer_fd, [])
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)

    reader_fd, writer_fd = os.pipe()

    def fail_write(_fd: int, _payload: memoryview) -> int:
        raise OSError("secret response contents")

    monkeypatch.setattr(frame.os, "write", fail_write)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.write_fd(writer_fd, {})
    assert str(error.value) == "controller response rejected: fd-write-failed"
    assert os.read(reader_fd, 1) == b""
    os.close(reader_fd)


def test_fd_writer_has_a_bounded_deadline_when_reader_stalls() -> None:
    reader_fd, writer_fd = os.pipe()
    document = {"payload": "x" * 200_000}
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.write_fd(writer_fd, document, timeout_seconds=0.02)
    assert error.value.code == "fd-write-timeout"
    os.close(reader_fd)


@pytest.mark.parametrize("timeout", [True, False, 0, -1, math.inf, math.nan, 301, "1"])
def test_fd_writer_rejects_boolean_or_unbounded_deadlines(timeout: Any) -> None:
    reader_fd, writer_fd = os.pipe()
    try:
        with pytest.raises(frame.ControllerResponseFrameError) as error:
            frame.write_fd(writer_fd, {}, timeout_seconds=timeout)
        assert error.value.code == "invalid-io-timeout"
    finally:
        os.close(reader_fd)
        os.close(writer_fd)


@pytest.mark.parametrize(
    ("exit_code", "response_class", "operation", "structurally_eligible"),
    [
        (0, "ready", "release-preflight", True),
        (0, "sealed-final", "release-run", True),
        (10, "not-ready", "release-preflight", False),
        (10, "indeterminate", "release-preflight", False),
        (20, "rejected", "release-run", False),
        (20, "rejected", "reconcile-run", False),
        (30, "rolled-back", "release-run", False),
        (30, "rolled-back", "reconcile-run", False),
        (31, "contained-failed", "release-run", False),
        (31, "contained-failed", "reconcile-run", False),
        (40, "conflict", "release-run", False),
        (40, "conflict", "reconcile-run", False),
    ],
)
def test_fixed_exit_classes_match_only_their_signed_response_class(
    exit_code: int,
    response_class: str,
    operation: str,
    structurally_eligible: bool,
) -> None:
    assert (
        frame.validate_exit_response(
            exit_code,
            _response(response_class, operation),
            expected_operation=operation,
        )
        is structurally_eligible
    )


@pytest.mark.parametrize(
    ("response_class", "operation"),
    [
        ("ready", "release-run"),
        ("not-ready", "reconcile-run"),
        ("indeterminate", "release-run"),
        ("sealed-final", "release-preflight"),
        ("sealed-final", "reconcile-run"),
        ("rejected", "release-preflight"),
        ("rolled-back", "release-preflight"),
        ("contained-failed", "release-preflight"),
        ("conflict", "release-preflight"),
    ],
)
def test_lifecycle_response_rejects_operation_class_confusion(
    response_class: str, operation: str
) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.lifecycle_response_discriminators(
            _response(response_class, operation)
        )
    assert error.value.code == "lifecycle-response-operation-class-mismatch"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(schema="wrong"), "lifecycle-response-schema-mismatch"),
        (lambda value: value.update(version=True), "lifecycle-response-version-mismatch"),
        (lambda value: value.update(response=[]), "lifecycle-response-body-required"),
        (lambda value: value.pop("signature"), "lifecycle-response-signature-required"),
        (lambda value: value.update(signature=[]), "lifecycle-response-signature-required"),
        (
            lambda value: value["response"].update({"class": "unknown"}),
            "lifecycle-response-class-invalid",
        ),
        (
            lambda value: value["response"].update({"class": True}),
            "lifecycle-response-class-invalid",
        ),
        (
            lambda value: value["response"].update(operation="unknown"),
            "lifecycle-response-operation-invalid",
        ),
        (
            lambda value: value["response"].update(operation=True),
            "lifecycle-response-operation-invalid",
        ),
    ],
)
def test_lifecycle_discriminator_shape_is_strict_but_does_not_claim_signature_trust(
    mutation: Any, code: str
) -> None:
    document = _response("ready", "release-preflight")
    mutation(document)
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.lifecycle_response_discriminators(document)
    assert error.value.code == code


def test_protocol_or_auth_failure_is_the_only_exit_permitting_no_response() -> None:
    assert (
        frame.validate_exit_response(
            50, None, expected_operation="release-preflight"
        )
        is False
    )

    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(
            50,
            _response("ready", "release-preflight"),
            expected_operation="release-preflight",
        )
    assert error.value.code == "exit-response-mismatch"

    for exit_code in (0, 10, 20, 30, 31, 40):
        with pytest.raises(frame.ControllerResponseFrameError) as error:
            frame.validate_exit_response(
                exit_code, None, expected_operation="release-run"
            )
        assert error.value.code == "signed-response-required"


@pytest.mark.parametrize(
    ("exit_code", "response_class", "operation"),
    [
        (0, "not-ready", "release-preflight"),
        (10, "ready", "release-preflight"),
        (20, "rolled-back", "release-run"),
        (30, "contained-failed", "release-run"),
        (31, "conflict", "release-run"),
        (40, "rejected", "release-run"),
    ],
)
def test_exit_response_mismatch_never_authorizes(
    exit_code: int, response_class: str, operation: str
) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(
            exit_code,
            _response(response_class, operation),
            expected_operation=operation,
        )
    assert error.value.code == "exit-response-mismatch"


@pytest.mark.parametrize("exit_code", [-15, 1, 11, 255, True, None])
def test_unknown_signal_like_and_boolean_exit_codes_never_authorize(
    exit_code: Any,
) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(
            exit_code,
            _response("ready", "release-preflight"),
            expected_operation="release-preflight",
        )
    assert error.value.code == "invalid-controller-exit"


@pytest.mark.parametrize(
    ("signaled", "timed_out"), [(True, False), (False, True), (True, True)]
)
def test_signal_or_timeout_never_authorizes_even_with_success_shaped_response(
    signaled: bool, timed_out: bool
) -> None:
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(
            0,
            _response("ready", "release-preflight"),
            expected_operation="release-preflight",
            signaled=signaled,
            timed_out=timed_out,
        )
    assert error.value.code == "unclean-process-termination"


def test_expected_operation_is_a_required_trusted_binding() -> None:
    sealed = _response("sealed-final", "release-run")
    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(
            0, sealed, expected_operation="release-preflight"
        )
    assert error.value.code == "exit-response-operation-mismatch"

    with pytest.raises(frame.ControllerResponseFrameError) as error:
        frame.validate_exit_response(0, sealed, expected_operation=True)
    assert error.value.code == "expected-operation-invalid"
