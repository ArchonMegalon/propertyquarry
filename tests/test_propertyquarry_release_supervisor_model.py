from __future__ import annotations

import inspect
import json
import os
from dataclasses import replace
from pathlib import Path
import signal
import time
from typing import Any

import pytest

from scripts import propertyquarry_release_supervisor_model as supervisor


EVENT_ID = "evt-supervisor-hostile-001"
REQUEST_DIGEST = "sha256:" + ("a" * 64)
POLICY_DIGEST = "sha256:" + ("b" * 64)
EXACT_PAYLOAD = (
    b'{ "version" : 2, "schema" : '
    b'"propertyquarry.release.lifecycle-response", "response" : '
    b'{"operation":"release-run","class":"sealed-final"}, '
    b'"signature" : {"fixture":"signed"} }'
)
EXACT_FRAME = len(EXACT_PAYLOAD).to_bytes(4, "big") + EXACT_PAYLOAD


CONTROLLER_FIXTURE = r'''#!/usr/bin/python3
import argparse
import json
import os
from pathlib import Path
import signal
import time

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--operation", required=True)
parser.add_argument("--response-fd", required=True, type=int)
parser.add_argument("--event-id", required=True)
parser.add_argument("--request-transport-digest", required=True)
args = parser.parse_args()
config = json.loads(Path(args.config).read_text(encoding="utf-8"))
behavior = config["behavior"]
response_fd = args.response_fd
os.set_inheritable(response_fd, False)

if config.get("controller_pid_file"):
    Path(config["controller_pid_file"]).write_text(str(os.getpid()), encoding="ascii")

def write_all(fd, data):
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        offset += os.write(fd, view[offset:])

frame = bytes.fromhex(config["frame_hex"])

if behavior == "zero-no-frame":
    raise SystemExit(0)
if behavior == "stdout-authority":
    write_all(1, frame)
    raise SystemExit(0)
if behavior == "malformed-json":
    write_all(response_fd, (1).to_bytes(4, "big") + b"{")
    raise SystemExit(0)
if behavior == "truncated":
    write_all(response_fd, (19).to_bytes(4, "big") + b"{}")
    raise SystemExit(0)
if behavior == "multiple":
    write_all(response_fd, frame + frame)
    raise SystemExit(0)
if behavior == "oversize":
    write_all(response_fd, (1_048_577).to_bytes(4, "big"))
    raise SystemExit(0)
if behavior == "signal":
    os.kill(os.getpid(), signal.SIGTERM)
    raise SystemExit(99)
if behavior == "timeout":
    time.sleep(60)
    raise SystemExit(99)
if behavior == "leaked-response-child":
    child_pid = os.fork()
    if child_pid == 0:
        if config.get("child_pid_file"):
            Path(config["child_pid_file"]).write_text(str(os.getpid()), encoding="ascii")
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        time.sleep(60)
        os._exit(99)
    write_all(response_fd, frame)
    raise SystemExit(0)
if behavior == "large-diagnostics":
    write_all(1, b"SENSITIVE-STDOUT-" * 20_000)
    write_all(2, b"SENSITIVE-STDERR-" * 20_000)
    write_all(response_fd, frame)
    raise SystemExit(int(config.get("exit_code", 0)))
if behavior == "valid":
    write_all(response_fd, frame)
    raise SystemExit(int(config.get("exit_code", 0)))
raise SystemExit(98)
'''


class LedgerRecorder:
    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls: list[supervisor.LedgerLookupQuery] = []

    def __call__(
        self, query: supervisor.LedgerLookupQuery
    ) -> supervisor.AuthenticatedLedgerReceipt | Any:
        self.calls.append(query)
        if self.mode == "raise":
            raise RuntimeError("ledger unavailable and secret detail")
        if self.mode == "truthy-object":
            return _Truthy()
        if self.mode == "explosive-field":
            return supervisor.AuthenticatedLedgerReceipt(
                schema=_ExplosiveComparison(),  # type: ignore[arg-type]
                version=2,
                query_digest=query.query_digest,
                authenticated=True,
                signature_verified=True,
                status="absent",
                record_digest=None,
            )
        authenticated: bool | int = True
        if self.mode == "truthy-bool":
            authenticated = 1
        return supervisor.AuthenticatedLedgerReceipt(
            schema="propertyquarry.release.ledger-lookup-receipt",
            version=2,
            query_digest=query.query_digest,
            authenticated=authenticated,  # type: ignore[arg-type]
            signature_verified=True,
            status="absent",
            record_digest=None,
        )


class _Truthy:
    def __bool__(self) -> bool:
        return True


class _ExplosiveComparison:
    def __eq__(self, _other: object) -> bool:
        raise RuntimeError("comparison must not run")


def _accepted_verification_receipt(
    context: supervisor.FullVerificationContext,
) -> supervisor.FullVerificationReceipt:
    return supervisor.FullVerificationReceipt(
        schema="propertyquarry.release.full-verification-receipt",
        version=2,
        context_digest=context.context_digest,
        event_id=context.event_id,
        request_transport_digest=context.request_transport_digest,
        exact_frame_digest=context.exact_frame_digest,
        policy_digest=context.expected_policy_digest,
        signature_verified=True,
        accepted=True,
    )


@pytest.fixture
def controller_contract(tmp_path: Path):
    executable = tmp_path / "hostile-controller"
    executable.write_text(CONTROLLER_FIXTURE, encoding="utf-8")
    executable.chmod(0o700)

    def make(behavior: str, **extra: Any):
        config = tmp_path / f"{behavior}-{len(list(tmp_path.iterdir()))}.json"
        document = {
            "behavior": behavior,
            "frame_hex": EXACT_FRAME.hex(),
            **extra,
        }
        config.write_text(json.dumps(document), encoding="utf-8")
        contract = supervisor._ControllerContract(
            executable=str(executable),
            config_path=str(config),
            contract_id="propertyquarry.test-controller-v2",
        )
        return contract, config

    return make


def _run(
    contract: supervisor._ControllerContract,
    *,
    verifier: Any = None,
    ledger: LedgerRecorder | None = None,
    process_timeout: float = 0.5,
    eof_timeout: float = 0.6,
    diagnostic_limit: int = 1024,
    expected_operation: str = "release-run",
) -> tuple[supervisor.SupervisorResult, LedgerRecorder]:
    ledger = ledger or LedgerRecorder()
    if verifier is None:
        verifier = (
            lambda _document, _frame, context: _accepted_verification_receipt(
                context
            )
        )
    result = supervisor._run_supervisor_with_contract(
        contract=contract,
        expected_operation=expected_operation,
        event_id=EVENT_ID,
        request_transport_digest=REQUEST_DIGEST,
        expected_policy_digest=POLICY_DIGEST,
        full_verifier=verifier,
        ledger_lookup=ledger,
        process_timeout_seconds=process_timeout,
        eof_timeout_seconds=eof_timeout,
        callback_timeout_seconds=0.2,
        cleanup_timeout_seconds=0.8,
        diagnostic_limit_bytes=diagnostic_limit,
    )
    return result, ledger


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        state = stat_path.read_text(encoding="ascii").split()[2]
    except (FileNotFoundError, IndexError, OSError):
        return False
    return state != "Z"


def _force_kill_from_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        pid = int(path.read_text(encoding="ascii"))
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, ValueError, OSError):
        pass


def test_server_broker_inner_controller_has_fixed_absolute_contract() -> None:
    assert supervisor.INSTALLED_CONTROLLER_EXECUTABLE.startswith(
        "/usr/libexec/propertyquarry-release-control/"
    )
    assert supervisor.INSTALLED_CONTROLLER_CONFIG.startswith(
        "/etc/propertyquarry-release-control/"
    )
    parameters = inspect.signature(supervisor.run_installed_controller).parameters
    assert "executable" not in parameters
    assert "config" not in parameters
    policy_parameter = parameters["expected_policy_digest"]
    assert policy_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert policy_parameter.default is inspect.Parameter.empty
    private_policy_parameter = inspect.signature(
        supervisor._run_supervisor_with_contract
    ).parameters["expected_policy_digest"]
    assert private_policy_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert private_policy_parameter.default is inspect.Parameter.empty
    described = supervisor.describe_contract()
    assert described["modeled_role"] == (
        "systemd-supervisor-broker-inner-controller-child"
    )
    assert described["workflow_client_transport"] == "separate-unix-socket-model"
    assert described["expected_policy_digest"] == (
        "required-trusted-root-owned-installed-input-not-request-derived"
    )
    assert "exact-installed-policy-bound" in described["full_verification"]
    with pytest.raises(ValueError):
        supervisor._ControllerContract("relative", "/absolute", "valid-id")


def test_exact_frame_is_retained_and_only_verified_candidate_is_returned(
    controller_contract,
) -> None:
    contract, _config = controller_contract("valid")
    seen: list[
        tuple[dict[str, Any], bytes, supervisor.FullVerificationContext]
    ] = []

    def verifier(
        document: dict[str, Any],
        frame: bytes,
        context: supervisor.FullVerificationContext,
    ) -> supervisor.FullVerificationReceipt:
        seen.append((document, frame, context))
        return _accepted_verification_receipt(context)

    result, ledger = _run(contract, verifier=verifier)
    assert result.disposition == "verified-success-candidate"
    assert result.protocol_eligible is True
    assert result.authorizes_release is False
    assert result.reconciliation_required is False
    assert result.exact_response_frame == EXACT_FRAME
    assert len(seen) == 1
    seen_document, seen_frame, seen_context = seen[0]
    assert seen_document == result.response_document
    assert seen_frame == EXACT_FRAME
    assert seen_context.expected_operation == "release-run"
    assert seen_context.event_id == EVENT_ID
    assert seen_context.request_transport_digest == REQUEST_DIGEST
    assert seen_context.exact_frame_digest.startswith("sha256:")
    assert seen_context.expected_policy_digest == POLICY_DIGEST
    assert result.full_verification_receipt == _accepted_verification_receipt(
        seen_context
    )
    assert ledger.calls == []

    assert result.response_document is not None
    with pytest.raises(TypeError):
        result.response_document["version"] = 99  # type: ignore[index]
    nested = result.response_document["response"]
    assert isinstance(nested, dict) is False
    with pytest.raises(TypeError):
        nested["class"] = "forged"  # type: ignore[index]


def test_request_digest_profile_is_prefixed_and_matches_request_authority() -> None:
    query = supervisor._ledger_query(
        event_id=EVENT_ID,
        request_transport_digest=REQUEST_DIGEST,
        expected_operation="release-run",
        incident_code="test-incident",
    )
    assert query.request_transport_digest == REQUEST_DIGEST
    assert query.query_digest.startswith("sha256:")
    contract = supervisor.describe_contract()
    assert contract["request_transport_digest"] == (
        "sha256-prefixed-lowercase-hex"
    )
    assert contract["authoritative"] is False
    assert "cgroup-or-pid-namespace" in contract["production_containment_required"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_digest", "sha256:" + ("0" * 64)),
        ("event_id", "different-event"),
        ("request_transport_digest", "sha256:" + ("0" * 64)),
        ("exact_frame_digest", "sha256:" + ("0" * 64)),
        ("policy_digest", "not-a-digest"),
        ("policy_digest", "sha256:" + ("c" * 64)),
        ("signature_verified", 1),
        ("accepted", _Truthy()),
    ],
)
def test_full_verification_receipt_is_exactly_context_bound(
    controller_contract,
    field: str,
    value: Any,
) -> None:
    contract, _config = controller_contract("valid")

    def verifier(
        _document: dict[str, Any],
        _frame: bytes,
        context: supervisor.FullVerificationContext,
    ) -> supervisor.FullVerificationReceipt:
        return replace(_accepted_verification_receipt(context), **{field: value})

    result, ledger = _run(contract, verifier=verifier)

    assert result.reason_code == "full-verifier-rejected"
    assert result.full_verification_receipt is None
    assert result.reconciliation_required is True
    assert len(ledger.calls) == 1


@pytest.mark.parametrize(
    "expected_policy_digest",
    [
        None,
        "",
        "b" * 64,
        "sha256:" + ("B" * 64),
        "sha256:" + ("b" * 63),
    ],
)
def test_expected_installed_policy_digest_is_strict_trusted_input(
    expected_policy_digest: Any,
) -> None:
    with pytest.raises(ValueError, match="expected installed policy digest"):
        supervisor._validated_inputs(
            expected_operation="release-run",
            event_id=EVENT_ID,
            request_transport_digest=REQUEST_DIGEST,
            expected_policy_digest=expected_policy_digest,
            diagnostic_limit_bytes=1024,
            full_verifier=lambda: None,
            ledger_lookup=lambda: None,
        )


def test_zero_exit_without_response_invokes_ledger_and_requires_reconciliation(
    controller_contract,
) -> None:
    contract, _config = controller_contract("zero-no-frame")
    result, ledger = _run(contract)
    assert result.reason_code == "response-missing-frame"
    assert result.reconciliation_required is True
    assert result.protocol_eligible is False
    assert result.authorizes_release is False
    assert result.ledger_lookup_authenticated is True
    assert len(ledger.calls) == 1


def test_stdout_can_never_supply_authority_and_is_fully_redacted(
    controller_contract,
) -> None:
    contract, _config = controller_contract("stdout-authority")
    result, ledger = _run(contract)
    assert result.response_document is None
    assert result.exact_response_frame is None
    assert result.stdout.byte_count == len(EXACT_FRAME)
    assert result.stdout.content == "<redacted>"
    assert b"sealed-final" not in repr(result).encode()
    assert result.reconciliation_required is True
    assert len(ledger.calls) == 1


@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ("malformed-json", "response-invalid-json"),
        ("truncated", "response-truncated-payload"),
        ("multiple", "response-trailing-data"),
        ("oversize", "response-oversize-payload"),
    ],
)
def test_malformed_truncated_multiple_and_oversize_frames_fail_closed(
    controller_contract, behavior: str, reason: str
) -> None:
    contract, _config = controller_contract(behavior)
    result, ledger = _run(contract)
    assert result.reason_code == reason
    assert result.disposition == "reconciliation-required"
    assert result.authorizes_release is False
    assert len(ledger.calls) == 1


def test_exit_response_mismatch_invokes_external_ledger(controller_contract) -> None:
    contract, _config = controller_contract("valid", exit_code=10)
    result, ledger = _run(contract)
    assert result.reason_code == "exit-response-mismatch"
    assert result.exit_code == 10
    assert result.reconciliation_required is True
    assert result.exact_response_frame == EXACT_FRAME
    assert len(ledger.calls) == 1


def test_signal_is_unclean_and_requires_reconciliation(controller_contract) -> None:
    contract, _config = controller_contract("signal")
    result, ledger = _run(contract)
    assert result.reason_code == "controller-signaled"
    assert result.exit_code == -signal.SIGTERM
    assert result.cleanup.direct_child_reaped is True
    assert result.authorizes_release is False
    assert len(ledger.calls) == 1


def test_absolute_process_timeout_kills_and_reaps_controller(
    controller_contract, tmp_path: Path
) -> None:
    controller_pid_file = tmp_path / "controller.pid"
    contract, _config = controller_contract(
        "timeout", controller_pid_file=str(controller_pid_file)
    )
    try:
        result, ledger = _run(
            contract, process_timeout=0.15, eof_timeout=0.35
        )
        assert result.reason_code == "controller-process-timeout"
        assert result.cleanup.direct_child_reaped is True
        assert result.cleanup.process_group_gone is True
        assert len(ledger.calls) == 1
        pid = int(controller_pid_file.read_text(encoding="ascii"))
        assert _pid_is_live(pid) is False
    finally:
        _force_kill_from_file(controller_pid_file)


def test_leaked_response_pipe_child_is_group_killed_and_reaped(
    controller_contract, tmp_path: Path
) -> None:
    child_pid_file = tmp_path / "descendant.pid"
    contract, _config = controller_contract(
        "leaked-response-child", child_pid_file=str(child_pid_file)
    )
    try:
        result, ledger = _run(
            contract, process_timeout=0.45, eof_timeout=0.18
        )
        assert result.reason_code in {
            "response-fd-read-timeout",
            "response-eof-leaked-child",
        }
        assert result.disposition == "reconciliation-required"
        assert result.cleanup.process_group_gone is True
        assert result.cleanup.adopted_children_reaped >= 1
        assert len(ledger.calls) == 1
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert _pid_is_live(child_pid) is False
    finally:
        _force_kill_from_file(child_pid_file)


def test_large_stdout_and_stderr_are_concurrently_drained_bounded_and_redacted(
    controller_contract,
) -> None:
    contract, _config = controller_contract("large-diagnostics")
    result, ledger = _run(
        contract,
        process_timeout=0.8,
        eof_timeout=0.9,
        diagnostic_limit=128,
    )
    assert result.disposition == "verified-success-candidate"
    assert result.stdout.byte_count > 100_000
    assert result.stderr.byte_count > 100_000
    assert result.stdout.omitted_byte_count == result.stdout.byte_count - 128
    assert result.stderr.omitted_byte_count == result.stderr.byte_count - 128
    assert result.stdout.content == result.stderr.content == "<redacted>"
    assert "SENSITIVE" not in repr(result)
    assert ledger.calls == []


@pytest.mark.parametrize("verifier_value", [False, 1, _Truthy()])
def test_full_verifier_rejection_and_truthy_non_true_values_fail_closed(
    controller_contract, verifier_value: Any
) -> None:
    contract, _config = controller_contract("valid")
    result, ledger = _run(
        contract,
        verifier=lambda _document, _frame, _operation: verifier_value,
    )
    assert result.reason_code == "full-verifier-rejected"
    assert result.full_verifier_accepted is False
    assert result.reconciliation_required is True
    assert result.exact_response_frame == EXACT_FRAME
    assert len(ledger.calls) == 1


@pytest.mark.parametrize(
    "ledger_mode",
    ["raise", "truthy-object", "truthy-bool", "explosive-field"],
)
def test_ledger_lookup_failure_or_truthy_non_true_authentication_stays_closed(
    controller_contract, ledger_mode: str
) -> None:
    contract, _config = controller_contract("zero-no-frame")
    ledger = LedgerRecorder(ledger_mode)
    result, ledger = _run(contract, ledger=ledger)
    assert result.reconciliation_required is True
    assert result.ledger_lookup_attempted is True
    assert result.ledger_lookup_authenticated is False
    assert result.ledger_receipt is None
    assert len(ledger.calls) == 1


def test_closed_non_success_response_is_verified_but_never_authorizing(
    controller_contract,
) -> None:
    payload = (
        b'{"response":{"class":"rejected","operation":"release-run"},'
        b'"schema":"propertyquarry.release.lifecycle-response",'
        b'"signature":{},"version":2}'
    )
    frame = len(payload).to_bytes(4, "big") + payload
    contract, config = controller_contract("valid", exit_code=20)
    config.write_text(
        json.dumps(
            {"behavior": "valid", "exit_code": 20, "frame_hex": frame.hex()}
        ),
        encoding="utf-8",
    )
    result, ledger = _run(contract)
    assert result.disposition == "verified-non-authorizing-terminal"
    assert result.protocol_eligible is False
    assert result.full_verifier_accepted is True
    assert result.authorizes_release is False
    assert result.reconciliation_required is False
    assert ledger.calls == []


def test_preflight_ready_is_protocol_eligible_but_never_authorizing(
    controller_contract,
) -> None:
    payload = (
        b'{"response":{"class":"ready","operation":"release-preflight"},'
        b'"schema":"propertyquarry.release.lifecycle-response",'
        b'"signature":{},"version":2}'
    )
    frame = len(payload).to_bytes(4, "big") + payload
    contract, config = controller_contract("valid")
    config.write_text(
        json.dumps({"behavior": "valid", "exit_code": 0, "frame_hex": frame.hex()}),
        encoding="utf-8",
    )

    result, ledger = _run(contract, expected_operation="release-preflight")

    assert result.disposition == "verified-success-candidate"
    assert result.protocol_eligible is True
    assert result.authorizes_release is False
    assert result.reconciliation_required is False
    assert ledger.calls == []


@pytest.mark.parametrize("mode", ["raise", "timeout"])
def test_verifier_exception_or_timeout_is_reconciliation_only(
    controller_contract,
    mode: str,
) -> None:
    contract, _config = controller_contract("valid")

    def verifier(
        _document: dict[str, Any],
        _frame: bytes,
        context: supervisor.FullVerificationContext,
    ) -> supervisor.FullVerificationReceipt:
        if mode == "raise":
            raise RuntimeError("verifier detail must be redacted")
        time.sleep(0.4)
        return _accepted_verification_receipt(context)

    result, ledger = _run(contract, verifier=verifier)

    assert result.reason_code == "full-verifier-rejected"
    assert result.authorizes_release is False
    assert result.full_verification_receipt is None
    assert result.reconciliation_required is True
    assert len(ledger.calls) == 1
