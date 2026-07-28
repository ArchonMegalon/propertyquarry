from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable

import pytest

from scripts.propertyquarry_release_request_authority_model import (
    ADMISSION_BINDING_DIGEST_DOMAIN,
    ADMISSION_BINDING_SCHEMA,
    MAX_SIGNED_INT64,
    MAX_JSON_DEPTH,
    MAX_TRANSPORT_BYTES,
    REQUEST_SCHEMA,
    ROOT_POLICY_DIGEST_DOMAIN,
    ROOT_POLICY_SCHEMA,
    AdmissionRequest,
    AdmissionResult,
    AdmissionBinding,
    AuthorityModelError,
    AuthorityState,
    CheckResult,
    CheckStatus,
    InjectedCrash,
    LifecycleHead,
    OIDCClaims,
    Operation,
    PreflightEvaluation,
    ReleaseRequestAuthorityModel,
    ReplayRecord,
    RootPolicy,
    RunIdentity,
    canonical_bytes,
    canonical_admission_binding_bytes,
    canonical_root_policy_bytes,
    describe_contract,
    digest_object,
    admission_binding_digest,
    root_policy_digest,
    sha256_digest,
)


SIGNED_PREFIX = b"reference-model-signature\0"
TEST_OIDC_BEARER = "secret-oidc-bearer-never-persist"
REQUIRED_CHECKS = (
    "immutable-artifact",
    "flagship-security",
    "watchdog-ready",
)


def _digest(label: str) -> str:
    return sha256_digest(label.encode("utf-8"))


def _signature(signature_payload: bytes) -> str:
    value = hashlib.sha256(
        b"token-independent-request-signature-v1\0" + signature_payload
    ).hexdigest()
    return f"test-signature-{value}"


def _claims(identity: RunIdentity) -> OIDCClaims:
    return OIDCClaims(**dataclasses.asdict(identity))


class Harness:
    def __init__(self) -> None:
        self.now = 1_000
        self.identity = RunIdentity(
            audience="propertyquarry-release-v2",
            repository="owner/property",
            ref="refs/heads/main",
            candidate_sha="a" * 40,
            workflow_ref=(
                "owner/property/.github/workflows/"
                "propertyquarry-release-v2.yml@refs/heads/main"
            ),
            workflow_sha="b" * 40,
            run_id="424242",
            run_attempt=1,
            job="propertyquarry-release-v2",
            environment="propertyquarry-production",
        )
        self.oidc_claims = _claims(self.identity)
        self.head = LifecycleHead(
            authority="propertyquarry-lifecycle-v1",
            namespace="propertyquarry-production",
            target="flagship-3d-tour",
            generation=7,
            seal_digest=_digest("seal-7"),
            state_digest=_digest("state-7"),
        )
        self.checks = tuple(
            CheckResult(name, CheckStatus.PASS, _digest(f"evidence:{name}"))
            for name in REQUIRED_CHECKS
        )
        self.oidc_calls = 0
        self.signature_calls = 0
        self.head_reads = 0
        self.preflight_calls = 0
        self.admission_calls = 0
        self.admission_requests: list[AdmissionRequest] = []
        self.admission_effects: dict[str, str] = {}
        self.admission_results: dict[str, AdmissionResult] = {}
        self.oidc_error = False
        self.signature_error = False
        self.head_error = False
        self.evaluator_error = False
        self.evaluator_root_policy_digest_override: str | None = None
        self.evaluator_decision_policy_digest_override: str | None = None
        self.admission_error = False
        self.advance_head_on_admission = False
        self.mutate_head_during_evaluation = False
        self.admission_transform: Callable[[AdmissionResult], AdmissionResult] = (
            lambda result: result
        )
        self.fault_stage: str | None = None
        self.fault_once = True
        self.faults_seen: list[str] = []

    @property
    def policy(self) -> RootPolicy:
        return RootPolicy(
            identity=self.identity,
            required_checks=REQUIRED_CHECKS,
            decision_policy_digest=_digest("decision-policy-v1"),
            max_request_ttl=300,
            max_preflight_validity=60,
        )

    def oidc_verifier(self, token: str, expected_audience: str) -> OIDCClaims:
        self.oidc_calls += 1
        if self.oidc_error or token != TEST_OIDC_BEARER:
            raise RuntimeError("untrusted token")
        if expected_audience != self.identity.audience:
            raise AssertionError("authority supplied the wrong fixed audience")
        return self.oidc_claims

    def signature_verifier(
        self,
        signature_payload: bytes,
        canonical_envelope: bytes,
        signature: str,
    ) -> bool:
        self.signature_calls += 1
        payload = json.loads(signature_payload)
        assert canonical_bytes(payload["envelope"]) == canonical_envelope
        assert "oidc_token" not in payload
        if self.signature_error or signature != _signature(signature_payload):
            raise RuntimeError("invalid signature")
        return True

    @staticmethod
    def response_signer(payload: bytes) -> bytes:
        return SIGNED_PREFIX + payload

    def clock(self) -> int:
        return self.now

    def head_reader(self) -> LifecycleHead:
        self.head_reads += 1
        if self.head_error:
            raise RuntimeError("head unavailable")
        return self.head

    def evaluator(
        self,
        identity: RunIdentity,
        observed_head: LifecycleHead,
        trusted_root_policy_digest: str,
        trusted_decision_policy_digest: str,
    ) -> PreflightEvaluation:
        self.preflight_calls += 1
        assert identity == self.identity
        assert observed_head == self.head
        assert trusted_root_policy_digest.startswith("sha256:")
        assert trusted_decision_policy_digest == self.policy.decision_policy_digest
        if self.evaluator_error:
            raise RuntimeError("evaluation unavailable")
        if self.mutate_head_during_evaluation:
            self.head = dataclasses.replace(
                self.head,
                generation=self.head.generation + 1,
                seal_digest=_digest("mutated-seal"),
            )
        return PreflightEvaluation(
            root_policy_digest=(
                self.evaluator_root_policy_digest_override
                or trusted_root_policy_digest
            ),
            decision_policy_digest=(
                self.evaluator_decision_policy_digest_override
                or trusted_decision_policy_digest
            ),
            checks=self.checks,
        )

    def admission(self, request: AdmissionRequest) -> AdmissionResult:
        self.admission_calls += 1
        self.admission_requests.append(request)
        stored = self.admission_results.get(request.admission_binding_digest)
        if stored is not None:
            return self.admission_transform(stored)
        if self.admission_error:
            raise RuntimeError("admission unavailable")
        if request.observed_current_head is None:
            return self.admission_transform(
                AdmissionResult(
                    admitted=False,
                    request_id=request.request_id,
                    request_transport_digest=request.transport_digest,
                    preflight_request_id=request.ready_preflight.request_id,
                    preflight_transport_digest=request.ready_preflight.transport_digest,
                    predecessor_seal_digest=request.expected_predecessor.seal_digest,
                    root_policy_digest=request.root_policy_digest,
                    admission_binding_digest=request.admission_binding_digest,
                    lifecycle_event_digest=None,
                    error_code="lifecycle-head-unavailable",
                )
            )
        if request.observed_current_head != request.expected_predecessor:
            return self.admission_transform(
                AdmissionResult(
                    admitted=False,
                    request_id=request.request_id,
                    request_transport_digest=request.transport_digest,
                    preflight_request_id=request.ready_preflight.request_id,
                    preflight_transport_digest=request.ready_preflight.transport_digest,
                    predecessor_seal_digest=request.expected_predecessor.seal_digest,
                    root_policy_digest=request.root_policy_digest,
                    admission_binding_digest=request.admission_binding_digest,
                    lifecycle_event_digest=None,
                    error_code="admission-predecessor-changed",
                )
            )
        event_digest = self.admission_effects.setdefault(
            request.admission_binding_digest,
            _digest(f"lifecycle-event:{request.admission_binding_digest}"),
        )
        result = AdmissionResult(
            admitted=True,
            request_id=request.request_id,
            request_transport_digest=request.transport_digest,
            preflight_request_id=request.ready_preflight.request_id,
            preflight_transport_digest=request.ready_preflight.transport_digest,
            predecessor_seal_digest=request.expected_predecessor.seal_digest,
            root_policy_digest=request.root_policy_digest,
            admission_binding_digest=request.admission_binding_digest,
            lifecycle_event_digest=event_digest,
            error_code=None,
        )
        self.admission_results[request.admission_binding_digest] = result
        if self.advance_head_on_admission:
            self.head = dataclasses.replace(
                self.head,
                generation=self.head.generation + 1,
                seal_digest=_digest(
                    f"seal-after:{request.admission_binding_digest}"
                ),
                state_digest=_digest(
                    f"state-after:{request.admission_binding_digest}"
                ),
            )
        return self.admission_transform(result)

    def fault(self, stage: str) -> None:
        self.faults_seen.append(stage)
        if stage == self.fault_stage:
            if self.fault_once:
                self.fault_stage = None
            raise InjectedCrash(stage)

    def model(
        self,
        *,
        policy: object | None = None,
        state: AuthorityState | None = None,
        response_signer: Callable[[bytes], bytes] | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> ReleaseRequestAuthorityModel:
        return ReleaseRequestAuthorityModel(
            policy=self.policy if policy is None else policy,
            oidc_verifier=self.oidc_verifier,
            signature_verifier=self.signature_verifier,
            response_signer=response_signer or self.response_signer,
            trusted_clock=self.clock,
            lifecycle_head_reader=self.head_reader,
            preflight_evaluator=self.evaluator,
            admission_callback=self.admission,
            state=state,
            fault_injector=fault_injector,
        )


def make_transport(
    harness: Harness,
    operation: Operation | str,
    request_id: str,
    *,
    nonce: str | None = None,
    identity: RunIdentity | None = None,
    issued_at: int | None = None,
    expires_at: int | None = None,
    envelope_updates: dict[str, object] | None = None,
    outer_updates: dict[str, object] | None = None,
    trailing_newline: bool = False,
) -> bytes:
    identity = identity or harness.identity
    issued_at = harness.now if issued_at is None else issued_at
    expires_at = issued_at + 100 if expires_at is None else expires_at
    envelope: dict[str, object] = {
        "operation": operation.value if isinstance(operation, Operation) else operation,
        "request_id": request_id,
        "nonce": nonce or f"nonce-{request_id}",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "identity": dataclasses.asdict(identity),
    }
    if envelope_updates:
        envelope.update(envelope_updates)
    envelope_bytes = canonical_bytes(envelope)
    outer: dict[str, object] = {
        "schema": REQUEST_SCHEMA,
        "envelope": envelope,
        "envelope_digest": sha256_digest(envelope_bytes),
    }
    if outer_updates:
        outer.update(outer_updates)
    if "request_signature" not in outer:
        signature_payload = canonical_bytes(
            {
                "schema": outer["schema"],
                "envelope": outer["envelope"],
                "envelope_digest": outer["envelope_digest"],
            }
        )
        outer["request_signature"] = _signature(signature_payload)
    raw = canonical_bytes(outer)
    return raw + (b"\n" if trailing_newline else b"")


def decode_response(response: bytes) -> dict[str, object]:
    assert response.startswith(SIGNED_PREFIX)
    return json.loads(response[len(SIGNED_PREFIX) :])


def assert_error(code: str, call: Callable[[], object]) -> AuthorityModelError:
    with pytest.raises(AuthorityModelError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def send(
    model: ReleaseRequestAuthorityModel,
    raw: bytes,
    oidc_bearer: object = TEST_OIDC_BEARER,
) -> bytes:
    return model.handle(raw, oidc_bearer)  # type: ignore[arg-type]


def issue_ready(
    harness: Harness,
    model: ReleaseRequestAuthorityModel,
    request_id: str = "preflight-1",
) -> tuple[bytes, bytes]:
    raw = make_transport(harness, Operation.RELEASE_PREFLIGHT, request_id)
    return raw, send(model, raw)


def test_contract_is_explicitly_non_authoritative() -> None:
    contract = describe_contract()
    assert contract["authoritative"] is False
    assert contract["cryptographic_authority"] is False
    assert contract["oidc_bearer"] == (
        "out-of-band-never-serialized-or-persisted"
    )
    assert contract["request_signature_binding"] == (
        "canonical-token-independent-request-and-envelope-bytes"
    )
    assert contract["replay_authentication"] == (
        "current-signature-and-oidc-with-exact-envelope-policy-record-"
        "identity-binding"
    )
    assert contract["replay_reevaluation"] == (
        "bypass-clock-lifecycle-head-and-policy-decisions-after-auth"
    )
    assert contract["preflight_mutates_lifecycle"] is False
    assert contract["client_selected_preflight_receipt_allowed"] is False
    assert contract["root_policy"] == "one-immutable-exact-per-run-snapshot"
    assert contract["root_policy_digest"] == {
        "schema": ROOT_POLICY_SCHEMA,
        "algorithm": "sha256",
        "domain_separator": ROOT_POLICY_DIGEST_DOMAIN.decode("ascii"),
        "length_framing": "unsigned-64-bit-big-endian-canonical-json-length",
        "canonical_json": "closed-sorted-keys-no-whitespace-ascii-escaped-utf8",
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
    }
    assert contract["persisted_policy_continuity"] == (
        "missing-or-different-root-policy-digest-rejected-before-replay"
    )
    assert contract["preflight_evaluator_binding"] == (
        "trusted-root-and-decision-policy-digests-passed-and-exactly-echoed"
    )
    assert contract["admission"] == (
        "injected-lookup-first-callback-after-all-checks"
    )
    assert contract["admission_retry_key"] == "admission-binding-digest"
    assert contract["admission_binding_digest"] == {
        "schema": ADMISSION_BINDING_SCHEMA,
        "algorithm": "sha256",
        "domain_separator": ADMISSION_BINDING_DIGEST_DOMAIN.decode("ascii"),
        "length_framing": "unsigned-64-bit-big-endian-canonical-json-length",
        "immutable_predecessor": "ready-preflight-observed-head",
        "excludes": ["release-evaluated-at", "observed-current-head"],
        "stability": (
            "same-prerequisites-same-post-crash-cas-key-after-head-advance"
        ),
    }
    assert contract["admission_callback_contract"] == (
        "lookup-binding-first-return-stored-result-otherwise-require-"
        "observed-head-equals-expected-predecessor-before-atomic-cas"
    )
    assert contract["durability_contract"] == "atomic-state-replacement"
    assert contract["authority_state_storage_requirement"] == (
        "external-authenticated-and-encrypted-durable-storage"
    )
    assert contract["signed_response_requirement"] == (
        "external-signature-verification-required"
    )
    assert set(contract["limitations"]) == {
        (
            "persisted-authority-state-requires-external-authenticated-"
            "encrypted-storage"
        ),
        (
            "injected-evaluator-echo-does-not-authenticate-the-"
            "decision-policy-artifact"
        ),
        "signed-responses-require-external-trust-root-verification",
    }


def test_model_snapshots_one_immutable_exact_per_run_root_policy() -> None:
    harness = Harness()
    model = harness.model()
    original = model.policy

    with pytest.raises(dataclasses.FrozenInstanceError):
        model.policy.max_request_ttl = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        model.policy = dataclasses.replace(  # type: ignore[misc]
            original, max_request_ttl=1
        )

    assert model.policy is original
    assert model.policy.identity.run_id == harness.identity.run_id
    assert model.policy.identity.run_attempt == harness.identity.run_attempt


def test_root_policy_digest_has_fixed_v2_canonical_domain_separated_vector() -> None:
    harness = Harness()
    policy_bytes = canonical_root_policy_bytes(harness.policy)
    decoded = json.loads(policy_bytes)

    assert decoded == {
        "schema": ROOT_POLICY_SCHEMA,
        "identity": dataclasses.asdict(harness.identity),
        "required_checks": list(REQUIRED_CHECKS),
        "decision_policy_digest": _digest("decision-policy-v1"),
        "max_request_ttl": 300,
        "max_preflight_validity": 60,
    }
    assert len(policy_bytes) == 706
    assert root_policy_digest(harness.policy) == (
        "sha256:695d0cd44cdde94729a3d8a308b80d7b21f06f1a46ded4f8686660cad0c3d5e1"
    )
    assert root_policy_digest(harness.policy) == sha256_digest(
        ROOT_POLICY_DIGEST_DOMAIN
        + len(policy_bytes).to_bytes(8, byteorder="big", signed=False)
        + policy_bytes
    )


@pytest.mark.parametrize(
    "changed_policy",
    (
        lambda policy: dataclasses.replace(
            policy,
            identity=dataclasses.replace(policy.identity, candidate_sha="c" * 40),
        ),
        lambda policy: dataclasses.replace(
            policy, required_checks=tuple(reversed(policy.required_checks))
        ),
        lambda policy: dataclasses.replace(
            policy, decision_policy_digest=_digest("decision-policy-v2")
        ),
        lambda policy: dataclasses.replace(
            policy, max_request_ttl=policy.max_request_ttl + 1
        ),
        lambda policy: dataclasses.replace(
            policy, max_preflight_validity=policy.max_preflight_validity + 1
        ),
    ),
)
def test_root_policy_digest_changes_for_every_policy_component(
    changed_policy: Callable[[RootPolicy], RootPolicy],
) -> None:
    harness = Harness()
    original = harness.policy
    changed = changed_policy(original)

    assert root_policy_digest(dataclasses.replace(original)) == root_policy_digest(
        original
    )
    assert root_policy_digest(changed) != root_policy_digest(original)


def test_ready_preflight_is_read_only_and_seals_exact_checks_and_validity() -> None:
    harness = Harness()
    model = harness.model()
    head_before = harness.head

    raw, response = issue_ready(harness, model)

    payload = decode_response(response)
    assert payload["authoritative"] is False
    assert payload["disposition"] == "ready"
    assert payload["request_transport_digest"] == sha256_digest(raw)
    assert payload["canonical_envelope_digest"] == model.state.records[0].canonical_envelope_digest
    assert harness.head == head_before
    assert harness.admission_calls == 0
    assert harness.preflight_calls == 1
    ready = model.state.ready_preflights[0]
    assert ready.observed_head == head_before
    assert ready.checks == harness.checks
    assert ready.evaluated_at == 1_000
    assert ready.valid_until == 1_060
    assert ready.transport_digest == sha256_digest(raw)
    assert ready.response_bytes == response


def test_release_run_uses_only_stored_ready_preflight_and_consumes_it_once() -> None:
    harness = Harness()
    model = harness.model()
    preflight_raw, _ = issue_ready(harness, model)
    run_raw = make_transport(harness, Operation.RELEASE_RUN, "run-1")

    response = send(model, run_raw)

    payload = decode_response(response)
    assert payload["disposition"] == "admitted"
    assert payload["details"]["ready_preflight_request_id"] == "preflight-1"
    assert payload["details"]["ready_preflight_transport_digest"] == sha256_digest(
        preflight_raw
    )
    assert harness.admission_calls == 1
    ready = model.state.ready_preflights[0]
    assert ready.consumed_by_request_id == "run-1"
    assert ready.consumed_by_transport_digest == sha256_digest(run_raw)

    second = make_transport(harness, Operation.RELEASE_RUN, "run-2")
    rejected = decode_response(send(model, second))
    assert rejected["error_code"] == "preflight-already-consumed"
    assert harness.admission_calls == 1


def test_policy_and_admission_digests_bind_ready_run_response_and_cas_key() -> None:
    harness = Harness()
    model = harness.model()
    _, ready_response = issue_ready(harness, model)
    expected_policy_digest = root_policy_digest(harness.policy)
    ready = model.state.ready_preflights[0]

    assert model.policy_digest == expected_policy_digest
    assert ready.root_policy_digest == expected_policy_digest
    assert ready.decision_policy_digest == harness.policy.decision_policy_digest
    assert model.state.records[0].root_policy_digest == expected_policy_digest
    ready_payload = decode_response(ready_response)
    assert ready_payload["root_policy_digest"] == expected_policy_digest
    assert ready_payload["details"]["decision_policy_digest"] == (
        harness.policy.decision_policy_digest
    )

    run_raw = make_transport(harness, Operation.RELEASE_RUN, "run-bound-policy")
    run_response = send(model, run_raw)
    request = harness.admission_requests[0]
    binding = AdmissionBinding(
        root_policy_digest=request.root_policy_digest,
        request_id=request.request_id,
        request_nonce=request.nonce,
        request_transport_digest=request.transport_digest,
        request_envelope_digest=request.envelope_digest,
        request_identity=request.identity,
        ready_preflight=request.ready_preflight,
        expected_predecessor=request.expected_predecessor,
    )
    binding_bytes = canonical_admission_binding_bytes(binding)

    assert request.root_policy_digest == expected_policy_digest
    assert request.admission_binding_digest == admission_binding_digest(binding)
    assert request.admission_binding_digest == sha256_digest(
        ADMISSION_BINDING_DIGEST_DOMAIN
        + len(binding_bytes).to_bytes(8, byteorder="big", signed=False)
        + binding_bytes
    )
    assert request.admission_binding_digest != sha256_digest(run_raw)
    assert set(harness.admission_effects) == {request.admission_binding_digest}
    run_payload = decode_response(run_response)
    assert run_payload["root_policy_digest"] == expected_policy_digest
    assert run_payload["details"]["admission"]["root_policy_digest"] == (
        expected_policy_digest
    )
    assert run_payload["details"]["admission"]["admission_binding_digest"] == (
        request.admission_binding_digest
    )
    assert model.state.records[-1].root_policy_digest == expected_policy_digest


def test_admission_binding_digest_changes_for_every_authority_prerequisite() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    send(model, make_transport(harness, Operation.RELEASE_RUN, "run-binding-inputs"))
    request = harness.admission_requests[0]
    binding = AdmissionBinding(
        root_policy_digest=request.root_policy_digest,
        request_id=request.request_id,
        request_nonce=request.nonce,
        request_transport_digest=request.transport_digest,
        request_envelope_digest=request.envelope_digest,
        request_identity=request.identity,
        ready_preflight=request.ready_preflight,
        expected_predecessor=request.expected_predecessor,
    )
    changed_bindings = (
        dataclasses.replace(
            binding, root_policy_digest=_digest("different-root-policy")
        ),
        dataclasses.replace(
            binding, request_transport_digest=_digest("different-request")
        ),
        dataclasses.replace(
            binding, request_envelope_digest=_digest("different-envelope")
        ),
        dataclasses.replace(
            binding,
            request_identity=dataclasses.replace(
                binding.request_identity, run_id="different-run"
            ),
        ),
        dataclasses.replace(
            binding,
            ready_preflight=dataclasses.replace(
                binding.ready_preflight,
                checks=(
                    dataclasses.replace(
                        binding.ready_preflight.checks[0],
                        evidence_digest=_digest("different-check-evidence"),
                    ),
                    *binding.ready_preflight.checks[1:],
                ),
            ),
        ),
        dataclasses.replace(
            binding,
            expected_predecessor=dataclasses.replace(
                binding.expected_predecessor,
                state_digest=_digest("different-predecessor-state"),
            ),
        ),
    )

    baseline = admission_binding_digest(binding)
    assert len({admission_binding_digest(item) for item in changed_bindings}) == len(
        changed_bindings
    )
    assert all(admission_binding_digest(item) != baseline for item in changed_bindings)


def test_authenticated_exact_raw_retry_is_byte_identical_without_effects() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-replay")
    first = send(model, raw)
    calls = (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.admission_calls,
    )
    harness.now = 999_999
    harness.head = dataclasses.replace(
        harness.head,
        generation=999,
        seal_digest=_digest("unrelated-new-head"),
    )
    replay = send(model, raw)

    assert replay == first
    assert (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.admission_calls,
    ) == (calls[0] + 1, calls[1] + 1, calls[2], calls[3])


@pytest.mark.parametrize(
    ("failure", "bearer", "error_code"),
    [
        ("signature", TEST_OIDC_BEARER, "signature-verification-failed"),
        ("oidc", TEST_OIDC_BEARER, "oidc-verification-failed"),
        (None, "stale-oidc-bearer", "oidc-verification-failed"),
        (None, object(), "oidc-token-invalid"),
    ],
)
def test_exact_replay_rejects_invalid_or_stale_current_authentication(
    failure: str | None, bearer: object, error_code: str
) -> None:
    harness = Harness()
    model = harness.model()
    raw, stored_response = issue_ready(harness, model)
    before = model.state
    if failure is not None:
        setattr(harness, f"{failure}_error", True)

    assert_error(error_code, lambda: send(model, raw, bearer))

    assert model.state == before
    assert model.state.records[0].response_bytes == stored_response


def test_exact_replay_rejects_wrong_current_run_identity_then_allows_valid_auth() -> None:
    harness = Harness()
    model = harness.model()
    raw, stored_response = issue_ready(harness, model)
    before = model.state
    harness.oidc_claims = dataclasses.replace(harness.oidc_claims, run_id="777")

    assert_error(
        "oidc-envelope-identity-mismatch", lambda: send(model, raw)
    )
    assert model.state == before

    harness.oidc_claims = _claims(harness.identity)
    assert send(model, raw) == stored_response
    assert model.state == before


def test_authenticated_exact_replay_rejects_current_root_policy_digest_drift() -> None:
    harness = Harness()
    model = harness.model()
    raw, _ = issue_ready(harness, model)
    before = model.state
    calls = (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    )
    model._policy_digest = _digest("simulated-current-policy-drift")

    assert_error("replay-root-policy-digest-mismatch", lambda: send(model, raw))

    assert model.state == before
    assert (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    ) == (calls[0] + 1, calls[1] + 1, calls[2], calls[3], calls[4])


def test_oidc_bearer_is_out_of_band_and_never_in_raw_or_durable_state() -> None:
    harness = Harness()
    model = harness.model()
    raw, response = issue_ready(harness, model)

    wire = json.loads(raw)
    assert set(wire) == {
        "schema",
        "envelope",
        "envelope_digest",
        "request_signature",
    }
    assert "oidc_token" not in wire
    assert TEST_OIDC_BEARER.encode("utf-8") not in raw
    record = model.state.records[0]
    assert record.raw_transport == raw
    assert TEST_OIDC_BEARER.encode("utf-8") not in record.raw_transport
    assert TEST_OIDC_BEARER not in repr(model.state)
    assert "oidc" not in {field.name for field in dataclasses.fields(record)}

    harness.oidc_error = True
    assert_error(
        "oidc-verification-failed",
        lambda: send(model, raw, "different-or-expired-token"),
    )
    assert model.state.records[0].response_bytes == response


@pytest.mark.parametrize("invalid_bearer", ("", None, b"not-a-string"))
def test_new_request_rejects_missing_or_mistyped_out_of_band_oidc_bearer(
    invalid_bearer: object,
) -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(
        harness, Operation.RELEASE_PREFLIGHT, "invalid-out-of-band-bearer"
    )

    assert_error(
        "oidc-token-invalid",
        lambda: send(model, raw, invalid_bearer),
    )
    assert model.state.records == ()
    assert harness.signature_calls == 1
    assert harness.oidc_calls == 0


def test_request_signature_is_token_independent_but_binds_request_envelope() -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(
        harness, Operation.RELEASE_PREFLIGHT, "token-independent-signature"
    )

    assert_error(
        "oidc-verification-failed",
        lambda: send(model, raw, "wrong-bearer"),
    )
    assert harness.signature_calls == 1
    assert model.state.records == ()

    response = send(model, raw, TEST_OIDC_BEARER)
    assert decode_response(response)["disposition"] == "ready"
    assert harness.signature_calls == 2

    other = make_transport(
        Harness(), Operation.RELEASE_PREFLIGHT, "different-envelope"
    )
    stolen_signature = json.loads(raw)["request_signature"]
    tampered = json.loads(other)
    tampered["request_signature"] = stolen_signature
    assert_error(
        "signature-verification-failed",
        lambda: send(harness.model(), canonical_bytes(tampered)),
    )


def test_changed_bytes_with_used_request_id_or_nonce_are_rejected() -> None:
    harness = Harness()
    model = harness.model()
    raw, _ = issue_ready(harness, model)

    assert_error("request-id-reuse", lambda: send(model, raw + b"\n"))
    nonce_reuse = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "preflight-other",
        nonce="nonce-preflight-1",
    )
    assert_error("nonce-reuse", lambda: send(model, nonce_reuse))
    assert len(model.state.records) == 1


def test_authenticated_signed_rejection_atomically_consumes_id_and_nonce() -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "expired-1",
        issued_at=800,
        expires_at=900,
    )

    first = send(model, raw)
    assert decode_response(first)["error_code"] == "request-expired"
    assert len(model.state.records) == 1
    harness.now = 50_000
    assert send(model, raw) == first
    assert_error("request-id-reuse", lambda: send(model, raw + b"\n"))
    reused_nonce = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "expired-2",
        nonce="nonce-expired-1",
    )
    assert_error("nonce-reuse", lambda: send(model, reused_nonce))


@pytest.mark.parametrize(
    ("field", "changed", "error_code"),
    (
        ("audience", "other-audience", "claim-audience-mismatch"),
        ("repository", "other/property", "claim-repository-mismatch"),
        ("ref", "refs/heads/other", "claim-ref-mismatch"),
        ("candidate_sha", "c" * 40, "claim-candidate-sha-mismatch"),
        (
            "workflow_ref",
            "owner/property/.github/workflows/other.yml@refs/heads/main",
            "claim-workflow-ref-mismatch",
        ),
        ("workflow_sha", "d" * 40, "claim-workflow-sha-mismatch"),
        ("run_id", "999999", "claim-run-id-mismatch"),
        ("run_attempt", 2, "claim-run-attempt-mismatch"),
        ("job", "other-job", "claim-job-mismatch"),
        ("environment", "other-environment", "claim-environment-mismatch"),
    ),
)
def test_every_root_claim_is_exactly_bound(
    field: str, changed: object, error_code: str
) -> None:
    harness = Harness()
    altered = dataclasses.replace(harness.identity, **{field: changed})
    harness.oidc_claims = _claims(altered)
    model = harness.model()
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        f"claim-{field.replace('_', '-')}",
        identity=altered,
    )

    assert_error(error_code, lambda: send(model, raw))
    assert model.state.records == ()


def test_oidc_identity_must_exactly_match_signed_envelope_identity() -> None:
    harness = Harness()
    harness.oidc_claims = dataclasses.replace(harness.oidc_claims, run_id="777")
    model = harness.model()
    raw = make_transport(harness, Operation.RELEASE_PREFLIGHT, "oidc-mismatch")

    assert_error("oidc-envelope-identity-mismatch", lambda: send(model, raw))
    assert model.state.records == ()


@pytest.mark.parametrize("failure", ("signature", "oidc"))
def test_untrusted_signature_or_oidc_failure_does_not_consume_replay_keys(
    failure: str,
) -> None:
    harness = Harness()
    setattr(harness, f"{failure}_error", True)
    model = harness.model()
    raw = make_transport(harness, Operation.RELEASE_PREFLIGHT, f"bad-{failure}")

    expected = (
        "signature-verification-failed"
        if failure == "signature"
        else "oidc-verification-failed"
    )
    assert_error(expected, lambda: send(model, raw))
    assert model.state.records == ()


def test_canonical_envelope_digest_mismatch_is_signed_and_consumed() -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "digest-mismatch",
        outer_updates={"envelope_digest": _digest("wrong-envelope")},
    )

    payload = decode_response(send(model, raw))

    assert payload["error_code"] == "envelope-digest-mismatch"
    assert len(model.state.records) == 1


@pytest.mark.parametrize(
    ("updates", "error_code"),
    (
        ({"expires_at": 1_000}, "request-expiry-order-invalid"),
        ({"expires_at": 1_301}, "request-ttl-exceeded"),
        ({"issued_at": 1_001, "expires_at": 1_101}, "request-issued-in-future"),
        ({"issued_at": 800, "expires_at": 900}, "request-expired"),
    ),
)
def test_trusted_clock_enforces_every_expiry_boundary(
    updates: dict[str, object], error_code: str
) -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        f"expiry-{error_code}",
        envelope_updates=updates,
    )

    assert decode_response(send(model, raw))["error_code"] == error_code
    assert len(model.state.records) == 1


@pytest.mark.parametrize(
    ("raw_factory", "error_code"),
    (
        (lambda h: b"\xef\xbb\xbf{}", "transport-bom-forbidden"),
        (lambda h: b"\xff", "transport-not-utf8"),
        (lambda h: b"{\"schema\":1,\"schema\":2}", "transport-duplicate-key"),
        (lambda h: b"{", "transport-json-invalid"),
        (lambda h: b"x" * (MAX_TRANSPORT_BYTES + 1), "transport-size-invalid"),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "bool-issued-at",
                envelope_updates={"issued_at": True},
            ),
            "request-issued-at-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "bool-expires-at",
                envelope_updates={"expires_at": True},
            ),
            "request-expires-at-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "bearer-on-wire",
                outer_updates={"oidc_token": TEST_OIDC_BEARER},
            ),
            "transport-fields-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "invalid-claimed-digest",
                outer_updates={"envelope_digest": "sha256:not-a-digest"},
            ),
            "claimed-envelope-digest-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "oversized-issued-at",
                issued_at=MAX_SIGNED_INT64 + 1,
                expires_at=MAX_SIGNED_INT64 + 1,
            ),
            "request-issued-at-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "oversized-expires-at",
                expires_at=MAX_SIGNED_INT64 + 1,
            ),
            "request-expires-at-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "bool-run-attempt",
                identity=dataclasses.replace(h.identity, run_attempt=True),
            ),
            "identity-run_attempt-invalid",
        ),
        (
            lambda h: make_transport(
                h,
                Operation.RELEASE_PREFLIGHT,
                "oversized-run-attempt",
                identity=dataclasses.replace(
                    h.identity, run_attempt=MAX_SIGNED_INT64 + 1
                ),
            ),
            "identity-run_attempt-invalid",
        ),
    ),
)
def test_strict_raw_transport_rejects_ambiguous_or_oversized_bytes(
    raw_factory: Callable[[Harness], bytes], error_code: str
) -> None:
    harness = Harness()
    model = harness.model()

    assert_error(error_code, lambda: send(model, raw_factory(harness)))
    assert model.state.records == ()


@pytest.mark.parametrize(
    "raw",
    (
        b'{"value":"\\ud800"}',
        b'{"\\udfff":0}',
    ),
)
def test_json_transport_rejects_escaped_unicode_surrogates(raw: bytes) -> None:
    harness = Harness()
    model = harness.model()

    assert len(raw) < MAX_TRANSPORT_BYTES
    assert_error(
        "transport-unicode-surrogate-forbidden",
        lambda: send(model, raw),
    )
    assert model.state.records == ()


@pytest.mark.parametrize("depth", (MAX_JSON_DEPTH + 2, 2_000))
def test_json_transport_depth_is_bounded_even_below_size_limit(depth: int) -> None:
    harness = Harness()
    model = harness.model()
    raw = b"[" * depth + b"0" + b"]" * depth

    assert len(raw) < MAX_TRANSPORT_BYTES
    assert_error(
        "transport-json-depth-exceeded",
        lambda: send(model, raw),
    )
    assert model.state.records == ()


def test_persisted_trusted_clock_rejects_regression_but_exact_replay_bypasses_it() -> None:
    harness = Harness()
    model = harness.model()
    raw, response = issue_ready(harness, model)
    harness.now = 999

    assert send(model, raw) == response
    new_raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "clock-regression",
        issued_at=999,
        expires_at=1_050,
    )
    assert_error("trusted-clock-regressed", lambda: send(model, new_raw))
    assert len(model.state.records) == 1


@pytest.mark.parametrize(
    "invalid_now", (True, -1, MAX_SIGNED_INT64 + 1, "1000")
)
def test_trusted_clock_result_is_closed_and_typed(invalid_now: object) -> None:
    harness = Harness()
    harness.now = invalid_now  # type: ignore[assignment]
    model = harness.model()
    raw = make_transport(
        Harness(), Operation.RELEASE_PREFLIGHT, "invalid-clock-result"
    )

    assert_error("trusted-clock-invalid", lambda: send(model, raw))
    assert model.state.records == ()


def test_operation_and_envelope_are_closed_and_client_cannot_select_receipt() -> None:
    harness = Harness()
    model = harness.model()
    invalid_operation = make_transport(
        harness, "release-preview", "bad-operation"
    )
    assert_error("operation-invalid", lambda: send(model, invalid_operation))

    selected_receipt = make_transport(
        harness,
        Operation.RELEASE_RUN,
        "client-selected",
        envelope_updates={"preflight_receipt": "attacker-choice"},
    )
    assert_error("envelope-fields-invalid", lambda: send(model, selected_receipt))
    assert model.state.records == ()


@pytest.mark.parametrize(
    ("location", "error_code"),
    (
        ("outer", "transport-fields-invalid"),
        ("envelope", "envelope-fields-invalid"),
    ),
)
def test_client_cannot_supply_or_override_root_policy_digest(
    location: str, error_code: str
) -> None:
    harness = Harness()
    model = harness.model()
    supplied = {"policy_digest": root_policy_digest(harness.policy)}
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        f"caller-policy-digest-{location}",
        outer_updates=supplied if location == "outer" else None,
        envelope_updates=supplied if location == "envelope" else None,
    )

    assert_error(error_code, lambda: send(model, raw))
    assert model.state.records == ()
    assert harness.signature_calls == 0
    assert harness.oidc_calls == 0


@pytest.mark.parametrize(
    ("status", "disposition", "error_code"),
    (
        (CheckStatus.FAIL, "not-ready", "preflight-check-failed"),
        (
            CheckStatus.INDETERMINATE,
            "indeterminate",
            "preflight-check-indeterminate",
        ),
    ),
)
def test_nonpassing_preflight_never_creates_ready_receipt(
    status: CheckStatus, disposition: str, error_code: str
) -> None:
    harness = Harness()
    harness.checks = (
        dataclasses.replace(harness.checks[0], status=status),
        *harness.checks[1:],
    )
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == disposition
    assert payload["error_code"] == error_code
    assert model.state.ready_preflights == ()
    assert harness.admission_calls == 0


@pytest.mark.parametrize(
    ("checks", "error_code"),
    (
        (lambda checks: checks[:-1], "preflight-check-set-mismatch"),
        (lambda checks: tuple(reversed(checks)), "preflight-check-set-mismatch"),
        (
            lambda checks: (checks[0], checks[0], checks[2]),
            "preflight-check-set-duplicate",
        ),
        (
            lambda checks: (
                dataclasses.replace(checks[0], evidence_digest="sha256:bad"),
                *checks[1:],
            ),
            "preflight-check-evidence-invalid",
        ),
    ),
)
def test_preflight_requires_exact_ordered_unique_typed_check_set(
    checks: Callable[[tuple[CheckResult, ...]], tuple[CheckResult, ...]],
    error_code: str,
) -> None:
    harness = Harness()
    harness.checks = checks(harness.checks)
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == "rejected"
    assert payload["error_code"] == error_code
    assert model.state.ready_preflights == ()


@pytest.mark.parametrize(
    ("field", "error_code"),
    (
        ("root_policy", "preflight-root-policy-digest-mismatch"),
        ("decision_policy", "preflight-decision-policy-digest-mismatch"),
    ),
)
def test_preflight_rejects_evaluator_bound_to_a_different_policy_artifact(
    field: str, error_code: str
) -> None:
    harness = Harness()
    setattr(
        harness,
        f"evaluator_{field}_digest_override",
        _digest(f"wrong-{field}"),
    )
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == "rejected"
    assert payload["error_code"] == error_code
    assert payload["root_policy_digest"] == model.policy_digest
    assert model.state.ready_preflights == ()
    assert harness.admission_calls == 0


def test_preflight_refuses_head_change_during_read_only_evaluation() -> None:
    harness = Harness()
    harness.mutate_head_during_evaluation = True
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == "indeterminate"
    assert payload["error_code"] == "preflight-head-changed"
    assert model.state.ready_preflights == ()
    assert harness.admission_calls == 0


@pytest.mark.parametrize("failure", ("head", "evaluator"))
def test_preflight_dependency_failure_is_signed_indeterminate(failure: str) -> None:
    harness = Harness()
    setattr(harness, f"{failure}_error", True)
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == "indeterminate"
    assert payload["error_code"] == "preflight-evaluator-failed"
    assert model.state.ready_preflights == ()
    assert harness.admission_calls == 0


@pytest.mark.parametrize("invalid_generation", (True, MAX_SIGNED_INT64 + 1))
def test_lifecycle_head_generation_rejects_bool_or_signed_int64_overflow(
    invalid_generation: object,
) -> None:
    harness = Harness()
    harness.head = dataclasses.replace(
        harness.head,
        generation=invalid_generation,  # type: ignore[arg-type]
    )
    model = harness.model()

    payload = decode_response(issue_ready(harness, model)[1])

    assert payload["disposition"] == "indeterminate"
    assert payload["error_code"] == "preflight-evaluator-failed"
    assert model.state.ready_preflights == ()


def test_signed_int64_maximum_is_accepted_without_arithmetic_wraparound() -> None:
    harness = Harness()
    harness.now = MAX_SIGNED_INT64 - 1
    harness.identity = dataclasses.replace(
        harness.identity, run_attempt=MAX_SIGNED_INT64
    )
    harness.oidc_claims = _claims(harness.identity)
    harness.head = dataclasses.replace(
        harness.head, generation=MAX_SIGNED_INT64
    )
    model = harness.model(
        policy=dataclasses.replace(
            harness.policy,
            max_request_ttl=MAX_SIGNED_INT64,
            max_preflight_validity=MAX_SIGNED_INT64,
        )
    )
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        "signed-int64-maximum",
        issued_at=MAX_SIGNED_INT64 - 1,
        expires_at=MAX_SIGNED_INT64,
    )

    response = send(model, raw)

    assert decode_response(response)["disposition"] == "ready"
    ready = model.state.ready_preflights[0]
    assert ready.evaluated_at == MAX_SIGNED_INT64 - 1
    assert ready.valid_until == MAX_SIGNED_INT64
    assert ready.observed_head.generation == MAX_SIGNED_INT64


def test_only_one_unconsumed_ready_preflight_exists_per_exact_identity() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)

    second = make_transport(
        harness, Operation.RELEASE_PREFLIGHT, "preflight-second"
    )
    payload = decode_response(send(model, second))

    assert payload["error_code"] == "ready-preflight-already-exists"
    assert len(model.state.ready_preflights) == 1
    assert model.state.ready_preflights[0].consumed_by_request_id is None


def test_release_run_without_ready_never_reaches_admission() -> None:
    harness = Harness()
    model = harness.model()
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-no-ready")

    payload = decode_response(send(model, raw))

    assert payload["error_code"] == "ready-preflight-required"
    assert harness.admission_calls == 0


def test_release_run_head_failure_uses_lookup_first_callback_without_effect() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    harness.head_error = True
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-head-unavailable")

    payload = decode_response(send(model, raw))

    assert payload["error_code"] == "lifecycle-head-unavailable"
    assert harness.admission_calls == 1
    assert harness.admission_effects == {}
    assert model.state.ready_preflights[0].consumed_by_request_id == (
        "run-head-unavailable"
    )


def test_expired_ready_preflight_is_consumed_by_distinct_run_and_can_be_replaced() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    harness.now = 1_060
    run = make_transport(harness, Operation.RELEASE_RUN, "run-expired-ready")

    payload = decode_response(send(model, run))

    assert payload["error_code"] == "ready-preflight-expired"
    assert harness.admission_calls == 0
    assert model.state.ready_preflights[0].consumed_by_request_id == "run-expired-ready"
    replacement = make_transport(
        harness, Operation.RELEASE_PREFLIGHT, "preflight-replacement"
    )
    assert decode_response(send(model, replacement))["disposition"] == "ready"


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("authority", "different-lifecycle-authority"),
        ("namespace", "different-namespace"),
        ("target", "different-target"),
        ("generation", 8),
        ("seal_digest", _digest("different-seal")),
        ("state_digest", _digest("different-state")),
    ),
)
def test_unrelated_head_drift_without_stored_result_rejects_without_effect(
    field: str, changed: object
) -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    harness.head = dataclasses.replace(harness.head, **{field: changed})
    run = make_transport(harness, Operation.RELEASE_RUN, f"run-head-{field}")

    payload = decode_response(send(model, run))

    assert payload["error_code"] == "admission-predecessor-changed"
    assert harness.admission_calls == 1
    assert harness.admission_effects == {}
    assert model.state.ready_preflights[0].consumed_by_request_id == f"run-head-{field}"


@pytest.mark.parametrize(
    "field",
    (
        "request_id",
        "request_transport_digest",
        "preflight_request_id",
        "preflight_transport_digest",
        "predecessor_seal_digest",
        "root_policy_digest",
        "admission_binding_digest",
    ),
)
def test_every_admission_result_binding_is_verified_before_commit(field: str) -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)

    def corrupt(result: AdmissionResult) -> AdmissionResult:
        value = "wrong-binding"
        if field.endswith("digest"):
            value = _digest("wrong-binding")
        return dataclasses.replace(result, **{field: value})

    harness.admission_transform = corrupt
    run = make_transport(harness, Operation.RELEASE_RUN, f"run-binding-{field}")

    payload = decode_response(send(model, run))

    assert payload["error_code"] == "admission-result-binding-mismatch"
    assert harness.admission_calls == 1
    assert model.state.ready_preflights[0].consumed_by_request_id == f"run-binding-{field}"


def test_admission_exception_becomes_signed_rejection_and_consumes_ready() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    harness.admission_error = True
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-admission-error")

    payload = decode_response(send(model, raw))

    assert payload["error_code"] == "admission-callback-failed"
    assert model.state.ready_preflights[0].consumed_by_request_id == "run-admission-error"


def test_valid_external_admission_rejection_is_preserved_and_consumes_ready() -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)

    def reject(result: AdmissionResult) -> AdmissionResult:
        return dataclasses.replace(
            result,
            admitted=False,
            lifecycle_event_digest=None,
            error_code="external-admission-denied",
        )

    harness.admission_transform = reject
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-external-rejected")

    payload = decode_response(send(model, raw))

    assert payload["disposition"] == "rejected"
    assert payload["error_code"] == "external-admission-denied"
    assert model.state.ready_preflights[0].consumed_by_request_id == (
        "run-external-rejected"
    )


@pytest.mark.parametrize(
    ("transform", "error_code"),
    (
        (
            lambda result: dataclasses.replace(
                result, lifecycle_event_digest="sha256:bad"
            ),
            "admission-event-digest-invalid",
        ),
        (
            lambda result: dataclasses.replace(
                result, root_policy_digest="sha256:bad"
            ),
            "admission-root-policy-digest-invalid",
        ),
        (
            lambda result: dataclasses.replace(
                result, admission_binding_digest="sha256:bad"
            ),
            "admission-binding-digest-invalid",
        ),
        (
            lambda result: dataclasses.replace(result, error_code="unexpected-error"),
            "admission-result-invalid",
        ),
        (lambda result: {"admitted": True}, "admission-result-invalid"),
    ),
)
def test_malformed_admission_result_fails_closed_and_consumes_ready(
    transform: Callable[[AdmissionResult], object], error_code: str
) -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    harness.admission_transform = transform  # type: ignore[assignment]
    raw = make_transport(harness, Operation.RELEASE_RUN, f"run-{error_code}")

    payload = decode_response(send(model, raw))

    assert payload["disposition"] == "rejected"
    assert payload["error_code"] == error_code
    assert model.state.ready_preflights[0].consumed_by_request_id == f"run-{error_code}"


@pytest.mark.parametrize(
    ("stage", "callback_count", "committed"),
    (
        ("before-admission-callback", 0, False),
        ("after-admission-callback-before-commit", 1, False),
        ("before-atomic-commit", 1, False),
        ("after-atomic-commit", 1, True),
    ),
)
def test_crash_boundaries_restart_without_partial_authority_state(
    stage: str, callback_count: int, committed: bool
) -> None:
    harness = Harness()
    base = harness.model()
    issue_ready(harness, base)
    before = base.state
    harness.fault_stage = stage
    crashing = harness.model(state=before, fault_injector=harness.fault)
    raw = make_transport(harness, Operation.RELEASE_RUN, f"crash-{stage}")

    with pytest.raises(InjectedCrash):
        send(crashing, raw)

    assert harness.admission_calls == callback_count
    if committed:
        assert len(crashing.state.records) == 2
        assert crashing.state.ready_preflights[0].consumed_by_request_id == f"crash-{stage}"
        stored_response = crashing.state.records[-1].response_bytes
        harness.now = 500_000
        harness.head = dataclasses.replace(
            harness.head,
            generation=900,
            seal_digest=_digest("head-after-committed-crash"),
        )
        restarted = harness.model(state=crashing.state)
        assert send(restarted, raw) == stored_response
        assert harness.admission_calls == callback_count
    else:
        assert crashing.state == before
        restarted = harness.model(state=crashing.state)
        response = send(restarted, raw)
        assert decode_response(response)["disposition"] == "admitted"
        assert len(restarted.state.records) == 2
        assert len(harness.admission_effects) == 1


@pytest.mark.parametrize(
    ("stage", "committed"),
    (("before-atomic-commit", False), ("after-atomic-commit", True)),
)
def test_signed_rejection_consumes_id_and_nonce_atomically_across_restart(
    stage: str, committed: bool
) -> None:
    harness = Harness()
    harness.fault_stage = stage
    model = harness.model(fault_injector=harness.fault)
    raw = make_transport(
        harness,
        Operation.RELEASE_PREFLIGHT,
        f"rejection-crash-{stage}",
        issued_at=800,
        expires_at=900,
    )

    with pytest.raises(InjectedCrash):
        send(model, raw)

    assert len(model.state.records) == int(committed)
    restarted = harness.model(state=model.state)
    response = send(restarted, raw)
    payload = decode_response(response)
    assert payload["error_code"] == "request-expired"
    assert len(restarted.state.records) == 1
    assert_error("request-id-reuse", lambda: send(restarted, raw + b"\n"))


def test_callback_side_effect_is_idempotent_across_post_callback_crash_retry() -> None:
    harness = Harness()
    base = harness.model()
    issue_ready(harness, base)
    harness.fault_stage = "after-admission-callback-before-commit"
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-idempotent-callback")
    crashing = harness.model(state=base.state, fault_injector=harness.fault)

    with pytest.raises(InjectedCrash):
        send(crashing, raw)
    assert harness.admission_calls == 1
    assert len(harness.admission_effects) == 1

    restarted = harness.model(state=crashing.state)
    response = send(restarted, raw)
    assert decode_response(response)["disposition"] == "admitted"
    assert harness.admission_calls == 2
    assert len(harness.admission_effects) == 1


def test_post_callback_crash_recovers_stored_result_after_head_advances() -> None:
    harness = Harness()
    harness.advance_head_on_admission = True
    base = harness.model()
    issue_ready(harness, base)
    expected_predecessor = harness.head
    harness.fault_stage = "after-admission-callback-before-commit"
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-stable-admission-key")
    crashing = harness.model(state=base.state, fault_injector=harness.fault)

    with pytest.raises(InjectedCrash):
        send(crashing, raw)
    first_request = harness.admission_requests[-1]
    stored_result = harness.admission_results[
        first_request.admission_binding_digest
    ]
    assert harness.head != expected_predecessor
    harness.now += 1

    restarted = harness.model(state=crashing.state)
    response = send(restarted, raw)
    second_request = harness.admission_requests[-1]

    payload = decode_response(response)
    assert payload["disposition"] == "admitted"
    assert payload["details"]["admission"] == dataclasses.asdict(stored_result)
    assert first_request.evaluated_at != second_request.evaluated_at
    assert first_request.observed_current_head == expected_predecessor
    assert second_request.observed_current_head == harness.head
    assert second_request.observed_current_head != (
        second_request.expected_predecessor
    )
    assert first_request.admission_binding_digest == (
        second_request.admission_binding_digest
    )
    assert harness.admission_results[first_request.admission_binding_digest] == (
        stored_result
    )
    assert harness.admission_calls == 2
    assert set(harness.admission_effects) == {
        first_request.admission_binding_digest
    }


def test_release_run_signer_failure_retries_external_admission_idempotently() -> None:
    harness = Harness()
    base = harness.model()
    issue_ready(harness, base)
    before = base.state

    def broken_signer(_: bytes) -> bytes:
        raise RuntimeError("signer offline after admission")

    failing = harness.model(state=before, response_signer=broken_signer)
    raw = make_transport(harness, Operation.RELEASE_RUN, "run-signer-retry")
    assert_error("response-signing-failed", lambda: send(failing, raw))
    assert failing.state == before
    assert harness.admission_calls == 1
    assert len(harness.admission_effects) == 1

    restarted = harness.model(state=failing.state)
    assert decode_response(send(restarted, raw))["disposition"] == "admitted"
    assert harness.admission_calls == 2
    assert len(harness.admission_effects) == 1


@pytest.mark.parametrize(
    ("policy_factory", "error_code"),
    (
        (lambda h: "not-a-root-policy", "root-policy-invalid"),
        (
            lambda h: dataclasses.replace(
                h.policy, identity=dataclasses.asdict(h.identity)
            ),
            "root-policy-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy, required_checks=list(REQUIRED_CHECKS)
            ),
            "root-policy-check-set-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy,
                required_checks=(REQUIRED_CHECKS[0], REQUIRED_CHECKS[0]),
            ),
            "root-policy-check-set-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy, decision_policy_digest="sha256:bad"
            ),
            "root-policy-decision-policy-digest-invalid",
        ),
        (
            lambda h: dataclasses.replace(h.policy, max_request_ttl=True),
            "root-policy-request-ttl-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy, max_request_ttl=MAX_SIGNED_INT64 + 1
            ),
            "root-policy-request-ttl-invalid",
        ),
        (
            lambda h: dataclasses.replace(h.policy, max_preflight_validity=True),
            "root-policy-preflight-validity-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy, max_preflight_validity=MAX_SIGNED_INT64 + 1
            ),
            "root-policy-preflight-validity-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy,
                identity=dataclasses.replace(h.identity, run_attempt=True),
            ),
            "identity-run_attempt-invalid",
        ),
        (
            lambda h: dataclasses.replace(
                h.policy,
                identity=dataclasses.replace(
                    h.identity, run_attempt=MAX_SIGNED_INT64 + 1
                ),
            ),
            "identity-run_attempt-invalid",
        ),
    ),
)
def test_root_policy_is_a_closed_typed_trust_root(
    policy_factory: Callable[[Harness], object], error_code: str
) -> None:
    harness = Harness()
    assert_error(
        error_code,
        lambda: harness.model(policy=policy_factory(harness)),
    )


@pytest.mark.parametrize(
    ("state_factory", "error_code"),
    (
        (lambda: {"records": []}, "state-invalid"),
        (
            lambda: AuthorityState(records=[]),
            "state-invalid",
        ),
        (
            lambda: AuthorityState(ready_preflights=[]),
            "state-invalid",
        ),
        (
            lambda: AuthorityState(authority_time=True),
            "state-authority-time-invalid",
        ),
        (
            lambda: AuthorityState(authority_time=MAX_SIGNED_INT64 + 1),
            "state-authority-time-invalid",
        ),
    ),
)
def test_persisted_authority_state_is_closed_and_typed(
    state_factory: Callable[[], object], error_code: str
) -> None:
    harness = Harness()
    assert_error(
        error_code,
        lambda: harness.model(state=state_factory()),  # type: ignore[arg-type]
    )


def test_persisted_state_rejects_policy_drift_before_any_replay_or_effect() -> None:
    harness = Harness()
    original = harness.model()
    raw, response = issue_ready(harness, original)
    state = original.state

    same_policy = harness.model(state=state)
    head_reads = harness.head_reads
    assert send(same_policy, raw) == response
    assert harness.head_reads == head_reads

    drifted_policy = dataclasses.replace(
        harness.policy,
        decision_policy_digest=_digest("replacement-decision-policy"),
    )
    calls_before_rejected_restart = (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    )

    assert_error(
        "state-root-policy-digest-mismatch",
        lambda: harness.model(policy=drifted_policy, state=state),
    )
    assert (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    ) == calls_before_rejected_restart


@pytest.mark.parametrize("artifact", ("record", "ready"))
def test_restart_rejects_record_or_ready_root_policy_digest_mismatch(
    artifact: str,
) -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)
    wrong_digest = _digest(f"wrong-{artifact}-root-policy")
    if artifact == "record":
        state = dataclasses.replace(
            model.state,
            records=(
                dataclasses.replace(
                    model.state.records[0], root_policy_digest=wrong_digest
                ),
            ),
        )
    else:
        state = dataclasses.replace(
            model.state,
            ready_preflights=(
                dataclasses.replace(
                    model.state.ready_preflights[0],
                    root_policy_digest=wrong_digest,
                ),
            ),
        )
    calls = (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    )

    assert_error(
        "state-root-policy-digest-mismatch",
        lambda: harness.model(state=state),
    )
    assert (
        harness.signature_calls,
        harness.oidc_calls,
        harness.head_reads,
        harness.preflight_calls,
        harness.admission_calls,
    ) == calls


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        (
            lambda state: dataclasses.replace(
                state, records=state.records + (state.records[0],)
            ),
            "state-request-id-duplicate",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(
                        state.records[0], raw_transport_digest=_digest("tampered")
                    ),
                ),
            ),
            "state-transport-digest-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(
                        state.records[0], response_digest=_digest("tampered")
                    ),
                ),
            ),
            "state-response-digest-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(
                        state.records[0], disposition="attacker-disposition"
                    ),
                ),
            ),
            "state-record-disposition-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(state.records[0], response_bytes="not-bytes"),
                ),
            ),
            "state-response-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        decision_policy_digest=_digest("wrong-decision-policy"),
                    ),
                ),
            ),
            "state-decision-policy-digest-mismatch",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        valid_until=state.ready_preflights[0].valid_until + 1,
                    ),
                ),
            ),
            "state-ready-preflight-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        checks=(
                            dataclasses.replace(
                                state.ready_preflights[0].checks[0],
                                status=CheckStatus.FAIL,
                            ),
                            *state.ready_preflights[0].checks[1:],
                        ),
                    ),
                ),
            ),
            "state-ready-preflight-not-ready",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        consumed_by_request_id="run-missing",
                    ),
                ),
            ),
            "state-ready-preflight-consumption-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(state.records[0], recorded_at=True),
                ),
            ),
            "state-record-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                records=(
                    dataclasses.replace(
                        state.records[0], recorded_at=MAX_SIGNED_INT64 + 1
                    ),
                ),
            ),
            "state-record-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0], evaluated_at=True
                    ),
                ),
            ),
            "state-ready-preflight-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        evaluated_at=MAX_SIGNED_INT64 + 1,
                    ),
                ),
            ),
            "state-ready-preflight-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0], valid_until=True
                    ),
                ),
            ),
            "state-ready-preflight-time-invalid",
        ),
        (
            lambda state: dataclasses.replace(
                state,
                ready_preflights=(
                    dataclasses.replace(
                        state.ready_preflights[0],
                        valid_until=MAX_SIGNED_INT64 + 1,
                    ),
                ),
            ),
            "state-ready-preflight-time-invalid",
        ),
    ),
)
def test_restart_rejects_hostile_or_internally_inconsistent_durable_state(
    mutation: Callable[[AuthorityState], AuthorityState], error_code: str
) -> None:
    harness = Harness()
    model = harness.model()
    issue_ready(harness, model)

    assert_error(error_code, lambda: harness.model(state=mutation(model.state)))


def test_response_signing_failure_leaves_request_id_and_nonce_unconsumed() -> None:
    harness = Harness()

    def broken_signer(_: bytes) -> bytes:
        raise RuntimeError("signer offline")

    model = harness.model(response_signer=broken_signer)
    raw = make_transport(harness, Operation.RELEASE_PREFLIGHT, "signer-failure")

    assert_error("response-signing-failed", lambda: send(model, raw))
    assert model.state.records == ()


def test_replay_record_binds_both_raw_and_canonical_digests() -> None:
    harness = Harness()
    model = harness.model()
    raw, response = issue_ready(harness, model)
    record: ReplayRecord = model.state.records[0]

    assert record.raw_transport == raw
    assert record.raw_transport_digest == sha256_digest(raw)
    assert record.canonical_envelope_digest == digest_object(
        json.loads(raw)["envelope"]
    )
    assert record.response_bytes == response
    assert record.response_digest == sha256_digest(response)
