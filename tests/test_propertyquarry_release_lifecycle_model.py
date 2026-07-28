from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.propertyquarry_release_lifecycle_model import (
    EXTERNAL_OPERATIONS,
    RAW_RESOURCE_CREDENTIAL_EFFECTS_ALLOWED,
    AdmissionCommand,
    EvidenceManifestEntry,
    FinalEvidence,
    IntentBinding,
    Lease,
    LeasePolicy,
    LeaseRenewalCommand,
    LifecycleBinding,
    LifecycleModelError,
    Phase,
    PreflightRequest,
    ReconciliationAdmissionCommand,
    RecoveryAuthority,
    ReleaseLifecycleModel,
    ResourceContract,
    ResourceKind,
    ResourceOutcome,
    ResourceOutcomeEvidence,
    ResultEvidence,
    RollbackCompletionCommand,
    SUCCESS_PATH,
    TransitionCommand,
    TrustedClockAuthority,
    WORKFLOW_OPERATIONS,
    describe_contract,
    digest,
    evidence_manifest_entries,
    expected_version_digest,
    final_manifest_root,
    global_fence_digest,
    resource_fence_digest,
)


CLOCK_ID = "propertyquarry-trusted-clock"


def _d(label: str) -> str:
    return digest({"test-label": label})


def _resources(*, fence_enforcing: bool = True) -> tuple[ResourceContract, ...]:
    return (
        ResourceContract(
            "database",
            ResourceKind.DATABASE,
            _d("database-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
            forward_compatible_allowed=True,
        ),
        ResourceContract(
            "launch-authority",
            ResourceKind.LAUNCH_AUTHORITY,
            _d("launch-authority-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
        ResourceContract(
            "monitoring-delivery",
            ResourceKind.MONITORING_DELIVERY,
            _d("monitoring-delivery-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
        ResourceContract(
            "gateway",
            ResourceKind.TRAFFIC,
            _d("gateway-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
        ResourceContract(
            "overlay",
            ResourceKind.OVERLAY,
            _d("overlay-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
        ResourceContract(
            "public-tour",
            ResourceKind.PUBLIC_TOUR,
            _d("public-tour-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
        ResourceContract(
            "runtime",
            ResourceKind.RUNTIME,
            _d("runtime-mediator"),
            digest({"capability": "reject-stale-global-and-resource-fences"}),
            fence_enforcing=fence_enforcing,
        ),
    )


def _binding(
    lifecycle_id: str = "lifecycle-1",
    *,
    controller: str = "installed-controller-1",
    release: str = "release-1",
    ttl: int = 120,
    horizon: int = 300,
    resources: tuple[ResourceContract, ...] | None = None,
) -> LifecycleBinding:
    return LifecycleBinding.build(
        lifecycle_id=lifecycle_id,
        release_sha=release,
        controller_digest=controller,
        policy_digest=_d(f"policy:{lifecycle_id}"),
        lease_policy=LeasePolicy(CLOCK_ID, ttl, horizon),
        resources=resources or _resources(),
    )


def _observe(clock: TrustedClockAuthority, at: int | None = None):
    if at is not None:
        clock.advance(at)
    return clock.observe()


def _lease(
    binding: LifecycleBinding,
    observation,
    *,
    lease_id: str = "lease-1",
    holder: str | None = None,
    deadline: int | None = None,
    fence: int = 10,
    resource_fence: int | None = None,
) -> Lease:
    token = fence if resource_fence is None else resource_fence
    return Lease.build(
        lease_id=lease_id,
        holder_id=holder or binding.controller_digest,
        time_authority_id=CLOCK_ID,
        issued_at=observation.observed_at,
        deadline=(observation.observed_at + 100 if deadline is None else deadline),
        fencing_token=fence,
        resource_fencing={resource: token for resource in binding.resource_set},
    )


def _admit(
    model: ReleaseLifecycleModel,
    clock: TrustedClockAuthority,
    *,
    event_id: str = "admit-1",
    binding: LifecycleBinding | None = None,
    epoch: int = 1,
    lease_id: str = "lease-1",
    deadline: int | None = None,
    fence: int = 10,
    resource_fence: int | None = None,
):
    binding = binding or _binding()
    observation = _observe(clock)
    lease = _lease(
        binding,
        observation,
        lease_id=lease_id,
        deadline=deadline,
        fence=fence,
        resource_fence=resource_fence,
    )
    command = AdmissionCommand(
        event_id=event_id,
        request_transport_digest=_d(f"request:{event_id}"),
        binding=binding,
        epoch=epoch,
        lease=lease,
        cas=model.cas_successor(),
        clock=observation,
    )
    return command, model.start_release_run(command)


def _intent(
    model: ReleaseLifecycleModel,
    request_digest: str,
    *,
    cas=None,
    binding=None,
    lease=None,
) -> IntentBinding:
    cas = cas or model.cas_successor()
    binding = binding or model.binding
    lease = lease or model.lease
    assert binding is not None and lease is not None
    return IntentBinding(
        plan_digest=_d("intent-plan"),
        input_digest=_d("intent-input"),
        effect_digest=_d("intent-effect"),
        idempotency_digest=request_digest,
        expected_version_digest=expected_version_digest(cas),
        global_fence_digest=global_fence_digest(lease),
        resource_fence_digest=resource_fence_digest(binding, lease),
    )


def _latest(model: ReleaseLifecycleModel, phase: Phase):
    for record in reversed(model.records):
        if (
            record.phase is phase
            and record.epoch == model.head.epoch
            and record.event_kind == "phase-transition"
        ):
            return record
    raise AssertionError(f"missing phase {phase}")


def _result(model: ReleaseLifecycleModel, intent_phase: Phase, label: str) -> ResultEvidence:
    intent = _latest(model, intent_phase)
    return ResultEvidence(
        intent_record_hash=intent.record_hash,
        result_digest=_d(f"{label}:result"),
        evidence_digest=_d(f"{label}:evidence"),
        persisted_ack_digest=_d(f"{label}:persisted"),
        fsynced_ack_digest=_d(f"{label}:fsynced"),
    )


def _final(model: ReleaseLifecycleModel, label: str = "final") -> FinalEvidence:
    intent = _latest(model, Phase.FINALIZATION_STARTED)
    provisional = FinalEvidence(
        intent_record_hash=intent.record_hash,
        result_digest=_d(f"{label}:result"),
        evidence_digest=_d(f"{label}:evidence"),
        persisted_ack_digest=_d(f"{label}:persisted"),
        fsynced_ack_digest=_d(f"{label}:fsynced"),
        ordered_manifest=evidence_manifest_entries(model.records, model.head.epoch),
        manifest_root=_d("placeholder-root"),
    )
    return replace(provisional, manifest_root=final_manifest_root(provisional))


RESULT_INTENT = {
    Phase.CONTAINED: Phase.CONTAINMENT_STARTED,
    Phase.DEPLOYED: Phase.DEPLOY_STARTED,
    Phase.LIVE_VERIFIED: Phase.LIVE_VERIFICATION_STARTED,
    Phase.ACTIVATION_VERIFIED: Phase.ACTIVATION_STARTED,
    Phase.OVERLAY_ACTIVATED: Phase.OVERLAY_ACTIVATION_STARTED,
}


def _step(
    model: ReleaseLifecycleModel,
    clock: TrustedClockAuthority,
    event_id: str,
    target: Phase,
    *,
    at: int | None = None,
    proof=None,
    request_digest: str | None = None,
    cas=None,
    binding=None,
    lease=None,
):
    observation = _observe(clock, clock.now + 1 if at is None else at)
    request_digest = request_digest or _d(f"request:{event_id}")
    cas = cas or model.cas_successor()
    binding = binding or model.binding
    lease = lease or model.lease
    assert binding is not None and lease is not None
    if proof is None:
        if target.value.endswith("-started"):
            proof = _intent(
                model, request_digest, cas=cas, binding=binding, lease=lease
            )
        elif target is Phase.SEALED_FINAL:
            proof = _final(model)
        else:
            proof = _result(model, RESULT_INTENT[target], event_id)
    command = TransitionCommand(
        event_id=event_id,
        request_transport_digest=request_digest,
        target=target,
        binding=binding,
        lease=lease,
        cas=cas,
        clock=observation,
        proof=proof,
    )
    return command, model.transition(command)


def _seal(model: ReleaseLifecycleModel, clock: TrustedClockAuthority) -> None:
    for index, phase in enumerate(SUCCESS_PATH[1:], 1):
        _step(model, clock, f"success-{index}", phase)


def _outcome(
    resource_id: str,
    outcome: ResourceOutcome,
    *,
    complete_database_proof: bool = True,
) -> ResourceOutcomeEvidence:
    database_fields = {}
    if resource_id == "database" and outcome is ResourceOutcome.RESTORED_VERIFIED:
        database_fields = {
            "backup_digest": _d("db-backup"),
            "schema_digest": _d("db-schema"),
            "wal_or_lsn_digest": _d("db-wal-lsn"),
            "checksum_digest": _d("db-checksum"),
            "probe_digest": _d("db-probe"),
        }
        if not complete_database_proof:
            database_fields["wal_or_lsn_digest"] = None
    elif resource_id == "database" and outcome is ResourceOutcome.FORWARD_COMPATIBLE:
        database_fields = {
            "schema_digest": _d("db-schema"),
            "checksum_digest": _d("db-checksum"),
            "probe_digest": _d("db-probe"),
        }
    return ResourceOutcomeEvidence(
        resource_id=resource_id,
        outcome=outcome,
        evidence_digest=_d(f"{resource_id}:{outcome.value}:evidence"),
        mediator_receipt_digest=_d(f"{resource_id}:mediator-receipt"),
        **database_fields,
    )


def _safe_outcomes(
    database: ResourceOutcome = ResourceOutcome.RESTORED_VERIFIED,
) -> tuple[ResourceOutcomeEvidence, ...]:
    return (
        _outcome("database", database),
        _outcome("gateway", ResourceOutcome.UNCHANGED),
        _outcome("launch-authority", ResourceOutcome.UNCHANGED),
        _outcome("monitoring-delivery", ResourceOutcome.UNCHANGED),
        _outcome("overlay", ResourceOutcome.UNCHANGED),
        _outcome("public-tour", ResourceOutcome.UNCHANGED),
        _outcome("runtime", ResourceOutcome.UNCHANGED),
    )


def _rollback_result(
    model: ReleaseLifecycleModel,
    clock: TrustedClockAuthority,
    event_id: str,
    outcomes: tuple[ResourceOutcomeEvidence, ...],
    *,
    at: int | None = None,
):
    observation = _observe(clock, clock.now + 1 if at is None else at)
    command = RollbackCompletionCommand(
        event_id=event_id,
        request_transport_digest=_d(f"request:{event_id}"),
        binding=model.binding,
        lease=model.lease,
        resource_outcomes=outcomes,
        evidence=_result(model, Phase.ROLLBACK_STARTED, event_id),
        cas=model.cas_successor(),
        clock=observation,
    )
    return command, model.complete_rollback(command)


def _recovery(
    model: ReleaseLifecycleModel,
    clock: TrustedClockAuthority,
    *,
    event_id: str = "recovery-1",
    at: int,
    holder: str = "watchdog-1",
    lease_id: str = "recovery-lease-1",
    fence: int = 20,
    resource_fence: int | None = None,
):
    observation = _observe(clock, at)
    binding = model.binding
    assert binding is not None
    lease = _lease(
        binding,
        observation,
        lease_id=lease_id,
        holder=holder,
        deadline=at + 100,
        fence=fence,
        resource_fence=resource_fence,
    )
    return ReconciliationAdmissionCommand(
        event_id=event_id,
        request_transport_digest=_d(f"request:{event_id}"),
        binding=binding,
        recovery_authority=RecoveryAuthority(
            authority_id=holder,
            authority_digest=_d(f"{holder}:authority"),
            reason_digest=_d(f"{holder}:reason"),
        ),
        recovery_lease=lease,
        cas=model.cas_successor(),
        clock=observation,
    )


def _assert_error(code: str, callback) -> None:
    with pytest.raises(LifecycleModelError) as raised:
        callback()
    assert raised.value.code == code
    assert str(raised.value) == code


def test_contract_is_explicitly_non_authoritative_and_preflight_is_read_only() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    binding = _binding()
    observation = _observe(clock)
    lease = _lease(binding, observation)
    before = model.snapshot()

    result = model.preflight(
        PreflightRequest(
            _d("preflight-request"),
            binding,
            1,
            lease,
            model.cas_successor(),
            observation,
        )
    )

    assert result.authoritative is False
    assert result.next_external_operation == "release-run"
    assert model.snapshot() == before
    assert EXTERNAL_OPERATIONS == (
        "release-preflight",
        "release-run",
        "reconcile-run",
    )
    assert WORKFLOW_OPERATIONS == ("release-preflight", "release-run")
    assert RAW_RESOURCE_CREDENTIAL_EFFECTS_ALLOWED is False
    contract = describe_contract()
    assert contract["cryptographic_authority"] is False
    assert contract["resource_effect_path"] == "fence-enforcing-mediators-only"
    assert contract["event_index_storage"] == "same-external-cas-record-as-chain-append"
    assert contract["replay_return_requires"] == [
        "event-id",
        "request-transport-digest",
        "event-kind",
        "phase-when-applicable",
        "exact-immutable-command-context",
    ]
    assert contract["replay_command_context"] == (
        "binding-policy-controller-resources-epoch-lease-and-effect-inputs;"
        "clock-and-cas-excluded"
    )
    assert contract["persisted_clock_receipts"] == "restart-verifiable-authenticated-harness"
    assert contract["required_resource_kinds"] == [
        "database",
        "launch-authority",
        "monitoring-delivery",
        "overlay",
        "public-tour",
        "runtime",
        "traffic",
    ]


def test_happy_path_binds_every_intent_result_and_final_ordered_manifest() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _seal(model, clock)

    assert tuple(record.phase for record in model.records) == SUCCESS_PATH
    for record in model.records:
        if record.phase.value.endswith("-started"):
            assert isinstance(record.proof, IntentBinding)
    final = model.head
    assert final is not None and isinstance(final.proof, FinalEvidence)
    assert tuple(entry.phase for entry in final.proof.ordered_manifest) == (
        Phase.CONTAINED,
        Phase.DEPLOYED,
        Phase.LIVE_VERIFIED,
        Phase.ACTIVATION_VERIFIED,
        Phase.OVERLAY_ACTIVATED,
    )
    assert final.proof.manifest_root == final_manifest_root(final.proof)
    assert model.verify_chain() is True


def test_intent_requires_all_exact_plan_version_idempotency_and_fence_bindings() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    request = _d("request:intent")
    valid = _intent(model, request)
    cases = (
        (replace(valid, plan_digest="bad"), "invalid-intent-binding"),
        (replace(valid, idempotency_digest=_d("other")), "intent-idempotency-binding-mismatch"),
        (replace(valid, expected_version_digest=_d("other")), "intent-expected-version-mismatch"),
        (replace(valid, global_fence_digest=_d("other")), "intent-global-fence-mismatch"),
        (replace(valid, resource_fence_digest=_d("other")), "intent-resource-fence-mismatch"),
    )
    for index, (proof, code) in enumerate(cases):
        _assert_error(
            code,
            lambda proof=proof, index=index: _step(
                model,
                clock,
                f"bad-intent-{index}",
                Phase.CONTAINMENT_STARTED,
                proof=proof,
                request_digest=request,
            ),
        )


def test_result_requires_exact_intent_evidence_and_persisted_fsynced_acks() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _step(model, clock, "containment-start", Phase.CONTAINMENT_STARTED)
    valid = _result(model, Phase.CONTAINMENT_STARTED, "contained")
    cases = (
        (replace(valid, intent_record_hash=_d("wrong-intent")), "result-intent-binding-mismatch"),
        (replace(valid, evidence_digest="bad"), "invalid-result-evidence"),
        (replace(valid, persisted_ack_digest="bad"), "invalid-result-evidence"),
        (replace(valid, fsynced_ack_digest="bad"), "invalid-result-evidence"),
    )
    for index, (proof, code) in enumerate(cases):
        _assert_error(
            code,
            lambda proof=proof, index=index: _step(
                model, clock, f"bad-result-{index}", Phase.CONTAINED, proof=proof
            ),
        )


def test_finalization_rejects_missing_unordered_or_wrong_root_manifest() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    for index, phase in enumerate(SUCCESS_PATH[1:-1], 1):
        _step(model, clock, f"pre-final-{index}", phase)
    valid = _final(model)
    missing = replace(valid, ordered_manifest=valid.ordered_manifest[:-1])
    wrong_order = replace(valid, ordered_manifest=tuple(reversed(valid.ordered_manifest)))
    wrong_root = replace(valid, manifest_root=_d("wrong-root"))
    _assert_error(
        "final-manifest-incomplete-or-unordered",
        lambda: _step(model, clock, "missing-manifest", Phase.SEALED_FINAL, proof=missing),
    )
    _assert_error(
        "final-manifest-incomplete-or-unordered",
        lambda: _step(model, clock, "unordered-manifest", Phase.SEALED_FINAL, proof=wrong_order),
    )
    _assert_error(
        "final-manifest-root-mismatch",
        lambda: _step(model, clock, "wrong-root", Phase.SEALED_FINAL, proof=wrong_root),
    )


def test_replay_identity_excludes_later_clock_and_cas_but_conflicts_on_request_digest() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    command, first = _admit(model, clock)
    later = _observe(clock, 50)
    retry = replace(command, clock=later, cas=model.cas_successor())

    assert model.start_release_run(retry) is first
    assert len(model.records) == 1
    _assert_error(
        "event-id-conflict",
        lambda: model.start_release_run(
            replace(retry, request_transport_digest=_d("altered-signed-request"))
        ),
    )


def test_admission_replay_rejects_binding_epoch_resource_and_lease_substitution() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    command, _first = _admit(model, clock)
    before = model.records
    later = _observe(clock, 50)
    retry = replace(command, clock=later, cas=model.cas_successor())
    resources = list(command.binding.resources)
    resources[0] = replace(
        resources[0], mediator_digest=_d("substituted-resource-mediator")
    )

    substitutions = (
        replace(
            retry,
            binding=replace(
                command.binding, policy_digest=_d("substituted-policy")
            ),
        ),
        replace(
            retry,
            binding=replace(
                command.binding, controller_digest="substituted-controller"
            ),
        ),
        replace(
            retry,
            binding=replace(command.binding, resources=tuple(resources)),
        ),
        replace(retry, epoch=2),
        replace(retry, lease=replace(command.lease, lease_id="substituted-lease")),
    )
    for substituted in substitutions:
        _assert_error(
            "event-replay-context-mismatch",
            lambda substituted=substituted: model.start_release_run(substituted),
        )
        assert model.records == before


def test_every_replay_binds_effect_inputs_but_excludes_fresh_clock_and_cas() -> None:
    transition_clock = TrustedClockAuthority(CLOCK_ID, 10)
    transition_model = ReleaseLifecycleModel(transition_clock)
    _admit(transition_model, transition_clock)
    transition, transition_record = _step(
        transition_model,
        transition_clock,
        "replay-transition",
        Phase.CONTAINMENT_STARTED,
    )
    transition_later = _observe(transition_clock, 50)
    transition_retry = replace(
        transition,
        clock=transition_later,
        cas=transition_model.cas_successor(),
    )
    assert transition_model.transition(transition_retry) is transition_record
    assert isinstance(transition.proof, IntentBinding)
    _assert_error(
        "event-replay-context-mismatch",
        lambda: transition_model.transition(
            replace(
                transition_retry,
                proof=replace(
                    transition.proof, plan_digest=_d("substituted-plan")
                ),
            )
        ),
    )

    renewal_clock = TrustedClockAuthority(CLOCK_ID, 10)
    renewal_model = ReleaseLifecycleModel(renewal_clock)
    _admit(renewal_model, renewal_clock, deadline=70)
    original_lease = renewal_model.lease
    assert original_lease is not None
    renewal_observation = _observe(renewal_clock, 20)
    renewal = LeaseRenewalCommand(
        "replay-renewal",
        _d("request:replay-renewal"),
        renewal_model.binding,
        original_lease,
        100,
        renewal_model.cas_successor(),
        renewal_observation,
    )
    renewal_record = renewal_model.renew_lease(renewal)
    renewal_later = _observe(renewal_clock, 30)
    renewal_retry = replace(
        renewal, clock=renewal_later, cas=renewal_model.cas_successor()
    )
    assert renewal_model.renew_lease(renewal_retry) is renewal_record
    _assert_error(
        "event-replay-context-mismatch",
        lambda: renewal_model.renew_lease(
            replace(renewal_retry, new_deadline=101)
        ),
    )

    recovery_clock = TrustedClockAuthority(CLOCK_ID, 10)
    recovery_model = ReleaseLifecycleModel(recovery_clock)
    _admit(recovery_model, recovery_clock, deadline=30)
    _step(
        recovery_model,
        recovery_clock,
        "recovery-intent",
        Phase.CONTAINMENT_STARTED,
    )
    recovery = _recovery(recovery_model, recovery_clock, at=30)
    recovery_record = recovery_model.admit_reconciliation(recovery)
    recovery_later = _observe(recovery_clock, 40)
    recovery_retry = replace(
        recovery, clock=recovery_later, cas=recovery_model.cas_successor()
    )
    assert recovery_model.admit_reconciliation(recovery_retry) is recovery_record
    _assert_error(
        "event-replay-context-mismatch",
        lambda: recovery_model.admit_reconciliation(
            replace(
                recovery_retry,
                recovery_authority=replace(
                    recovery.recovery_authority,
                    reason_digest=_d("substituted-recovery-reason"),
                ),
            )
        ),
    )

    rollback_clock = TrustedClockAuthority(CLOCK_ID, 10)
    rollback_model = ReleaseLifecycleModel(rollback_clock)
    _admit(rollback_model, rollback_clock)
    _step(
        rollback_model,
        rollback_clock,
        "rollback-intent",
        Phase.ROLLBACK_STARTED,
    )
    rollback, rollback_record = _rollback_result(
        rollback_model,
        rollback_clock,
        "replay-rollback",
        _safe_outcomes(),
    )
    rollback_later = _observe(rollback_clock, 50)
    rollback_retry = replace(
        rollback, clock=rollback_later, cas=rollback_model.cas_successor()
    )
    assert rollback_model.complete_rollback(rollback_retry) is rollback_record
    substituted_outcomes = list(rollback.resource_outcomes)
    substituted_outcomes[0] = replace(
        substituted_outcomes[0], evidence_digest=_d("substituted-outcome")
    )
    _assert_error(
        "event-replay-context-mismatch",
        lambda: rollback_model.complete_rollback(
            replace(rollback_retry, resource_outcomes=tuple(substituted_outcomes))
        ),
    )


def test_restart_rebuilds_event_index_and_preserves_byte_identical_replay() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    command, first = _admit(model, clock)
    _step(model, clock, "containment-start", Phase.CONTAINMENT_STARTED)

    restored = ReleaseLifecycleModel(clock, model.records)
    later = _observe(clock, 80)
    replay = restored.start_release_run(
        replace(command, clock=later, cas=restored.cas_successor())
    )
    assert replay == first
    assert replay.record_hash == first.record_hash
    assert restored.verify_chain() is True


def test_restart_rejects_duplicate_conflicting_and_semantically_tampered_records() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _step(model, clock, "containment-start", Phase.CONTAINMENT_STARTED)
    records = model.records

    _assert_error(
        "persisted-event-id-duplicate",
        lambda: ReleaseLifecycleModel(clock, records + (records[-1],)),
    )
    conflicting = replace(
        records[-1], request_transport_digest=_d("conflicting-request")
    )
    _assert_error(
        "persisted-event-id-conflict",
        lambda: ReleaseLifecycleModel(clock, records + (conflicting,)),
    )

    tampered = replace(
        records[-1], phase=Phase.CONTAINED, state=Phase.CONTAINED.value
    )
    tampered = replace(
        tampered,
        record_hash=digest(ReleaseLifecycleModel._record_payload(tampered)),
    )
    _assert_error(
        "chain-transition-invalid",
        lambda: ReleaseLifecycleModel(clock, (records[0], tampered)),
    )


def test_restart_semantic_audit_rejects_rehashed_binding_fence_and_outcome_forgery() -> None:
    binding_clock = TrustedClockAuthority(CLOCK_ID, 10)
    binding_model = ReleaseLifecycleModel(binding_clock)
    _, admission = _admit(binding_model, binding_clock)
    wrong_binding = replace(
        admission, binding=replace(admission.binding, controller_digest="forged-controller")
    )
    wrong_binding = replace(
        wrong_binding,
        record_hash=digest(ReleaseLifecycleModel._record_payload(wrong_binding)),
    )
    _assert_error(
        "chain-holder-controller-mismatch",
        lambda: ReleaseLifecycleModel(binding_clock, (wrong_binding,)),
    )

    fence_clock = TrustedClockAuthority(CLOCK_ID, 10)
    fence_model = ReleaseLifecycleModel(fence_clock)
    _admit(fence_model, fence_clock, deadline=30)
    _step(fence_model, fence_clock, "intent", Phase.CONTAINMENT_STARTED)
    recovery = fence_model.admit_reconciliation(
        _recovery(fence_model, fence_clock, at=30)
    )
    stale_fence = replace(
        recovery,
        lease=replace(recovery.lease, fencing_token=10),
    )
    stale_fence = replace(
        stale_fence,
        record_hash=digest(ReleaseLifecycleModel._record_payload(stale_fence)),
    )
    _assert_error(
        "chain-global-fence-stale",
        lambda: ReleaseLifecycleModel(
            fence_clock, fence_model.records[:-1] + (stale_fence,)
        ),
    )

    outcome_clock = TrustedClockAuthority(CLOCK_ID, 10)
    outcome_model = ReleaseLifecycleModel(outcome_clock)
    _admit(outcome_model, outcome_clock)
    _step(outcome_model, outcome_clock, "rollback", Phase.ROLLBACK_STARTED)
    _, terminal = _rollback_result(
        outcome_model, outcome_clock, "complete", _safe_outcomes()
    )
    forged_outcomes = list(terminal.resource_outcomes)
    forged_outcomes[1] = _outcome("gateway", ResourceOutcome.FORWARD_COMPATIBLE)
    forged_terminal = replace(terminal, resource_outcomes=tuple(forged_outcomes))
    forged_terminal = replace(
        forged_terminal,
        record_hash=digest(ReleaseLifecycleModel._record_payload(forged_terminal)),
    )
    _assert_error(
        "forward-compatible-outcome-not-allowed",
        lambda: ReleaseLifecycleModel(
            outcome_clock, outcome_model.records[:-1] + (forged_terminal,)
        ),
    )


def test_trusted_clock_rejects_caller_forged_or_future_observation() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    binding = _binding()
    valid = _observe(clock)
    forged = replace(valid, issued_at=999, observed_at=999)
    lease = _lease(binding, forged, deadline=1_050)
    command = AdmissionCommand(
        "forged-clock",
        _d("request:forged-clock"),
        binding,
        1,
        lease,
        model.cas_successor(),
        forged,
    )
    _assert_error(
        "untrusted-clock-observation", lambda: model.start_release_run(command)
    )


def test_stale_authority_observation_cannot_authorize_a_new_event() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _, admitted = _admit(model, clock)
    stale = admitted.clock
    clock.advance(20)
    request = _d("request:stale-clock")
    command = TransitionCommand(
        "stale-clock",
        request,
        Phase.CONTAINMENT_STARTED,
        model.binding,
        model.lease,
        model.cas_successor(),
        stale,
        _intent(model, request),
    )
    _assert_error(
        "untrusted-clock-observation", lambda: model.transition(command)
    )


def test_lease_issued_at_max_ttl_and_renewal_horizon_are_enforced() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    binding = _binding(ttl=50, horizon=80)
    observation = _observe(clock)

    bad_issue_model = ReleaseLifecycleModel(clock)
    bad_issue = _lease(binding, observation, deadline=40)
    bad_issue = replace(bad_issue, issued_at=9, renewal_origin=9)
    _assert_error(
        "lease-issued-at-mismatch",
        lambda: bad_issue_model.start_release_run(
            AdmissionCommand(
                "bad-issued-at",
                _d("request:bad-issued-at"),
                binding,
                1,
                bad_issue,
                bad_issue_model.cas_successor(),
                observation,
            )
        ),
    )

    ttl_model = ReleaseLifecycleModel(clock)
    too_long = _lease(binding, observation, deadline=61)
    _assert_error(
        "lease-max-ttl-exceeded",
        lambda: ttl_model.start_release_run(
            AdmissionCommand(
                "ttl",
                _d("request:ttl"),
                binding,
                1,
                too_long,
                ttl_model.cas_successor(),
                observation,
            )
        ),
    )

    model = ReleaseLifecycleModel(clock)
    valid = _lease(binding, observation, deadline=50)
    model.start_release_run(
        AdmissionCommand(
            "valid",
            _d("request:valid"),
            binding,
            1,
            valid,
            model.cas_successor(),
            observation,
        )
    )
    renewal_clock = _observe(clock, 40)
    renewal = LeaseRenewalCommand(
        "renew-too-far",
        _d("request:renew-too-far"),
        binding,
        model.lease,
        91,
        model.cas_successor(),
        renewal_clock,
    )
    _assert_error("lease-max-ttl-exceeded", lambda: model.renew_lease(renewal))
    horizon = replace(renewal, event_id="horizon", request_transport_digest=_d("request:horizon"), new_deadline=91)
    # Move late enough for the per-renewal TTL to pass, while origin horizon fails.
    later = _observe(clock, 41)
    horizon = replace(horizon, clock=later, new_deadline=91)
    _assert_error(
        "lease-renewal-horizon-exceeded", lambda: model.renew_lease(horizon)
    )


def test_valid_renewal_preserves_holder_fences_and_rejects_stale_view() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock, deadline=70)
    old = model.lease
    observation = _observe(clock, 20)
    renewal = LeaseRenewalCommand(
        "renew",
        _d("request:renew"),
        model.binding,
        old,
        100,
        model.cas_successor(),
        observation,
    )
    renewed = model.renew_lease(renewal)
    assert renewed.lease.holder_id == old.holder_id
    assert renewed.lease.fencing_token == old.fencing_token
    assert renewed.lease.resource_fencing == old.resource_fencing
    _assert_error(
        "lease-window-mismatch",
        lambda: _step(
            model,
            clock,
            "stale-lease-view",
            Phase.CONTAINMENT_STARTED,
            lease=old,
        ),
    )


def test_renewal_between_intent_and_result_does_not_replace_intent_identity() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _, intent = _step(model, clock, "containment-intent", Phase.CONTAINMENT_STARTED)
    observation = _observe(clock, 12)
    renewal = LeaseRenewalCommand(
        "mid-intent-renewal",
        _d("request:mid-intent-renewal"),
        model.binding,
        model.lease,
        120,
        model.cas_successor(),
        observation,
    )
    model.renew_lease(renewal)
    _, result = _step(model, clock, "contained", Phase.CONTAINED)
    assert isinstance(result.proof, ResultEvidence)
    assert result.proof.intent_record_hash == intent.record_hash
    assert model.verify_chain() is True


def test_resources_require_fence_enforcing_mediator_capability() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    binding = _binding(resources=_resources(fence_enforcing=False))
    observation = _observe(clock)
    command = AdmissionCommand(
        "raw-resource-path",
        _d("request:raw-resource-path"),
        binding,
        1,
        _lease(binding, observation),
        model.cas_successor(),
        observation,
    )
    _assert_error(
        "resource-mediator-not-fence-enforcing",
        lambda: model.start_release_run(command),
    )

    wrong_capability_resources = list(_resources())
    wrong_capability_resources[0] = replace(
        wrong_capability_resources[0], fence_capability="accept-any-fence"
    )
    wrong_binding = _binding(resources=tuple(wrong_capability_resources))
    observation = _observe(clock)
    wrong_command = AdmissionCommand(
        "wrong-capability",
        _d("request:wrong-capability"),
        wrong_binding,
        1,
        _lease(wrong_binding, observation),
        model.cas_successor(),
        observation,
    )
    _assert_error(
        "resource-fence-capability-invalid",
        lambda: model.start_release_run(wrong_command),
    )

    incomplete_binding = _binding(resources=_resources()[:-1])
    observation = _observe(clock)
    incomplete_command = AdmissionCommand(
        "incomplete-domain",
        _d("request:incomplete-domain"),
        incomplete_binding,
        1,
        _lease(incomplete_binding, observation),
        model.cas_successor(),
        observation,
    )
    _assert_error(
        "required-resource-domain-incomplete",
        lambda: model.start_release_run(incomplete_command),
    )


def test_resource_outcomes_are_kind_constrained_and_proof_bound() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _step(model, clock, "rollback-start", Phase.ROLLBACK_STARTED)

    traffic_forward = list(_safe_outcomes(ResourceOutcome.UNCHANGED))
    traffic_forward[1] = _outcome("gateway", ResourceOutcome.FORWARD_COMPATIBLE)
    _assert_error(
        "forward-compatible-outcome-not-allowed",
        lambda: _rollback_result(
            model, clock, "traffic-forward", tuple(traffic_forward)
        ),
    )
    db_incomplete = list(_safe_outcomes())
    db_incomplete[0] = _outcome(
        "database",
        ResourceOutcome.RESTORED_VERIFIED,
        complete_database_proof=False,
    )
    _assert_error(
        "database-restore-proof-incomplete",
        lambda: _rollback_result(
            model, clock, "db-incomplete", tuple(db_incomplete)
        ),
    )
    overlay_restored = list(_safe_outcomes(ResourceOutcome.UNCHANGED))
    overlay_restored[4] = _outcome("overlay", ResourceOutcome.RESTORED_VERIFIED)
    _, terminal = _rollback_result(
        model, clock, "overlay-restored", tuple(overlay_restored)
    )
    assert terminal.phase is Phase.ROLLED_BACK


def test_database_forward_compatible_is_safe_only_when_policy_declares_it() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _step(model, clock, "rollback-start", Phase.ROLLBACK_STARTED)
    _, terminal = _rollback_result(
        model,
        clock,
        "forward-db",
        _safe_outcomes(ResourceOutcome.FORWARD_COMPATIBLE),
    )
    assert terminal.phase is Phase.ROLLED_BACK


def test_expired_owner_has_zero_writes_including_rollback_and_renewal() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock, deadline=30)
    _step(model, clock, "containment-start", Phase.CONTAINMENT_STARTED)
    _observe(clock, 30)
    count = len(model.records)
    _assert_error(
        "lease-expired",
        lambda: _step(model, clock, "expired-forward", Phase.CONTAINED, at=30),
    )
    _assert_error(
        "lease-expired",
        lambda: _step(model, clock, "expired-rollback", Phase.ROLLBACK_STARTED, at=30),
    )
    renewal = LeaseRenewalCommand(
        "expired-renew",
        _d("request:expired-renew"),
        model.binding,
        model.lease,
        80,
        model.cas_successor(),
        _observe(clock, 30),
    )
    _assert_error("lease-expired", lambda: model.renew_lease(renewal))
    assert len(model.records) == count


def test_orphaned_intent_requires_new_holder_lease_and_higher_all_fences() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock, deadline=30)
    _step(model, clock, "deploy-intent", Phase.CONTAINMENT_STARTED)

    before = _recovery(model, clock, event_id="before", at=29)
    _assert_error(
        "reconciliation-before-lease-expiry",
        lambda: model.admit_reconciliation(before),
    )
    stale_global = _recovery(model, clock, event_id="stale-global", at=30, fence=10, resource_fence=20)
    _assert_error(
        "fencing-token-not-increasing",
        lambda: model.admit_reconciliation(stale_global),
    )
    stale_resource = _recovery(model, clock, event_id="stale-resource", at=30, fence=20, resource_fence=10)
    _assert_error(
        "resource-fencing-token-not-increasing",
        lambda: model.admit_reconciliation(stale_resource),
    )
    same_holder = _recovery(model, clock, event_id="same-holder", at=30, holder="installed-controller-1")
    _assert_error(
        "recovery-holder-not-new",
        lambda: model.admit_reconciliation(same_holder),
    )

    recovery = _recovery(model, clock, at=30)
    recovered = model.admit_reconciliation(recovery)
    assert recovered.phase is Phase.RECONCILIATION_ADMITTED
    assert recovered.reconciliation_from_phase is Phase.CONTAINMENT_STARTED
    _step(model, clock, "recovery-rollback", Phase.ROLLBACK_STARTED)
    _, terminal = _rollback_result(model, clock, "recovery-complete", _safe_outcomes())
    assert terminal.phase is Phase.ROLLED_BACK


def test_contained_failed_blocks_normal_release_but_allows_exact_recovery_then_epoch_successor() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock, deadline=30)
    _step(model, clock, "rollback-start", Phase.ROLLBACK_STARTED)
    unresolved = list(_safe_outcomes(ResourceOutcome.UNCHANGED))
    unresolved[0] = _outcome("database", ResourceOutcome.UNRESOLVED)
    _, failed = _rollback_result(model, clock, "unresolved", tuple(unresolved))
    assert failed.phase is Phase.CONTAINED_FAILED

    next_binding = _binding("lifecycle-2", controller="installed-controller-2")
    observation = _observe(clock, 20)
    illegal = AdmissionCommand(
        "illegal-next",
        _d("request:illegal-next"),
        next_binding,
        2,
        _lease(next_binding, observation, lease_id="lease-2", fence=20),
        model.cas_successor(),
        observation,
    )
    _assert_error(
        "prior-lifecycle-not-terminal-safe",
        lambda: model.start_release_run(illegal),
    )

    model.admit_reconciliation(_recovery(model, clock, at=30))
    _step(model, clock, "retry-rollback", Phase.ROLLBACK_STARTED)
    _, rolled_back = _rollback_result(model, clock, "restored", _safe_outcomes())
    assert rolled_back.phase is Phase.ROLLED_BACK

    next_observation = _observe(clock, clock.now + 1)
    next_command = AdmissionCommand(
        "admit-2",
        _d("request:admit-2"),
        next_binding,
        2,
        _lease(
            next_binding,
            next_observation,
            lease_id="lease-2",
            fence=30,
            resource_fence=30,
        ),
        model.cas_successor(),
        next_observation,
    )
    next_record = model.start_release_run(next_command)
    assert next_record.epoch == 2
    assert next_record.predecessor_hash == rolled_back.record_hash


def test_sealed_final_forbids_same_epoch_rollback_and_links_epoch_plus_one() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _seal(model, clock)
    sealed = model.head
    _assert_error(
        "sealed-final-rollback-forbidden",
        lambda: _step(model, clock, "late-rollback", Phase.ROLLBACK_STARTED),
    )

    binding = _binding("lifecycle-2", controller="installed-controller-2")
    observation = _observe(clock, clock.now + 1)
    command = AdmissionCommand(
        "admit-next",
        _d("request:admit-next"),
        binding,
        2,
        _lease(binding, observation, lease_id="lease-2", fence=20, resource_fence=20),
        model.cas_successor(),
        observation,
    )
    admitted = model.start_release_run(command)
    assert admitted.epoch == 2
    assert admitted.predecessor_hash == sealed.record_hash


def test_phase_skips_reversals_repeats_and_cas_forks_are_rejected() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _assert_error(
        "transition-not-allowed",
        lambda: _step(model, clock, "skip", Phase.CONTAINED, proof=_d("wrong")),
    )
    shared = model.cas_successor()
    first, _ = _step(model, clock, "winner", Phase.CONTAINMENT_STARTED, cas=shared)
    proof = _intent(model, _d("request:loser"), cas=shared)
    _assert_error(
        "cas-predecessor-generation-mismatch",
        lambda: _step(model, clock, "loser", Phase.CONTAINMENT_STARTED, cas=shared, proof=proof),
    )
    _assert_error(
        "transition-not-allowed",
        lambda: _step(model, clock, "repeat", Phase.CONTAINMENT_STARTED),
    )
    assert first.cas == shared


def test_exact_replay_is_bound_to_the_original_operation_and_phase() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    admission_command, admission = _admit(model, clock)

    assert model.start_release_run(admission_command) is admission
    cross_operation = TransitionCommand(
        event_id=admission_command.event_id,
        request_transport_digest=admission_command.request_transport_digest,
        target=Phase.CONTAINMENT_STARTED,
        binding=model.binding,
        lease=model.lease,
        cas=model.cas_successor(),
        clock=_observe(clock, 11),
        proof=_intent(model, admission_command.request_transport_digest),
    )
    _assert_error(
        "event-replay-kind-mismatch",
        lambda: model.transition(cross_operation),
    )

    transition_command, transition = _step(
        model,
        clock,
        "containment-intent",
        Phase.CONTAINMENT_STARTED,
    )
    assert model.transition(transition_command) is transition
    wrong_phase = replace(
        transition_command,
        target=Phase.CONTAINED,
        proof=_result(model, Phase.CONTAINMENT_STARTED, "wrong-replay-phase"),
    )
    _assert_error(
        "event-replay-phase-mismatch",
        lambda: model.transition(wrong_phase),
    )


def test_persisted_clock_receipts_verify_after_real_authority_process_restart() -> None:
    authentication_key = b"propertyquarry-test-clock-key-01"
    assert len(authentication_key) == 32
    clock = TrustedClockAuthority(
        CLOCK_ID,
        10,
        authentication_key=authentication_key,
    )
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    _step(model, clock, "containment-intent", Phase.CONTAINMENT_STARTED)

    restarted_clock = TrustedClockAuthority(
        CLOCK_ID,
        clock.now,
        authentication_key=authentication_key,
    )
    restarted = ReleaseLifecycleModel(restarted_clock, model.records)
    assert restarted.verify_chain() is True
    _step(restarted, restarted_clock, "contained-after-restart", Phase.CONTAINED)

    wrong_authority_key = TrustedClockAuthority(
        CLOCK_ID,
        restarted_clock.now,
        authentication_key=b"propertyquarry-wrong-clock-key-1",
    )
    _assert_error(
        "untrusted-clock-observation",
        lambda: ReleaseLifecycleModel(wrong_authority_key, restarted.records),
    )


def test_boolean_lease_and_cas_integers_are_rejected_not_coerced() -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 0)
    model = ReleaseLifecycleModel(clock)
    binding = _binding()
    observation = _observe(clock)
    boolean_deadline = replace(
        _lease(binding, observation, deadline=1),
        deadline=True,
    )
    command = AdmissionCommand(
        "boolean-deadline",
        _d("request:boolean-deadline"),
        binding,
        1,
        boolean_deadline,
        model.cas_successor(),
        observation,
    )
    _assert_error(
        "invalid-lease-window",
        lambda: model.start_release_run(command),
    )

    valid_lease = _lease(binding, observation)
    boolean_cas = replace(model.cas_successor(), successor_generation=True)
    _assert_error(
        "cas-shape-invalid",
        lambda: model.start_release_run(
            replace(command, event_id="boolean-cas", lease=valid_lease, cas=boolean_cas)
        ),
    )


def test_post_append_cache_fault_rebuilds_from_committed_chain(monkeypatch) -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    binding = _binding()
    observation = _observe(clock)
    command = AdmissionCommand(
        "faulted-admission",
        _d("request:faulted-admission"),
        binding,
        1,
        _lease(binding, observation, deadline=30, fence=10),
        model.cas_successor(),
        observation,
    )

    def fail_after_append(_lease_value) -> None:
        raise RuntimeError("injected derived-cache fault")

    monkeypatch.setattr(model, "_record_new_lease", fail_after_append)
    with pytest.raises(RuntimeError, match="injected derived-cache fault"):
        model.start_release_run(command)

    assert len(model.records) == 1
    assert model.start_release_run(command) is model.records[0]
    stale_recovery = _recovery(
        model,
        clock,
        event_id="stale-recovery-after-fault",
        at=30,
        fence=5,
        resource_fence=5,
    )
    _assert_error(
        "fencing-token-not-increasing",
        lambda: model.admit_reconciliation(stale_recovery),
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("predecessor_generation", 9, "cas-predecessor-generation-mismatch"),
        ("successor_generation", 9, "cas-successor-generation-mismatch"),
        ("predecessor_hash", _d("wrong-hash"), "cas-predecessor-hash-mismatch"),
        ("predecessor_state", "deployed", "cas-predecessor-state-mismatch"),
        ("predecessor_lifecycle_id", "wrong", "cas-predecessor-lifecycle-mismatch"),
        ("predecessor_epoch", 9, "cas-predecessor-epoch-mismatch"),
    ),
)
def test_exact_cas_predecessor_dimensions(field, value, code) -> None:
    clock = TrustedClockAuthority(CLOCK_ID, 10)
    model = ReleaseLifecycleModel(clock)
    _admit(model, clock)
    cas = replace(model.cas_successor(), **{field: value})
    request = _d(f"request:cas:{field}")
    proof = _intent(model, request, cas=cas)
    _assert_error(
        code,
        lambda: _step(
            model,
            clock,
            f"cas-{field}",
            Phase.CONTAINMENT_STARTED,
            cas=cas,
            request_digest=request,
            proof=proof,
        ),
    )
