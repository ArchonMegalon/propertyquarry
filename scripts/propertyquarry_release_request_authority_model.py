#!/usr/bin/env python3
"""Non-authoritative request/replay/preflight reference model.

This executable model describes an external PropertyQuarry release-request
authority.  It does not validate a real OIDC token or signature, establish a
trust root, sign with a production key, read a production lifecycle seal, or
admit a production release.  Those capabilities are injected explicitly.

The durable replay record is the atomic source of truth for both request ID
and nonce consumption.  Exact raw-byte retries return the stored response only
after the current request signature and out-of-band OIDC bearer authenticate
and bind the exact signed-envelope, root-policy, and replay-record run identity.
An authenticated exact retry still bypasses clock, lifecycle-head, and mutable
policy-decision re-evaluation and repeats no external effect.

The OIDC bearer is an out-of-band ``handle`` argument.  It is never serialized
into request transport bytes or retained in durable replay state.  It and the
request signature are revalidated before every replay lookup.

Each model instance snapshots one immutable per-run root policy, including the
exact run identity and ordered check set.  The injected admission callback
first looks up the canonical policy-bound admission digest.  A stored result
is returned even if the volatile lifecycle head advanced after an earlier
external compare-and-swap.  Only an absent binding may require the newly
observed head to equal the ready preflight's immutable expected predecessor
before performing a new external compare-and-swap.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "propertyquarry.release-request-authority-model.v2"
REQUEST_SCHEMA = "propertyquarry.release-request.v2"
RESPONSE_SCHEMA = "propertyquarry.release-response.v2"
ROOT_POLICY_SCHEMA = "propertyquarry.release-root-policy.v2"
ROOT_POLICY_DIGEST_DOMAIN = b"propertyquarry.release-root-policy-digest.v2\0"
ADMISSION_BINDING_SCHEMA = "propertyquarry.release-admission-binding.v2"
ADMISSION_BINDING_DIGEST_DOMAIN = (
    b"propertyquarry.release-admission-binding-digest.v2\0"
)
MAX_TRANSPORT_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
MAX_SIGNED_INT64 = (1 << 63) - 1
MAX_JSON_DEPTH = 32
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")


class Operation(str, Enum):
    RELEASE_PREFLIGHT = "release-preflight"
    RELEASE_RUN = "release-run"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class AuthorityModelError(ValueError):
    """Deterministic failure that does not create a signed disposition."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InjectedCrash(RuntimeError):
    pass


def _reject(code: str) -> None:
    raise AuthorityModelError(code)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_object(value: Any) -> str:
    return sha256_digest(canonical_bytes(value))


def _validate_digest(value: str, code: str = "invalid-digest") -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _reject(code)


def _validate_identifier(value: str, code: str) -> None:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        _reject(code)


def _validate_int(
    value: int,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SIGNED_INT64,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _reject(code)


@dataclass(frozen=True)
class RunIdentity:
    audience: str
    repository: str
    ref: str
    candidate_sha: str
    workflow_ref: str
    workflow_sha: str
    run_id: str
    run_attempt: int
    job: str
    environment: str


@dataclass(frozen=True)
class OIDCClaims:
    audience: str
    repository: str
    ref: str
    candidate_sha: str
    workflow_ref: str
    workflow_sha: str
    run_id: str
    run_attempt: int
    job: str
    environment: str

    def identity(self) -> RunIdentity:
        return RunIdentity(**dataclasses.asdict(self))


@dataclass(frozen=True)
class RootPolicy:
    identity: RunIdentity
    required_checks: tuple[str, ...]
    decision_policy_digest: str
    max_request_ttl: int
    max_preflight_validity: int


def _validate_root_policy(policy: RootPolicy) -> None:
    if type(policy) is not RootPolicy:
        _reject("root-policy-invalid")
    if type(policy.identity) is not RunIdentity:
        _reject("root-policy-invalid")
    _identity_from_mapping(_jsonable(policy.identity))
    if type(policy.required_checks) is not tuple or not policy.required_checks:
        _reject("root-policy-check-set-invalid")
    for check in policy.required_checks:
        _validate_identifier(check, "root-policy-check-invalid")
    if len(set(policy.required_checks)) != len(policy.required_checks):
        _reject("root-policy-check-set-invalid")
    _validate_digest(
        policy.decision_policy_digest,
        "root-policy-decision-policy-digest-invalid",
    )
    _validate_int(policy.max_request_ttl, "root-policy-request-ttl-invalid", minimum=1)
    _validate_int(
        policy.max_preflight_validity,
        "root-policy-preflight-validity-invalid",
        minimum=1,
    )


def canonical_root_policy_bytes(policy: RootPolicy) -> bytes:
    """Return the closed v2 root-policy object as canonical UTF-8 JSON."""

    _validate_root_policy(policy)
    return canonical_bytes(
        {
            "schema": ROOT_POLICY_SCHEMA,
            "identity": policy.identity,
            "required_checks": policy.required_checks,
            "decision_policy_digest": policy.decision_policy_digest,
            "max_request_ttl": policy.max_request_ttl,
            "max_preflight_validity": policy.max_preflight_validity,
        }
    )


def root_policy_digest(policy: RootPolicy) -> str:
    """Digest an exact root policy with versioned domain and length framing."""

    policy_bytes = canonical_root_policy_bytes(policy)
    framed = (
        ROOT_POLICY_DIGEST_DOMAIN
        + len(policy_bytes).to_bytes(8, byteorder="big", signed=False)
        + policy_bytes
    )
    return sha256_digest(framed)


@dataclass(frozen=True)
class RequestEnvelope:
    operation: Operation
    request_id: str
    nonce: str
    issued_at: int
    expires_at: int
    identity: RunIdentity


@dataclass(frozen=True)
class ParsedTransport:
    raw_bytes: bytes
    raw_digest: str
    envelope: RequestEnvelope
    canonical_envelope_bytes: bytes
    canonical_envelope_digest: str
    claimed_envelope_digest: str
    signature_payload_bytes: bytes
    request_signature: str


@dataclass(frozen=True)
class LifecycleHead:
    authority: str
    namespace: str
    target: str
    generation: int
    seal_digest: str
    state_digest: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    evidence_digest: str


@dataclass(frozen=True)
class PreflightEvaluation:
    root_policy_digest: str
    decision_policy_digest: str
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True)
class ReadyPreflight:
    request_id: str
    nonce: str
    transport_digest: str
    envelope_digest: str
    identity: RunIdentity
    root_policy_digest: str
    decision_policy_digest: str
    observed_head: LifecycleHead
    checks: tuple[CheckResult, ...]
    evaluated_at: int
    valid_until: int
    response_bytes: bytes
    consumed_by_request_id: str | None = None
    consumed_by_transport_digest: str | None = None


@dataclass(frozen=True)
class AdmissionBinding:
    root_policy_digest: str
    request_id: str
    request_nonce: str
    request_transport_digest: str
    request_envelope_digest: str
    request_identity: RunIdentity
    ready_preflight: ReadyPreflight
    expected_predecessor: LifecycleHead


def canonical_admission_binding_bytes(binding: AdmissionBinding) -> bytes:
    """Return the closed v2 admission/CAS binding as canonical UTF-8 JSON."""

    if type(binding) is not AdmissionBinding:
        _reject("admission-binding-invalid")
    ready = binding.ready_preflight
    if type(ready) is not ReadyPreflight:
        _reject("admission-binding-invalid")
    return canonical_bytes(
        {
            "schema": ADMISSION_BINDING_SCHEMA,
            "operation": Operation.RELEASE_RUN,
            "root_policy_digest": binding.root_policy_digest,
            "request": {
                "request_id": binding.request_id,
                "nonce": binding.request_nonce,
                "transport_digest": binding.request_transport_digest,
                "envelope_digest": binding.request_envelope_digest,
                "identity": binding.request_identity,
            },
            "ready_preflight": {
                "request_id": ready.request_id,
                "nonce": ready.nonce,
                "transport_digest": ready.transport_digest,
                "envelope_digest": ready.envelope_digest,
                "identity": ready.identity,
                "root_policy_digest": ready.root_policy_digest,
                "decision_policy_digest": ready.decision_policy_digest,
                "observed_head": ready.observed_head,
                "checks": ready.checks,
                "evaluated_at": ready.evaluated_at,
                "valid_until": ready.valid_until,
                "response_digest": sha256_digest(ready.response_bytes),
            },
            "expected_predecessor": binding.expected_predecessor,
        }
    )


def admission_binding_digest(binding: AdmissionBinding) -> str:
    """Digest admission prerequisites into a stable idempotency/CAS key."""

    binding_bytes = canonical_admission_binding_bytes(binding)
    framed = (
        ADMISSION_BINDING_DIGEST_DOMAIN
        + len(binding_bytes).to_bytes(8, byteorder="big", signed=False)
        + binding_bytes
    )
    return sha256_digest(framed)


@dataclass(frozen=True)
class AdmissionRequest:
    request_id: str
    nonce: str
    transport_digest: str
    envelope_digest: str
    identity: RunIdentity
    root_policy_digest: str
    admission_binding_digest: str
    ready_preflight: ReadyPreflight
    expected_predecessor: LifecycleHead
    observed_current_head: LifecycleHead | None
    evaluated_at: int


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    request_id: str
    request_transport_digest: str
    preflight_request_id: str
    preflight_transport_digest: str
    predecessor_seal_digest: str
    root_policy_digest: str
    admission_binding_digest: str
    lifecycle_event_digest: str | None
    error_code: str | None


@dataclass(frozen=True)
class ReplayRecord:
    request_id: str
    nonce: str
    operation: Operation
    raw_transport: bytes
    raw_transport_digest: str
    canonical_envelope_digest: str
    identity: RunIdentity
    root_policy_digest: str
    disposition: str
    error_code: str | None
    response_bytes: bytes
    response_digest: str
    recorded_at: int


@dataclass(frozen=True)
class AuthorityState:
    records: tuple[ReplayRecord, ...] = ()
    ready_preflights: tuple[ReadyPreflight, ...] = ()
    authority_time: int = 0


OIDCVerifier = Callable[[str, str], OIDCClaims]
SignatureVerifier = Callable[[bytes, bytes, str], bool]
ResponseSigner = Callable[[bytes], bytes]
TrustedClock = Callable[[], int]
LifecycleHeadReader = Callable[[], LifecycleHead]
PreflightEvaluator = Callable[
    [RunIdentity, LifecycleHead, str, str], PreflightEvaluation
]
AdmissionCallback = Callable[[AdmissionRequest], AdmissionResult]
FaultInjector = Callable[[str], None]


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite number")


def _closed_mapping(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _reject(code)
    return value


def _validate_json_tree(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _reject("transport-json-depth-exceeded")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            _reject("transport-unicode-surrogate-forbidden")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_tree(key, depth=depth + 1)
            _validate_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth=depth + 1)


def _identity_from_mapping(value: Any) -> RunIdentity:
    payload = _closed_mapping(
        value,
        {
            "audience",
            "repository",
            "ref",
            "candidate_sha",
            "workflow_ref",
            "workflow_sha",
            "run_id",
            "run_attempt",
            "job",
            "environment",
        },
        "identity-fields-invalid",
    )
    for key in (
        "audience",
        "repository",
        "ref",
        "workflow_ref",
        "run_id",
        "job",
        "environment",
    ):
        _validate_identifier(payload[key], f"identity-{key}-invalid")
    for key in ("candidate_sha", "workflow_sha"):
        if not isinstance(payload[key], str) or SHA_RE.fullmatch(payload[key]) is None:
            _reject(f"identity-{key}-invalid")
    _validate_int(payload["run_attempt"], "identity-run_attempt-invalid", minimum=1)
    return RunIdentity(**payload)


def parse_transport(raw_transport: bytes) -> ParsedTransport:
    if not isinstance(raw_transport, bytes):
        _reject("transport-not-bytes")
    if not raw_transport or len(raw_transport) > MAX_TRANSPORT_BYTES:
        _reject("transport-size-invalid")
    if raw_transport.startswith(b"\xef\xbb\xbf"):
        _reject("transport-bom-forbidden")
    try:
        text = raw_transport.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("transport-not-utf8")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        _reject("transport-duplicate-key")
    except RecursionError:
        _reject("transport-json-depth-exceeded")
    except (json.JSONDecodeError, ValueError):
        _reject("transport-json-invalid")
    _validate_json_tree(payload)
    outer = _closed_mapping(
        payload,
        {
            "schema",
            "envelope",
            "envelope_digest",
            "request_signature",
        },
        "transport-fields-invalid",
    )
    if outer["schema"] != REQUEST_SCHEMA:
        _reject("transport-schema-invalid")
    if not isinstance(outer["request_signature"], str) or not outer["request_signature"]:
        _reject("request-signature-invalid")
    _validate_digest(outer["envelope_digest"], "claimed-envelope-digest-invalid")

    envelope_payload = _closed_mapping(
        outer["envelope"],
        {"operation", "request_id", "nonce", "issued_at", "expires_at", "identity"},
        "envelope-fields-invalid",
    )
    try:
        operation = Operation(envelope_payload["operation"])
    except (TypeError, ValueError):
        _reject("operation-invalid")
    _validate_identifier(envelope_payload["request_id"], "request-id-invalid")
    _validate_identifier(envelope_payload["nonce"], "nonce-invalid")
    _validate_int(envelope_payload["issued_at"], "request-issued-at-invalid")
    _validate_int(envelope_payload["expires_at"], "request-expires-at-invalid")
    envelope = RequestEnvelope(
        operation=operation,
        request_id=envelope_payload["request_id"],
        nonce=envelope_payload["nonce"],
        issued_at=envelope_payload["issued_at"],
        expires_at=envelope_payload["expires_at"],
        identity=_identity_from_mapping(envelope_payload["identity"]),
    )
    envelope_bytes = canonical_bytes(envelope)
    signature_payload_bytes = canonical_bytes(
        {
            "schema": outer["schema"],
            "envelope": envelope,
            "envelope_digest": outer["envelope_digest"],
        }
    )
    return ParsedTransport(
        raw_bytes=raw_transport,
        raw_digest=sha256_digest(raw_transport),
        envelope=envelope,
        canonical_envelope_bytes=envelope_bytes,
        canonical_envelope_digest=sha256_digest(envelope_bytes),
        claimed_envelope_digest=outer["envelope_digest"],
        signature_payload_bytes=signature_payload_bytes,
        request_signature=outer["request_signature"],
    )


class ReleaseRequestAuthorityModel:
    """Durable request/replay and ready-preflight reference state machine."""

    def __init__(
        self,
        *,
        policy: RootPolicy,
        oidc_verifier: OIDCVerifier,
        signature_verifier: SignatureVerifier,
        response_signer: ResponseSigner,
        trusted_clock: TrustedClock,
        lifecycle_head_reader: LifecycleHeadReader,
        preflight_evaluator: PreflightEvaluator,
        admission_callback: AdmissionCallback,
        state: AuthorityState | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._validate_policy(policy)
        self._policy = policy
        self._policy_digest = root_policy_digest(policy)
        self._oidc_verifier = oidc_verifier
        self._signature_verifier = signature_verifier
        self._response_signer = response_signer
        self._clock = trusted_clock
        self._head_reader = lifecycle_head_reader
        self._preflight_evaluator = preflight_evaluator
        self._admission_callback = admission_callback
        self._fault_injector = fault_injector
        if state is not None and type(state) is not AuthorityState:
            _reject("state-invalid")
        self._state = AuthorityState() if state is None else state
        self._request_index: dict[str, ReplayRecord] = {}
        self._nonce_index: dict[str, ReplayRecord] = {}
        self._validate_and_index_state()

    @property
    def state(self) -> AuthorityState:
        return self._state

    @property
    def policy(self) -> RootPolicy:
        """The one immutable exact per-run trust-root snapshot."""

        return self._policy

    @property
    def policy_digest(self) -> str:
        """The exact versioned digest of the immutable root-policy snapshot."""

        return self._policy_digest

    @property
    def records(self) -> tuple[ReplayRecord, ...]:
        return self._state.records

    @property
    def ready_preflights(self) -> tuple[ReadyPreflight, ...]:
        return self._state.ready_preflights

    def handle(self, raw_transport: bytes, oidc_token: str) -> bytes:
        parsed = parse_transport(raw_transport)
        claims = self._authenticate(parsed, oidc_token)
        identity = self._bind_current_authentication(parsed, claims)
        replay = self._lookup_replay(parsed)
        if replay is not None:
            if replay.identity != identity:
                _reject("replay-identity-mismatch")
            if replay.root_policy_digest != self.policy_digest:
                _reject("replay-root-policy-digest-mismatch")
            return replay.response_bytes

        now = self._trusted_now()
        error = self._validate_authenticated_request(parsed, now)
        if error is not None:
            return self._commit_disposition(
                parsed, parsed.envelope.identity, now, "rejected", error
            )
        if parsed.envelope.operation is Operation.RELEASE_PREFLIGHT:
            return self._handle_preflight(parsed, claims.identity(), now)
        if parsed.envelope.operation is Operation.RELEASE_RUN:
            return self._handle_release_run(parsed, claims.identity(), now)
        _reject("operation-invalid")

    def _lookup_replay(self, parsed: ParsedTransport) -> ReplayRecord | None:
        by_id = self._request_index.get(parsed.envelope.request_id)
        by_nonce = self._nonce_index.get(parsed.envelope.nonce)
        if by_id is None and by_nonce is None:
            return None
        if by_id is not None and by_nonce is by_id:
            if (
                by_id.raw_transport_digest == parsed.raw_digest
                and by_id.raw_transport == parsed.raw_bytes
            ):
                return by_id
            _reject("request-id-reuse")
        if by_id is not None:
            _reject("request-id-reuse")
        _reject("nonce-reuse")

    def _authenticate(
        self, parsed: ParsedTransport, oidc_token: str
    ) -> OIDCClaims:
        try:
            signature_ok = self._signature_verifier(
                parsed.signature_payload_bytes,
                parsed.canonical_envelope_bytes,
                parsed.request_signature,
            )
        except Exception:
            _reject("signature-verification-failed")
        if signature_ok is not True:
            _reject("signature-verification-failed")
        if not isinstance(oidc_token, str) or not oidc_token:
            _reject("oidc-token-invalid")
        try:
            claims = self._oidc_verifier(
                oidc_token, self.policy.identity.audience
            )
        except Exception:
            _reject("oidc-verification-failed")
        if type(claims) is not OIDCClaims:
            _reject("oidc-verifier-result-invalid")
        return claims

    def _bind_current_authentication(
        self, parsed: ParsedTransport, claims: OIDCClaims
    ) -> RunIdentity:
        """Bind current authentication before consulting durable replay keys."""

        identity = claims.identity()
        if identity != parsed.envelope.identity:
            _reject("oidc-envelope-identity-mismatch")
        policy_identity = self.policy.identity
        claim_codes = (
            ("audience", "claim-audience-mismatch"),
            ("repository", "claim-repository-mismatch"),
            ("ref", "claim-ref-mismatch"),
            ("candidate_sha", "claim-candidate-sha-mismatch"),
            ("workflow_ref", "claim-workflow-ref-mismatch"),
            ("workflow_sha", "claim-workflow-sha-mismatch"),
            ("run_id", "claim-run-id-mismatch"),
            ("run_attempt", "claim-run-attempt-mismatch"),
            ("job", "claim-job-mismatch"),
            ("environment", "claim-environment-mismatch"),
        )
        for field, code in claim_codes:
            if getattr(identity, field) != getattr(policy_identity, field):
                _reject(code)
        return identity

    def _trusted_now(self) -> int:
        try:
            now = self._clock()
        except Exception:
            _reject("trusted-clock-failed")
        _validate_int(now, "trusted-clock-invalid")
        if now < self._state.authority_time:
            _reject("trusted-clock-regressed")
        return now

    def _validate_authenticated_request(
        self, parsed: ParsedTransport, now: int
    ) -> str | None:
        envelope = parsed.envelope
        if parsed.claimed_envelope_digest != parsed.canonical_envelope_digest:
            return "envelope-digest-mismatch"
        if envelope.expires_at <= envelope.issued_at:
            return "request-expiry-order-invalid"
        if envelope.expires_at - envelope.issued_at > self.policy.max_request_ttl:
            return "request-ttl-exceeded"
        if envelope.issued_at > now:
            return "request-issued-in-future"
        if now >= envelope.expires_at:
            return "request-expired"
        return None

    def _handle_preflight(
        self, parsed: ParsedTransport, identity: RunIdentity, now: int
    ) -> bytes:
        existing = [
            item
            for item in self._state.ready_preflights
            if item.identity == identity and item.consumed_by_request_id is None
        ]
        if existing:
            return self._commit_disposition(
                parsed,
                identity,
                now,
                "rejected",
                "ready-preflight-already-exists",
            )
        try:
            head_before = self._read_head()
            evaluation = self._preflight_evaluator(
                identity,
                head_before,
                self.policy_digest,
                self.policy.decision_policy_digest,
            )
            head_after = self._read_head()
        except Exception:
            return self._commit_disposition(
                parsed, identity, now, "indeterminate", "preflight-evaluator-failed"
            )
        if head_before != head_after:
            return self._commit_disposition(
                parsed, identity, now, "indeterminate", "preflight-head-changed"
            )
        try:
            checks = self._validate_checks(evaluation)
        except AuthorityModelError as exc:
            return self._commit_disposition(
                parsed, identity, now, "rejected", exc.code
            )

        if all(check.status is CheckStatus.PASS for check in checks):
            disposition = "ready"
            error_code = None
        elif any(check.status is CheckStatus.FAIL for check in checks):
            disposition = "not-ready"
            error_code = "preflight-check-failed"
        else:
            disposition = "indeterminate"
            error_code = "preflight-check-indeterminate"
        valid_until = min(
            parsed.envelope.expires_at,
            now + self.policy.max_preflight_validity,
        )
        response_payload = self._response_payload(
            parsed,
            disposition,
            error_code,
            now,
            {
                "observed_head": head_before,
                "checks": checks,
                "decision_policy_digest": self.policy.decision_policy_digest,
                "valid_until": valid_until,
            },
        )
        response = self._sign_response(response_payload)
        ready = None
        if disposition == "ready":
            ready = ReadyPreflight(
                request_id=parsed.envelope.request_id,
                nonce=parsed.envelope.nonce,
                transport_digest=parsed.raw_digest,
                envelope_digest=parsed.canonical_envelope_digest,
                identity=identity,
                root_policy_digest=self.policy_digest,
                decision_policy_digest=self.policy.decision_policy_digest,
                observed_head=head_before,
                checks=checks,
                evaluated_at=now,
                valid_until=valid_until,
                response_bytes=response,
            )
        return self._commit_record(
            parsed,
            identity,
            now,
            disposition,
            error_code,
            response,
            add_ready=ready,
        )

    def _handle_release_run(
        self, parsed: ParsedTransport, identity: RunIdentity, now: int
    ) -> bytes:
        candidates = [
            item for item in self._state.ready_preflights if item.identity == identity
        ]
        unconsumed = [item for item in candidates if item.consumed_by_request_id is None]
        if not unconsumed:
            code = "preflight-already-consumed" if candidates else "ready-preflight-required"
            return self._commit_disposition(parsed, identity, now, "rejected", code)
        if len(unconsumed) != 1:
            return self._commit_disposition(
                parsed, identity, now, "rejected", "ready-preflight-ambiguous"
            )
        ready = unconsumed[0]
        if ready.root_policy_digest != self.policy_digest:
            return self._commit_run_rejection(
                parsed,
                identity,
                now,
                "rejected",
                "ready-preflight-root-policy-digest-mismatch",
                ready,
            )
        if ready.decision_policy_digest != self.policy.decision_policy_digest:
            return self._commit_run_rejection(
                parsed,
                identity,
                now,
                "rejected",
                "ready-preflight-decision-policy-digest-mismatch",
                ready,
            )
        if ready.request_id == parsed.envelope.request_id or ready.nonce == parsed.envelope.nonce:
            return self._commit_run_rejection(
                parsed,
                identity,
                now,
                "rejected",
                "release-run-not-distinct",
                ready,
            )
        if now >= ready.valid_until:
            return self._commit_run_rejection(
                parsed, identity, now, "rejected", "ready-preflight-expired", ready
            )
        if tuple(check.name for check in ready.checks) != self.policy.required_checks:
            return self._commit_run_rejection(
                parsed,
                identity,
                now,
                "rejected",
                "ready-preflight-check-set-mismatch",
                ready,
            )
        if any(check.status is not CheckStatus.PASS for check in ready.checks):
            return self._commit_run_rejection(
                parsed, identity, now, "rejected", "ready-preflight-not-ready", ready
            )
        try:
            observed_current_head: LifecycleHead | None = self._read_head()
        except AuthorityModelError:
            observed_current_head = None

        admission_binding = AdmissionBinding(
            root_policy_digest=self.policy_digest,
            request_id=parsed.envelope.request_id,
            request_nonce=parsed.envelope.nonce,
            request_transport_digest=parsed.raw_digest,
            request_envelope_digest=parsed.canonical_envelope_digest,
            request_identity=identity,
            ready_preflight=ready,
            expected_predecessor=ready.observed_head,
        )
        admission_request = AdmissionRequest(
            request_id=parsed.envelope.request_id,
            nonce=parsed.envelope.nonce,
            transport_digest=parsed.raw_digest,
            envelope_digest=parsed.canonical_envelope_digest,
            identity=identity,
            root_policy_digest=self.policy_digest,
            admission_binding_digest=admission_binding_digest(admission_binding),
            ready_preflight=ready,
            expected_predecessor=ready.observed_head,
            observed_current_head=observed_current_head,
            evaluated_at=now,
        )
        self._fault("before-admission-callback")
        try:
            admission = self._admission_callback(admission_request)
            self._validate_admission_result(admission, admission_request)
            callback_error = None
        except AuthorityModelError as exc:
            admission = None
            callback_error = exc.code
        except Exception:
            admission = None
            callback_error = "admission-callback-failed"
        self._fault("after-admission-callback-before-commit")

        consumed = replace(
            ready,
            consumed_by_request_id=parsed.envelope.request_id,
            consumed_by_transport_digest=parsed.raw_digest,
        )
        if admission is not None and admission.admitted:
            disposition = "admitted"
            error_code = None
        else:
            disposition = "rejected"
            error_code = (
                callback_error
                or (admission.error_code if admission is not None else None)
                or "admission-rejected"
            )
        response_payload = self._response_payload(
            parsed,
            disposition,
            error_code,
            now,
            {
                "ready_preflight_request_id": ready.request_id,
                "ready_preflight_transport_digest": ready.transport_digest,
                "expected_predecessor": ready.observed_head,
                "observed_current_head": observed_current_head,
                "admission": admission,
            },
        )
        response = self._sign_response(response_payload)
        return self._commit_record(
            parsed,
            identity,
            now,
            disposition,
            error_code,
            response,
            replace_ready=consumed,
        )

    def _validate_checks(
        self, evaluation: PreflightEvaluation
    ) -> tuple[CheckResult, ...]:
        if type(evaluation) is not PreflightEvaluation:
            _reject("preflight-evaluation-invalid")
        _validate_digest(
            evaluation.root_policy_digest,
            "preflight-root-policy-digest-invalid",
        )
        if evaluation.root_policy_digest != self.policy_digest:
            _reject("preflight-root-policy-digest-mismatch")
        _validate_digest(
            evaluation.decision_policy_digest,
            "preflight-decision-policy-digest-invalid",
        )
        if evaluation.decision_policy_digest != self.policy.decision_policy_digest:
            _reject("preflight-decision-policy-digest-mismatch")
        checks = evaluation.checks
        if type(checks) is not tuple:
            _reject("preflight-check-invalid")
        for check in checks:
            if type(check) is not CheckResult or type(check.status) is not CheckStatus:
                _reject("preflight-check-invalid")
            _validate_identifier(check.name, "preflight-check-invalid")
            _validate_digest(check.evidence_digest, "preflight-check-evidence-invalid")
        names = tuple(check.name for check in checks)
        if len(set(names)) != len(names):
            _reject("preflight-check-set-duplicate")
        if names != self.policy.required_checks:
            _reject("preflight-check-set-mismatch")
        return checks

    def _read_head(self) -> LifecycleHead:
        try:
            head = self._head_reader()
        except Exception:
            _reject("lifecycle-head-unavailable")
        self._validate_head(head)
        return head

    def _validate_admission_result(
        self, result: AdmissionResult, request: AdmissionRequest
    ) -> None:
        expected_request_binding = admission_binding_digest(
            AdmissionBinding(
                root_policy_digest=request.root_policy_digest,
                request_id=request.request_id,
                request_nonce=request.nonce,
                request_transport_digest=request.transport_digest,
                request_envelope_digest=request.envelope_digest,
                request_identity=request.identity,
                ready_preflight=request.ready_preflight,
                expected_predecessor=request.expected_predecessor,
            )
        )
        if (
            request.root_policy_digest != self.policy_digest
            or request.ready_preflight.root_policy_digest != self.policy_digest
            or request.expected_predecessor
            != request.ready_preflight.observed_head
            or request.admission_binding_digest != expected_request_binding
        ):
            _reject("admission-request-binding-mismatch")
        if type(result) is not AdmissionResult:
            _reject("admission-result-invalid")
        if type(result.admitted) is not bool:
            _reject("admission-result-invalid")
        _validate_digest(
            result.root_policy_digest, "admission-root-policy-digest-invalid"
        )
        _validate_digest(
            result.admission_binding_digest,
            "admission-binding-digest-invalid",
        )
        expected = (
            (result.request_id, request.request_id),
            (result.request_transport_digest, request.transport_digest),
            (result.preflight_request_id, request.ready_preflight.request_id),
            (
                result.preflight_transport_digest,
                request.ready_preflight.transport_digest,
            ),
            (
                result.predecessor_seal_digest,
                request.expected_predecessor.seal_digest,
            ),
            (result.root_policy_digest, request.root_policy_digest),
            (
                result.admission_binding_digest,
                request.admission_binding_digest,
            ),
        )
        if any(actual != wanted for actual, wanted in expected):
            _reject("admission-result-binding-mismatch")
        if result.admitted:
            if result.error_code is not None or result.lifecycle_event_digest is None:
                _reject("admission-result-invalid")
            _validate_digest(result.lifecycle_event_digest, "admission-event-digest-invalid")
        else:
            if not result.error_code or result.lifecycle_event_digest is not None:
                _reject("admission-result-invalid")
            _validate_identifier(result.error_code, "admission-error-code-invalid")

    def _commit_run_rejection(
        self,
        parsed: ParsedTransport,
        identity: RunIdentity,
        now: int,
        disposition: str,
        error_code: str,
        ready: ReadyPreflight,
    ) -> bytes:
        consumed = replace(
            ready,
            consumed_by_request_id=parsed.envelope.request_id,
            consumed_by_transport_digest=parsed.raw_digest,
        )
        payload = self._response_payload(
            parsed,
            disposition,
            error_code,
            now,
            {
                "ready_preflight_request_id": ready.request_id,
                "ready_preflight_transport_digest": ready.transport_digest,
            },
        )
        response = self._sign_response(payload)
        return self._commit_record(
            parsed,
            identity,
            now,
            disposition,
            error_code,
            response,
            replace_ready=consumed,
        )

    def _commit_disposition(
        self,
        parsed: ParsedTransport,
        identity: RunIdentity,
        now: int,
        disposition: str,
        error_code: str,
    ) -> bytes:
        payload = self._response_payload(
            parsed, disposition, error_code, now, {}
        )
        response = self._sign_response(payload)
        return self._commit_record(
            parsed, identity, now, disposition, error_code, response
        )

    def _response_payload(
        self,
        parsed: ParsedTransport,
        disposition: str,
        error_code: str | None,
        now: int,
        details: Mapping[str, Any],
    ) -> bytes:
        return canonical_bytes(
            {
                "schema": RESPONSE_SCHEMA,
                "authoritative": False,
                "operation": parsed.envelope.operation,
                "request_id": parsed.envelope.request_id,
                "nonce": parsed.envelope.nonce,
                "request_transport_digest": parsed.raw_digest,
                "canonical_envelope_digest": parsed.canonical_envelope_digest,
                "identity_digest": digest_object(parsed.envelope.identity),
                "root_policy_digest": self.policy_digest,
                "evaluated_at": now,
                "disposition": disposition,
                "error_code": error_code,
                "details": details,
            }
        )

    def _sign_response(self, payload: bytes) -> bytes:
        try:
            response = self._response_signer(payload)
        except Exception:
            _reject("response-signing-failed")
        if (
            not isinstance(response, bytes)
            or not response
            or len(response) > MAX_RESPONSE_BYTES
        ):
            _reject("signed-response-invalid")
        return response

    def _commit_record(
        self,
        parsed: ParsedTransport,
        identity: RunIdentity,
        now: int,
        disposition: str,
        error_code: str | None,
        response: bytes,
        *,
        add_ready: ReadyPreflight | None = None,
        replace_ready: ReadyPreflight | None = None,
    ) -> bytes:
        record = ReplayRecord(
            request_id=parsed.envelope.request_id,
            nonce=parsed.envelope.nonce,
            operation=parsed.envelope.operation,
            raw_transport=parsed.raw_bytes,
            raw_transport_digest=parsed.raw_digest,
            canonical_envelope_digest=parsed.canonical_envelope_digest,
            identity=identity,
            root_policy_digest=self.policy_digest,
            disposition=disposition,
            error_code=error_code,
            response_bytes=response,
            response_digest=sha256_digest(response),
            recorded_at=now,
        )
        ready_items = list(self._state.ready_preflights)
        if add_ready is not None:
            ready_items.append(add_ready)
        if replace_ready is not None:
            matches = [
                index
                for index, item in enumerate(ready_items)
                if item.request_id == replace_ready.request_id
            ]
            if len(matches) != 1:
                _reject("ready-preflight-transaction-conflict")
            ready_items[matches[0]] = replace_ready
        next_state = AuthorityState(
            records=self._state.records + (record,),
            ready_preflights=tuple(ready_items),
            authority_time=max(self._state.authority_time, now),
        )
        self._fault("before-atomic-commit")
        self._state = next_state
        self._request_index[record.request_id] = record
        self._nonce_index[record.nonce] = record
        self._fault("after-atomic-commit")
        return response

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _validate_and_index_state(self) -> None:
        if (
            type(self._state) is not AuthorityState
            or type(self._state.records) is not tuple
            or type(self._state.ready_preflights) is not tuple
        ):
            _reject("state-invalid")
        request_ids: set[str] = set()
        nonces: set[str] = set()
        last_time = 0
        _validate_int(
            self._state.authority_time, "state-authority-time-invalid"
        )
        for record in self._state.records:
            if type(record) is not ReplayRecord:
                _reject("state-record-invalid")
            _validate_digest(
                record.root_policy_digest,
                "state-record-root-policy-digest-invalid",
            )
            if record.root_policy_digest != self.policy_digest:
                _reject("state-root-policy-digest-mismatch")
            _validate_identifier(record.request_id, "state-record-invalid")
            _validate_identifier(record.nonce, "state-record-invalid")
            if type(record.operation) is not Operation:
                _reject("state-record-invalid")
            if not isinstance(record.raw_transport, bytes):
                _reject("state-record-invalid")
            if type(record.identity) is not RunIdentity:
                _reject("state-record-invalid")
            _identity_from_mapping(_jsonable(record.identity))
            allowed_dispositions = {
                Operation.RELEASE_PREFLIGHT: {
                    "ready",
                    "not-ready",
                    "indeterminate",
                    "rejected",
                },
                Operation.RELEASE_RUN: {"admitted", "rejected"},
            }
            if (
                not isinstance(record.disposition, str)
                or record.disposition not in allowed_dispositions[record.operation]
            ):
                _reject("state-record-disposition-invalid")
            if (record.disposition in {"ready", "admitted"}) != (
                record.error_code is None
            ):
                _reject("state-record-disposition-invalid")
            if record.error_code is not None:
                _validate_identifier(
                    record.error_code, "state-record-disposition-invalid"
                )
            if record.request_id in request_ids:
                _reject("state-request-id-duplicate")
            if record.nonce in nonces:
                _reject("state-nonce-duplicate")
            if record.raw_transport_digest != sha256_digest(record.raw_transport):
                _reject("state-transport-digest-invalid")
            parsed = parse_transport(record.raw_transport)
            if (
                parsed.envelope.request_id != record.request_id
                or parsed.envelope.nonce != record.nonce
                or parsed.envelope.operation is not record.operation
                or parsed.envelope.identity != record.identity
                or parsed.canonical_envelope_digest
                != record.canonical_envelope_digest
            ):
                _reject("state-record-binding-invalid")
            if (
                not isinstance(record.response_bytes, bytes)
                or not record.response_bytes
                or len(record.response_bytes) > MAX_RESPONSE_BYTES
            ):
                _reject("state-response-invalid")
            if record.response_digest != sha256_digest(record.response_bytes):
                _reject("state-response-digest-invalid")
            _validate_int(record.recorded_at, "state-record-time-invalid")
            if record.recorded_at < last_time:
                _reject("state-time-regressed")
            request_ids.add(record.request_id)
            nonces.add(record.nonce)
            self._request_index[record.request_id] = record
            self._nonce_index[record.nonce] = record
            last_time = record.recorded_at
        if self._state.authority_time < last_time:
            _reject("state-authority-time-invalid")
        ready_ids: set[str] = set()
        unconsumed_identities: set[RunIdentity] = set()
        for ready in self._state.ready_preflights:
            if type(ready) is not ReadyPreflight:
                _reject("state-ready-preflight-invalid")
            _validate_digest(
                ready.root_policy_digest,
                "state-ready-preflight-root-policy-digest-invalid",
            )
            if ready.root_policy_digest != self.policy_digest:
                _reject("state-root-policy-digest-mismatch")
            _validate_digest(
                ready.decision_policy_digest,
                "state-ready-preflight-decision-policy-digest-invalid",
            )
            if ready.decision_policy_digest != self.policy.decision_policy_digest:
                _reject("state-decision-policy-digest-mismatch")
            _validate_identifier(ready.request_id, "state-ready-preflight-invalid")
            _validate_identifier(ready.nonce, "state-ready-preflight-invalid")
            _validate_digest(
                ready.transport_digest, "state-ready-preflight-invalid"
            )
            _validate_digest(ready.envelope_digest, "state-ready-preflight-invalid")
            if type(ready.identity) is not RunIdentity:
                _reject("state-ready-preflight-invalid")
            _identity_from_mapping(_jsonable(ready.identity))
            if ready.request_id in ready_ids:
                _reject("state-ready-preflight-invalid")
            source = self._request_index.get(ready.request_id)
            if (
                source is None
                or source.operation is not Operation.RELEASE_PREFLIGHT
                or source.disposition != "ready"
                or source.raw_transport_digest != ready.transport_digest
                or source.response_bytes != ready.response_bytes
                or source.root_policy_digest != ready.root_policy_digest
            ):
                _reject("state-ready-preflight-source-invalid")
            if (
                ready.nonce != source.nonce
                or ready.envelope_digest != source.canonical_envelope_digest
                or ready.identity != source.identity
            ):
                _reject("state-ready-preflight-binding-invalid")
            if ready.identity != self.policy.identity:
                _reject("state-ready-preflight-binding-invalid")
            self._validate_head(ready.observed_head)
            self._validate_checks(
                PreflightEvaluation(
                    root_policy_digest=ready.root_policy_digest,
                    decision_policy_digest=ready.decision_policy_digest,
                    checks=ready.checks,
                )
            )
            if any(check.status is not CheckStatus.PASS for check in ready.checks):
                _reject("state-ready-preflight-not-ready")
            _validate_int(ready.evaluated_at, "state-ready-preflight-time-invalid")
            _validate_int(ready.valid_until, "state-ready-preflight-time-invalid")
            source_envelope = parse_transport(source.raw_transport).envelope
            expected_valid_until = min(
                source_envelope.expires_at,
                ready.evaluated_at + self.policy.max_preflight_validity,
            )
            if (
                ready.evaluated_at != source.recorded_at
                or ready.valid_until != expected_valid_until
                or ready.valid_until <= ready.evaluated_at
            ):
                _reject("state-ready-preflight-time-invalid")
            if (ready.consumed_by_request_id is None) != (
                ready.consumed_by_transport_digest is None
            ):
                _reject("state-ready-preflight-consumption-invalid")
            if ready.consumed_by_request_id is not None:
                _validate_identifier(
                    ready.consumed_by_request_id,
                    "state-ready-preflight-consumption-invalid",
                )
                _validate_digest(
                    ready.consumed_by_transport_digest,
                    "state-ready-preflight-consumption-invalid",
                )
                consumer = self._request_index.get(ready.consumed_by_request_id)
                if (
                    consumer is None
                    or consumer.operation is not Operation.RELEASE_RUN
                    or consumer.raw_transport_digest
                    != ready.consumed_by_transport_digest
                    or consumer.identity != ready.identity
                    or consumer.recorded_at < ready.evaluated_at
                ):
                    _reject("state-ready-preflight-consumer-invalid")
            elif ready.identity in unconsumed_identities:
                _reject("state-ready-preflight-ambiguous")
            else:
                unconsumed_identities.add(ready.identity)
            ready_ids.add(ready.request_id)

    @staticmethod
    def _validate_policy(policy: RootPolicy) -> None:
        _validate_root_policy(policy)

    @staticmethod
    def _validate_head(head: LifecycleHead) -> None:
        if type(head) is not LifecycleHead:
            _reject("lifecycle-head-invalid")
        for field in (head.authority, head.namespace, head.target):
            _validate_identifier(field, "lifecycle-head-invalid")
        _validate_int(head.generation, "lifecycle-head-invalid")
        _validate_digest(head.seal_digest, "lifecycle-head-invalid")
        _validate_digest(head.state_digest, "lifecycle-head-invalid")


def describe_contract() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "authoritative": False,
        "cryptographic_authority": False,
        "operations": [operation.value for operation in Operation],
        "replay_identity": ["exact-raw-transport-bytes", "request-id", "nonce"],
        "replay_authentication": (
            "current-signature-and-oidc-with-exact-envelope-policy-record-"
            "identity-binding"
        ),
        "replay_reevaluation": (
            "bypass-clock-lifecycle-head-and-policy-decisions-after-auth"
        ),
        "oidc_bearer": "out-of-band-never-serialized-or-persisted",
        "request_signature_binding": (
            "canonical-token-independent-request-and-envelope-bytes"
        ),
        "preflight_mutates_lifecycle": False,
        "preflight_evaluator_binding": (
            "trusted-root-and-decision-policy-digests-passed-and-exactly-echoed"
        ),
        "client_selected_preflight_receipt_allowed": False,
        "root_policy": "one-immutable-exact-per-run-snapshot",
        "root_policy_digest": {
            "schema": ROOT_POLICY_SCHEMA,
            "algorithm": "sha256",
            "domain_separator": ROOT_POLICY_DIGEST_DOMAIN.decode("ascii"),
            "length_framing": "unsigned-64-bit-big-endian-canonical-json-length",
            "canonical_json": (
                "closed-sorted-keys-no-whitespace-ascii-escaped-utf8"
            ),
            "decision_policy_digest": (
                "authenticated-check-definition-decision-trust-policy-artifact"
            ),
            "bindings": [
                "replay-record",
                "ready-preflight",
                "admission-request",
                "admission-result",
                "signed-response-payload",
                "persisted-state",
            ],
        },
        "persisted_policy_continuity": (
            "missing-or-different-root-policy-digest-rejected-before-replay"
        ),
        "admission": "injected-lookup-first-callback-after-all-checks",
        "admission_retry_key": "admission-binding-digest",
        "admission_binding_digest": {
            "schema": ADMISSION_BINDING_SCHEMA,
            "algorithm": "sha256",
            "domain_separator": ADMISSION_BINDING_DIGEST_DOMAIN.decode("ascii"),
            "length_framing": "unsigned-64-bit-big-endian-canonical-json-length",
            "immutable_predecessor": "ready-preflight-observed-head",
            "excludes": ["release-evaluated-at", "observed-current-head"],
            "stability": (
                "same-prerequisites-same-post-crash-cas-key-after-head-advance"
            ),
        },
        "admission_callback_contract": (
            "lookup-binding-first-return-stored-result-otherwise-require-"
            "observed-head-equals-expected-predecessor-before-atomic-cas"
        ),
        "durability_contract": "atomic-state-replacement",
        "authority_state_storage_requirement": (
            "external-authenticated-and-encrypted-durable-storage"
        ),
        "signed_response_requirement": "external-signature-verification-required",
        "limitations": [
            (
                "persisted-authority-state-requires-external-authenticated-"
                "encrypted-storage"
            ),
            (
                "injected-evaluator-echo-does-not-authenticate-the-"
                "decision-policy-artifact"
            ),
            "signed-responses-require-external-trust-root-verification",
        ],
    }


def main() -> int:
    print(json.dumps(describe_contract(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
