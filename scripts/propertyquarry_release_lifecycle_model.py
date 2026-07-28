#!/usr/bin/env python3
"""Executable, non-authoritative PropertyQuarry release-lifecycle v2 model.

This module is an offline structural and semantic reference.  It does not
verify cryptographic signatures, grant production authority, hold credentials,
or perform effects.  The modeled GitHub boundary has exactly two operations:
read-only ``preflight`` and one installed-controller ``release-run``.  Durable
phase methods represent external-CAS transactions internal to that run.

Every persisted chain record is also its event-index entry.  Therefore event
uniqueness, replay identity, and the chain append are one modeled transaction;
the replay index is reconstructed from records after restart, never persisted
as an independent source of truth.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "propertyquarry.release-lifecycle-model.v2"
GENESIS_HASH = "sha256:" + hashlib.sha256(
    b"propertyquarry.release-lifecycle-model.v2/genesis"
).hexdigest()
GENESIS_STATE = "genesis"
MAX_INT64 = 2**63 - 1
EXTERNAL_OPERATIONS = ("release-preflight", "release-run", "reconcile-run")
WORKFLOW_OPERATIONS = ("release-preflight", "release-run")
RAW_RESOURCE_CREDENTIAL_EFFECTS_ALLOWED = False
FENCE_MEDIATOR_CAPABILITY = "reject-stale-global-and-resource-fences"


class Phase(str, Enum):
    ADMITTED = "admitted"
    CONTAINMENT_STARTED = "containment-started"
    CONTAINED = "contained"
    DEPLOY_STARTED = "deploy-started"
    DEPLOYED = "deployed"
    LIVE_VERIFICATION_STARTED = "live-verification-started"
    LIVE_VERIFIED = "live-verified"
    ACTIVATION_STARTED = "activation-started"
    ACTIVATION_VERIFIED = "activation-verified"
    OVERLAY_ACTIVATION_STARTED = "overlay-activation-started"
    OVERLAY_ACTIVATED = "overlay-activated"
    FINALIZATION_STARTED = "finalization-started"
    SEALED_FINAL = "sealed-final"
    RECONCILIATION_ADMITTED = "reconciliation-admitted"
    ROLLBACK_STARTED = "rollback-started"
    ROLLED_BACK = "rolled-back"
    CONTAINED_FAILED = "contained-failed"


class ResourceKind(str, Enum):
    DATABASE = "database"
    LAUNCH_AUTHORITY = "launch-authority"
    MONITORING_DELIVERY = "monitoring-delivery"
    OVERLAY = "overlay"
    PUBLIC_TOUR = "public-tour"
    RUNTIME = "runtime"
    TRAFFIC = "traffic"


REQUIRED_RESOURCE_KINDS = frozenset(ResourceKind)


class ResourceOutcome(str, Enum):
    UNCHANGED = "unchanged"
    FORWARD_COMPATIBLE = "forward-compatible"
    RESTORED_VERIFIED = "restored-verified"
    UNRESOLVED = "unresolved"


SUCCESS_PATH = (
    Phase.ADMITTED,
    Phase.CONTAINMENT_STARTED,
    Phase.CONTAINED,
    Phase.DEPLOY_STARTED,
    Phase.DEPLOYED,
    Phase.LIVE_VERIFICATION_STARTED,
    Phase.LIVE_VERIFIED,
    Phase.ACTIVATION_STARTED,
    Phase.ACTIVATION_VERIFIED,
    Phase.OVERLAY_ACTIVATION_STARTED,
    Phase.OVERLAY_ACTIVATED,
    Phase.FINALIZATION_STARTED,
    Phase.SEALED_FINAL,
)
FORWARD_SUCCESSOR: Mapping[Phase, Phase] = dict(zip(SUCCESS_PATH, SUCCESS_PATH[1:]))
STARTED_PHASES = frozenset(
    (
        Phase.CONTAINMENT_STARTED,
        Phase.DEPLOY_STARTED,
        Phase.LIVE_VERIFICATION_STARTED,
        Phase.ACTIVATION_STARTED,
        Phase.OVERLAY_ACTIVATION_STARTED,
        Phase.FINALIZATION_STARTED,
        Phase.ROLLBACK_STARTED,
    )
)
RESULT_TO_INTENT: Mapping[Phase, Phase] = {
    Phase.CONTAINED: Phase.CONTAINMENT_STARTED,
    Phase.DEPLOYED: Phase.DEPLOY_STARTED,
    Phase.LIVE_VERIFIED: Phase.LIVE_VERIFICATION_STARTED,
    Phase.ACTIVATION_VERIFIED: Phase.ACTIVATION_STARTED,
    Phase.OVERLAY_ACTIVATED: Phase.OVERLAY_ACTIVATION_STARTED,
    Phase.SEALED_FINAL: Phase.FINALIZATION_STARTED,
}
MANIFEST_RESULT_PHASES = tuple(RESULT_TO_INTENT)[:-1]
ROLLBACK_SOURCES = frozenset(SUCCESS_PATH[:-1]) | frozenset(
    (Phase.RECONCILIATION_ADMITTED,)
)
SAFE_ADMISSION_TERMINALS = frozenset((Phase.SEALED_FINAL, Phase.ROLLED_BACK))
ALL_TERMINALS = SAFE_ADMISSION_TERMINALS | frozenset((Phase.CONTAINED_FAILED,))
SAFE_RESOURCE_OUTCOMES = frozenset(
    (
        ResourceOutcome.UNCHANGED,
        ResourceOutcome.FORWARD_COMPATIBLE,
        ResourceOutcome.RESTORED_VERIFIED,
    )
)


class LifecycleModelError(ValueError):
    """A deterministic, machine-comparable model rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    raise LifecycleModelError(code)


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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_digest(value: str, code: str = "invalid-digest") -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        _reject(code)
    payload = value.removeprefix("sha256:")
    if len(payload) != 64 or any(character not in "0123456789abcdef" for character in payload):
        _reject(code)


@dataclass(frozen=True)
class TrustedClockObservation:
    authority_id: str
    observation_id: str
    issued_at: int
    observed_at: int
    evidence_digest: str


class TrustedClockAuthority:
    """Small authority-owned clock harness for the offline executable model.

    Callers may request the authority's current observation, but cannot select
    its value.  Tests can advance the authority explicitly to model time.
    """

    def __init__(
        self,
        authority_id: str,
        initial_time: int = 0,
        *,
        authentication_key: bytes | None = None,
    ) -> None:
        if (
            not isinstance(authority_id, str)
            or not authority_id
            or isinstance(initial_time, bool)
            or not isinstance(initial_time, int)
            or initial_time < 0
        ):
            _reject("invalid-clock-authority")
        if authentication_key is not None and (
            not isinstance(authentication_key, bytes) or len(authentication_key) < 32
        ):
            _reject("invalid-clock-authentication-key")
        self.authority_id = authority_id
        self._now = initial_time
        # A real controller verifies an external authority signature.  HMAC is
        # only the offline harness used here to make persisted observations
        # independently re-verifiable after a process restart; it does not
        # turn this reference model into production authority.
        self._authentication_key = authentication_key or secrets.token_bytes(32)

    @property
    def now(self) -> int:
        return self._now

    def advance(self, new_time: int) -> None:
        if isinstance(new_time, bool) or not isinstance(new_time, int) or new_time < self._now:
            _reject("trusted-clock-cannot-regress")
        self._now = new_time

    def observe(self) -> TrustedClockObservation:
        observation_id = f"{self.authority_id}:{secrets.token_hex(16)}"
        payload = {
            "authority_id": self.authority_id,
            "observation_id": observation_id,
            "issued_at": self._now,
            "observed_at": self._now,
        }
        observation = TrustedClockObservation(
            authority_id=self.authority_id,
            observation_id=observation_id,
            issued_at=self._now,
            observed_at=self._now,
            evidence_digest=self._authenticate(payload),
        )
        return observation

    def verify(
        self,
        observation: TrustedClockObservation,
        *,
        require_current: bool = True,
    ) -> bool:
        if (
            not isinstance(observation, TrustedClockObservation)
            or observation.authority_id != self.authority_id
            or not isinstance(observation.observation_id, str)
            or not observation.observation_id.startswith(f"{self.authority_id}:")
            or not isinstance(observation.evidence_digest, str)
            or isinstance(observation.issued_at, bool)
            or not isinstance(observation.issued_at, int)
            or isinstance(observation.observed_at, bool)
            or not isinstance(observation.observed_at, int)
            or observation.issued_at < 0
            or observation.issued_at != observation.observed_at
        ):
            return False
        payload = {
            "authority_id": observation.authority_id,
            "observation_id": observation.observation_id,
            "issued_at": observation.issued_at,
            "observed_at": observation.observed_at,
        }
        if not hmac.compare_digest(
            observation.evidence_digest, self._authenticate(payload)
        ):
            return False
        return not require_current or observation.observed_at == self._now

    def _authenticate(self, payload: Mapping[str, Any]) -> str:
        return "sha256:" + hmac.new(
            self._authentication_key,
            _canonical_bytes(payload),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True)
class LeasePolicy:
    trusted_clock_authority_id: str
    max_lease_ttl: int
    max_renewal_horizon: int


@dataclass(frozen=True)
class ResourceContract:
    resource_id: str
    kind: ResourceKind
    mediator_digest: str
    fence_capability_digest: str
    fence_capability: str = FENCE_MEDIATOR_CAPABILITY
    fence_enforcing: bool = True
    forward_compatible_allowed: bool = False


@dataclass(frozen=True)
class LifecycleBinding:
    """Release, policy, controller, and resource identities fixed per epoch."""

    lifecycle_id: str
    release_sha: str
    controller_digest: str
    policy_digest: str
    lease_policy: LeasePolicy
    resources: tuple[ResourceContract, ...]

    @property
    def resource_set(self) -> tuple[str, ...]:
        return tuple(resource.resource_id for resource in self.resources)

    @classmethod
    def build(
        cls,
        *,
        lifecycle_id: str,
        release_sha: str,
        controller_digest: str,
        policy_digest: str,
        lease_policy: LeasePolicy,
        resources: Iterable[ResourceContract],
    ) -> "LifecycleBinding":
        return cls(
            lifecycle_id=lifecycle_id,
            release_sha=release_sha,
            controller_digest=controller_digest,
            policy_digest=policy_digest,
            lease_policy=lease_policy,
            resources=tuple(sorted(resources, key=lambda item: item.resource_id)),
        )


@dataclass(frozen=True)
class Lease:
    lease_id: str
    holder_id: str
    time_authority_id: str
    issued_at: int
    renewal_origin: int
    deadline: int
    fencing_token: int
    resource_fencing: tuple[tuple[str, int], ...]

    @classmethod
    def build(
        cls,
        *,
        lease_id: str,
        holder_id: str,
        time_authority_id: str,
        issued_at: int,
        renewal_origin: int | None = None,
        deadline: int,
        fencing_token: int,
        resource_fencing: Mapping[str, int],
    ) -> "Lease":
        return cls(
            lease_id=lease_id,
            holder_id=holder_id,
            time_authority_id=time_authority_id,
            issued_at=issued_at,
            renewal_origin=issued_at if renewal_origin is None else renewal_origin,
            deadline=deadline,
            fencing_token=fencing_token,
            resource_fencing=tuple(sorted(resource_fencing.items())),
        )


@dataclass(frozen=True)
class RecoveryAuthority:
    authority_id: str
    authority_digest: str
    reason_digest: str


@dataclass(frozen=True)
class CasSuccessor:
    predecessor_generation: int
    predecessor_hash: str
    predecessor_state: str
    predecessor_lifecycle_id: str | None
    predecessor_epoch: int
    successor_generation: int


@dataclass(frozen=True)
class IntentBinding:
    plan_digest: str
    input_digest: str
    effect_digest: str
    idempotency_digest: str
    expected_version_digest: str
    global_fence_digest: str
    resource_fence_digest: str


@dataclass(frozen=True)
class ResultEvidence:
    intent_record_hash: str
    result_digest: str
    evidence_digest: str
    persisted_ack_digest: str
    fsynced_ack_digest: str


@dataclass(frozen=True)
class EvidenceManifestEntry:
    phase: Phase
    record_hash: str
    evidence_digest: str
    persisted_ack_digest: str
    fsynced_ack_digest: str


@dataclass(frozen=True)
class FinalEvidence:
    intent_record_hash: str
    result_digest: str
    evidence_digest: str
    persisted_ack_digest: str
    fsynced_ack_digest: str
    ordered_manifest: tuple[EvidenceManifestEntry, ...]
    manifest_root: str


@dataclass(frozen=True)
class ResourceOutcomeEvidence:
    resource_id: str
    outcome: ResourceOutcome
    evidence_digest: str
    mediator_receipt_digest: str
    backup_digest: str | None = None
    schema_digest: str | None = None
    wal_or_lsn_digest: str | None = None
    checksum_digest: str | None = None
    probe_digest: str | None = None


PhaseProof = IntentBinding | ResultEvidence | FinalEvidence


@dataclass(frozen=True)
class PreflightRequest:
    request_transport_digest: str
    binding: LifecycleBinding
    epoch: int
    lease: Lease
    cas: CasSuccessor
    clock: TrustedClockObservation


@dataclass(frozen=True)
class PreflightResult:
    admissible: bool
    authoritative: bool
    next_external_operation: str
    lifecycle_id: str
    epoch: int
    predecessor_hash: str
    successor_generation: int
    binding_digest: str


@dataclass(frozen=True)
class AdmissionCommand:
    event_id: str
    request_transport_digest: str
    binding: LifecycleBinding
    epoch: int
    lease: Lease
    cas: CasSuccessor
    clock: TrustedClockObservation


@dataclass(frozen=True)
class TransitionCommand:
    event_id: str
    request_transport_digest: str
    target: Phase
    binding: LifecycleBinding
    lease: Lease
    cas: CasSuccessor
    clock: TrustedClockObservation
    proof: PhaseProof


@dataclass(frozen=True)
class LeaseRenewalCommand:
    event_id: str
    request_transport_digest: str
    binding: LifecycleBinding
    lease: Lease
    new_deadline: int
    cas: CasSuccessor
    clock: TrustedClockObservation


@dataclass(frozen=True)
class ReconciliationAdmissionCommand:
    event_id: str
    request_transport_digest: str
    binding: LifecycleBinding
    recovery_authority: RecoveryAuthority
    recovery_lease: Lease
    cas: CasSuccessor
    clock: TrustedClockObservation


@dataclass(frozen=True)
class RollbackCompletionCommand:
    event_id: str
    request_transport_digest: str
    binding: LifecycleBinding
    lease: Lease
    resource_outcomes: tuple[ResourceOutcomeEvidence, ...]
    evidence: ResultEvidence
    cas: CasSuccessor
    clock: TrustedClockObservation


@dataclass(frozen=True)
class LifecycleRecord:
    schema: str
    generation: int
    record_hash: str
    predecessor_hash: str
    event_id: str
    request_transport_digest: str
    event_kind: str
    epoch: int
    binding: LifecycleBinding
    lease: Lease
    phase: Phase
    state: str
    clock: TrustedClockObservation
    proof: PhaseProof | None
    recovery_authority: RecoveryAuthority | None
    reconciliation_from_phase: Phase | None
    resource_outcomes: tuple[ResourceOutcomeEvidence, ...]

    @property
    def observed_at(self) -> int:
        return self.clock.observed_at


def expected_version_digest(cas: CasSuccessor) -> str:
    return digest({"cas_successor": cas})


def global_fence_digest(lease: Lease) -> str:
    return digest(
        {
            "lease_id": lease.lease_id,
            "holder_id": lease.holder_id,
            "fencing_token": lease.fencing_token,
        }
    )


def resource_fence_digest(binding: LifecycleBinding, lease: Lease) -> str:
    tokens = dict(lease.resource_fencing)
    return digest(
        {
            "resources": [
                {
                    "resource_id": resource.resource_id,
                    "kind": resource.kind,
                    "mediator_digest": resource.mediator_digest,
                    "fence_capability_digest": resource.fence_capability_digest,
                    "fence_capability": resource.fence_capability,
                    "fencing_token": tokens.get(resource.resource_id),
                }
                for resource in binding.resources
            ]
        }
    )


def evidence_manifest_entries(
    records: Sequence[LifecycleRecord], epoch: int
) -> tuple[EvidenceManifestEntry, ...]:
    entries: list[EvidenceManifestEntry] = []
    for record in records:
        if (
            record.epoch != epoch
            or record.phase not in MANIFEST_RESULT_PHASES
            or record.event_kind != "phase-transition"
        ):
            continue
        if not isinstance(record.proof, ResultEvidence):
            _reject("manifest-result-evidence-missing")
        entries.append(
            EvidenceManifestEntry(
                phase=record.phase,
                record_hash=record.record_hash,
                evidence_digest=record.proof.evidence_digest,
                persisted_ack_digest=record.proof.persisted_ack_digest,
                fsynced_ack_digest=record.proof.fsynced_ack_digest,
            )
        )
    return tuple(entries)


def final_manifest_root(evidence: FinalEvidence) -> str:
    return digest(
        {
            "ordered_manifest": evidence.ordered_manifest,
            "finalization_intent_record_hash": evidence.intent_record_hash,
            "final_result_digest": evidence.result_digest,
            "final_evidence_digest": evidence.evidence_digest,
            "persisted_ack_digest": evidence.persisted_ack_digest,
            "fsynced_ack_digest": evidence.fsynced_ack_digest,
        }
    )


class ReleaseLifecycleModel:
    """Append-only global lineage with controller-internal phase epochs."""

    def __init__(
        self,
        clock_authority: TrustedClockAuthority,
        records: Iterable[LifecycleRecord] = (),
    ) -> None:
        self._clock_authority = clock_authority
        self._records = list(records)
        self._events: dict[str, tuple[str, LifecycleRecord]] = {}
        self._used_lifecycle_ids: set[str] = set()
        self._used_lease_ids: set[str] = set()
        self._highest_fencing_token = -1
        self._resource_fencing: dict[str, int] = {}
        self._last_observed_at: int | None = None
        if self._records:
            self.verify_chain()
            self._rebuild_indexes()

    @property
    def records(self) -> tuple[LifecycleRecord, ...]:
        return tuple(self._records)

    @property
    def head(self) -> LifecycleRecord | None:
        return self._records[-1] if self._records else None

    @property
    def binding(self) -> LifecycleBinding | None:
        return self.head.binding if self.head else None

    @property
    def lease(self) -> Lease | None:
        return self.head.lease if self.head else None

    @property
    def phase(self) -> Phase | None:
        return self.head.phase if self.head else None

    def cas_successor(self) -> CasSuccessor:
        head = self.head
        if head is None:
            return CasSuccessor(0, GENESIS_HASH, GENESIS_STATE, None, 0, 1)
        return CasSuccessor(
            predecessor_generation=head.generation,
            predecessor_hash=head.record_hash,
            predecessor_state=head.state,
            predecessor_lifecycle_id=head.binding.lifecycle_id,
            predecessor_epoch=head.epoch,
            successor_generation=head.generation + 1,
        )

    def snapshot(self) -> tuple[Any, ...]:
        return (
            self.records,
            tuple(sorted(self._events)),
            tuple(sorted(self._used_lifecycle_ids)),
            tuple(sorted(self._used_lease_ids)),
            self._highest_fencing_token,
            tuple(sorted(self._resource_fencing.items())),
            self._last_observed_at,
        )

    def preflight(self, request: PreflightRequest) -> PreflightResult:
        _validate_digest(request.request_transport_digest, "invalid-request-transport-digest")
        self._validate_normal_admission(
            request.binding, request.epoch, request.lease, request.cas, request.clock
        )
        return PreflightResult(
            admissible=True,
            authoritative=False,
            next_external_operation="release-run",
            lifecycle_id=request.binding.lifecycle_id,
            epoch=request.epoch,
            predecessor_hash=request.cas.predecessor_hash,
            successor_generation=request.cas.successor_generation,
            binding_digest=digest(request.binding),
        )

    def start_release_run(self, command: AdmissionCommand) -> LifecycleRecord:
        replay = self._replay(
            command,
            expected_kind="normal-admission",
            expected_phase=Phase.ADMITTED,
        )
        if replay is not None:
            return replay
        self._validate_normal_admission(
            command.binding,
            command.epoch,
            command.lease,
            command.cas,
            command.clock,
        )
        return self._append(
            command.event_id,
            command.request_transport_digest,
            "normal-admission",
            command.epoch,
            command.binding,
            command.lease,
            Phase.ADMITTED,
            Phase.ADMITTED.value,
            command.clock,
            None,
            None,
            None,
            (),
        )

    def transition(self, command: TransitionCommand) -> LifecycleRecord:
        replay = self._replay(
            command,
            expected_kind="phase-transition",
            expected_phase=command.target,
        )
        if replay is not None:
            return replay
        head = self._require_head()
        self._assert_cas(command.cas)
        self._assert_context(command.binding, command.lease)
        self._validate_clock(command.clock, command.binding)
        if head.phase is Phase.SEALED_FINAL and command.target is Phase.ROLLBACK_STARTED:
            _reject("sealed-final-rollback-forbidden")
        if head.phase in ALL_TERMINALS:
            _reject("terminal-lifecycle")
        self._assert_lease_active(command.lease, command.clock.observed_at)
        expected = FORWARD_SUCCESSOR.get(head.phase)
        if command.target is not expected and not (
            command.target is Phase.ROLLBACK_STARTED and head.phase in ROLLBACK_SOURCES
        ):
            _reject("transition-not-allowed")
        self._validate_phase_proof(
            command.target,
            command.proof,
            command.request_transport_digest,
            command.cas,
            command.binding,
            command.lease,
            self._records,
            head.epoch,
        )
        return self._append(
            command.event_id,
            command.request_transport_digest,
            "phase-transition",
            head.epoch,
            head.binding,
            head.lease,
            command.target,
            command.target.value,
            command.clock,
            command.proof,
            head.recovery_authority,
            head.reconciliation_from_phase,
            head.resource_outcomes,
        )

    def renew_lease(self, command: LeaseRenewalCommand) -> LifecycleRecord:
        replay = self._replay(
            command,
            expected_kind="lease-renewed",
        )
        if replay is not None:
            return replay
        head = self._require_head()
        self._assert_cas(command.cas)
        self._assert_context(command.binding, command.lease)
        self._validate_clock(command.clock, command.binding)
        if head.phase in ALL_TERMINALS:
            _reject("terminal-lifecycle")
        now = command.clock.observed_at
        self._assert_lease_active(command.lease, now)
        if command.new_deadline <= command.lease.deadline:
            _reject("lease-renewal-not-increasing")
        renewed = dataclasses.replace(
            command.lease, issued_at=now, deadline=command.new_deadline
        )
        self._validate_lease_window(renewed, command.binding.lease_policy, now)
        return self._append(
            command.event_id,
            command.request_transport_digest,
            "lease-renewed",
            head.epoch,
            head.binding,
            renewed,
            head.phase,
            f"lease-renewed:{head.phase.value}",
            command.clock,
            head.proof,
            head.recovery_authority,
            head.reconciliation_from_phase,
            head.resource_outcomes,
        )

    def admit_reconciliation(
        self, command: ReconciliationAdmissionCommand
    ) -> LifecycleRecord:
        replay = self._replay(
            command,
            expected_kind="reconciliation-admission",
            expected_phase=Phase.RECONCILIATION_ADMITTED,
        )
        if replay is not None:
            return replay
        head = self._require_head()
        self._assert_cas(command.cas)
        self._assert_binding(command.binding)
        self._validate_clock(command.clock, command.binding)
        now = command.clock.observed_at
        if head.phase in SAFE_ADMISSION_TERMINALS:
            _reject("reconciliation-not-allowed")
        if now < head.lease.deadline:
            _reject("reconciliation-before-lease-expiry")
        self._validate_recovery_authority(command.recovery_authority)
        self._validate_new_lease(command.recovery_lease, command.binding, command.clock)
        if command.recovery_lease.lease_id in self._used_lease_ids:
            _reject("lease-id-reused")
        if command.recovery_lease.holder_id == head.lease.holder_id:
            _reject("recovery-holder-not-new")
        if command.recovery_lease.holder_id != command.recovery_authority.authority_id:
            _reject("recovery-holder-authority-mismatch")
        self._assert_new_fences(command.recovery_lease)
        return self._append(
            command.event_id,
            command.request_transport_digest,
            "reconciliation-admission",
            head.epoch,
            head.binding,
            command.recovery_lease,
            Phase.RECONCILIATION_ADMITTED,
            Phase.RECONCILIATION_ADMITTED.value,
            command.clock,
            None,
            command.recovery_authority,
            head.phase,
            head.resource_outcomes,
        )

    def complete_rollback(
        self, command: RollbackCompletionCommand
    ) -> LifecycleRecord:
        replay = self._replay(
            command,
            expected_kind="rollback-result",
        )
        if replay is not None:
            return replay
        head = self._require_head()
        self._assert_cas(command.cas)
        self._assert_context(command.binding, command.lease)
        self._validate_clock(command.clock, command.binding)
        if head.phase is not Phase.ROLLBACK_STARTED:
            _reject("rollback-not-started")
        self._assert_lease_active(command.lease, command.clock.observed_at)
        self._validate_result_evidence(
            command.evidence, Phase.ROLLBACK_STARTED, self._records, head.epoch
        )
        outcomes = self._validate_resource_outcomes(
            command.resource_outcomes, command.binding
        )
        target = (
            Phase.ROLLED_BACK
            if all(item.outcome in SAFE_RESOURCE_OUTCOMES for item in outcomes)
            else Phase.CONTAINED_FAILED
        )
        return self._append(
            command.event_id,
            command.request_transport_digest,
            "rollback-result",
            head.epoch,
            head.binding,
            head.lease,
            target,
            target.value,
            command.clock,
            command.evidence,
            head.recovery_authority,
            head.reconciliation_from_phase,
            outcomes,
        )

    def verify_chain(self) -> bool:
        """Reconstruct and semantically validate the persisted lineage."""

        seen_events: dict[str, str] = {}
        used_lifecycles: set[str] = set()
        used_leases: set[str] = set()
        high_global = -1
        high_resources: dict[str, int] = {}
        prior: LifecycleRecord | None = None
        history: list[LifecycleRecord] = []
        last_time: int | None = None

        for record in self._records:
            if (
                isinstance(record.generation, bool)
                or not isinstance(record.generation, int)
                or record.generation < 1
                or record.generation > MAX_INT64
                or isinstance(record.epoch, bool)
                or not isinstance(record.epoch, int)
                or record.epoch < 1
                or record.epoch > MAX_INT64
                or not isinstance(record.event_id, str)
                or not record.event_id.strip()
                or not isinstance(record.event_kind, str)
                or not isinstance(record.state, str)
            ):
                _reject("chain-record-shape-invalid")
            if record.event_id in seen_events:
                if seen_events[record.event_id] != record.request_transport_digest:
                    _reject("persisted-event-id-conflict")
                _reject("persisted-event-id-duplicate")
            seen_events[record.event_id] = record.request_transport_digest
            if record.schema != SCHEMA:
                _reject("chain-schema-invalid")
            _validate_digest(
                record.request_transport_digest, "invalid-request-transport-digest"
            )
            expected_generation = 1 if prior is None else prior.generation + 1
            expected_predecessor = GENESIS_HASH if prior is None else prior.record_hash
            if record.generation != expected_generation:
                _reject("chain-generation-invalid")
            if record.predecessor_hash != expected_predecessor:
                _reject("chain-predecessor-invalid")
            if record.record_hash != digest(self._record_payload(record)):
                _reject("chain-hash-invalid")
            self._validate_clock(
                record.clock,
                record.binding,
                last_time=last_time,
                use_model_baseline=False,
            )
            last_time = record.observed_at

            if record.event_kind == "normal-admission":
                self._audit_normal_admission(record, prior, used_lifecycles, used_leases, high_global, high_resources)
                used_lifecycles.add(record.binding.lifecycle_id)
                used_leases.add(record.lease.lease_id)
                high_global = record.lease.fencing_token
                high_resources.update(dict(record.lease.resource_fencing))
            elif prior is None:
                _reject("chain-missing-admission")
            elif record.event_kind == "phase-transition":
                self._audit_phase_transition(record, prior, history)
            elif record.event_kind == "lease-renewed":
                self._audit_lease_renewal(record, prior)
            elif record.event_kind == "reconciliation-admission":
                self._audit_reconciliation(record, prior, used_leases, high_global, high_resources)
                used_leases.add(record.lease.lease_id)
                high_global = record.lease.fencing_token
                high_resources.update(dict(record.lease.resource_fencing))
            elif record.event_kind == "rollback-result":
                self._audit_rollback_result(record, prior, history)
            else:
                _reject("chain-event-kind-invalid")
            history.append(record)
            prior = record
        return True

    def _audit_normal_admission(
        self,
        record: LifecycleRecord,
        prior: LifecycleRecord | None,
        used_lifecycles: set[str],
        used_leases: set[str],
        high_global: int,
        high_resources: Mapping[str, int],
    ) -> None:
        if record.phase is not Phase.ADMITTED or record.state != Phase.ADMITTED.value:
            _reject("chain-admission-state-invalid")
        if (
            record.proof is not None
            or record.resource_outcomes
            or record.recovery_authority is not None
            or record.reconciliation_from_phase is not None
        ):
            _reject("chain-admission-payload-invalid")
        self._validate_binding_shape(record.binding)
        self._validate_new_lease(record.lease, record.binding, record.clock)
        if prior is None:
            expected_epoch = 1
        else:
            if prior.phase not in SAFE_ADMISSION_TERMINALS:
                _reject("chain-prior-not-terminal-safe")
            if prior.phase is Phase.ROLLED_BACK:
                self._validate_safe_terminal_outcomes(prior)
            expected_epoch = prior.epoch + 1
        if record.epoch != expected_epoch:
            _reject("chain-epoch-invalid")
        if record.binding.lifecycle_id in used_lifecycles:
            _reject("chain-lifecycle-id-reused")
        if record.lease.lease_id in used_leases:
            _reject("chain-lease-id-reused")
        if record.lease.holder_id != record.binding.controller_digest:
            _reject("chain-holder-controller-mismatch")
        self._audit_fences(record.lease, high_global, high_resources)

    def _audit_phase_transition(
        self,
        record: LifecycleRecord,
        prior: LifecycleRecord,
        history: Sequence[LifecycleRecord],
    ) -> None:
        self._audit_same_lifecycle(record, prior)
        if record.state != record.phase.value:
            _reject("chain-phase-state-invalid")
        if record.lease != prior.lease:
            _reject("chain-phase-lease-changed")
        if record.resource_outcomes != prior.resource_outcomes:
            _reject("chain-phase-resource-outcomes-changed")
        self._assert_lease_active(record.lease, record.observed_at)
        expected = FORWARD_SUCCESSOR.get(prior.phase)
        if record.phase is not expected and not (
            record.phase is Phase.ROLLBACK_STARTED and prior.phase in ROLLBACK_SOURCES
        ):
            _reject("chain-transition-invalid")
        cas = self._cas_for_prior(prior)
        self._validate_phase_proof(
            record.phase,
            record.proof,
            record.request_transport_digest,
            cas,
            record.binding,
            record.lease,
            history,
            record.epoch,
        )

    def _audit_lease_renewal(
        self, record: LifecycleRecord, prior: LifecycleRecord
    ) -> None:
        self._audit_same_lifecycle(record, prior)
        if record.phase is not prior.phase or record.state != f"lease-renewed:{prior.phase.value}":
            _reject("chain-renewal-state-invalid")
        if (
            record.proof != prior.proof
            or record.resource_outcomes != prior.resource_outcomes
        ):
            _reject("chain-renewal-payload-changed")
        old = prior.lease
        new = record.lease
        if (
            new.lease_id != old.lease_id
            or new.holder_id != old.holder_id
            or new.time_authority_id != old.time_authority_id
            or new.renewal_origin != old.renewal_origin
            or new.fencing_token != old.fencing_token
            or new.resource_fencing != old.resource_fencing
        ):
            _reject("chain-renewal-identity-changed")
        if new.issued_at != record.observed_at or new.deadline <= old.deadline:
            _reject("chain-renewal-window-invalid")
        if record.observed_at >= old.deadline:
            _reject("chain-renewal-after-expiry")
        self._validate_lease_window(new, record.binding.lease_policy, record.observed_at)

    def _audit_reconciliation(
        self,
        record: LifecycleRecord,
        prior: LifecycleRecord,
        used_leases: set[str],
        high_global: int,
        high_resources: Mapping[str, int],
    ) -> None:
        self._audit_same_binding(record, prior)
        if prior.phase in SAFE_ADMISSION_TERMINALS:
            _reject("chain-reconciliation-not-allowed")
        if record.observed_at < prior.lease.deadline:
            _reject("chain-reconciliation-before-expiry")
        if (
            record.phase is not Phase.RECONCILIATION_ADMITTED
            or record.state != Phase.RECONCILIATION_ADMITTED.value
        ):
            _reject("chain-reconciliation-state-invalid")
        if record.reconciliation_from_phase is not prior.phase:
            _reject("chain-reconciliation-source-invalid")
        if record.recovery_authority is None:
            _reject("chain-recovery-authority-missing")
        if record.proof is not None or record.resource_outcomes != prior.resource_outcomes:
            _reject("chain-reconciliation-payload-invalid")
        self._validate_recovery_authority(record.recovery_authority)
        self._validate_new_lease(record.lease, record.binding, record.clock)
        if record.lease.lease_id in used_leases:
            _reject("chain-recovery-lease-reused")
        if record.lease.holder_id == prior.lease.holder_id:
            _reject("chain-recovery-holder-not-new")
        if record.lease.holder_id != record.recovery_authority.authority_id:
            _reject("chain-recovery-authority-mismatch")
        self._audit_fences(record.lease, high_global, high_resources)

    def _audit_rollback_result(
        self,
        record: LifecycleRecord,
        prior: LifecycleRecord,
        history: Sequence[LifecycleRecord],
    ) -> None:
        self._audit_same_lifecycle(record, prior)
        if prior.phase is not Phase.ROLLBACK_STARTED:
            _reject("chain-rollback-without-intent")
        if record.lease != prior.lease:
            _reject("chain-rollback-lease-changed")
        self._assert_lease_active(record.lease, record.observed_at)
        if not isinstance(record.proof, ResultEvidence):
            _reject("chain-rollback-evidence-missing")
        self._validate_result_evidence(
            record.proof, Phase.ROLLBACK_STARTED, history, record.epoch
        )
        outcomes = self._validate_resource_outcomes(
            record.resource_outcomes, record.binding
        )
        expected = (
            Phase.ROLLED_BACK
            if all(item.outcome in SAFE_RESOURCE_OUTCOMES for item in outcomes)
            else Phase.CONTAINED_FAILED
        )
        if record.phase is not expected or record.state != expected.value:
            _reject("chain-rollback-terminal-invalid")

    def _audit_same_binding(
        self, record: LifecycleRecord, prior: LifecycleRecord
    ) -> None:
        if record.binding != prior.binding or record.epoch != prior.epoch:
            _reject("chain-lifecycle-binding-changed")

    def _audit_same_lifecycle(
        self, record: LifecycleRecord, prior: LifecycleRecord
    ) -> None:
        self._audit_same_binding(record, prior)
        if (
            record.recovery_authority != prior.recovery_authority
            or record.reconciliation_from_phase != prior.reconciliation_from_phase
        ):
            _reject("chain-recovery-binding-changed")

    @staticmethod
    def _audit_fences(
        lease: Lease, high_global: int, high_resources: Mapping[str, int]
    ) -> None:
        if lease.fencing_token <= high_global:
            _reject("chain-global-fence-stale")
        for resource, token in lease.resource_fencing:
            if token <= high_resources.get(resource, -1):
                _reject("chain-resource-fence-stale")

    def _validate_normal_admission(
        self,
        binding: LifecycleBinding,
        epoch: int,
        lease: Lease,
        cas: CasSuccessor,
        clock: TrustedClockObservation,
    ) -> None:
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
            or epoch > MAX_INT64
        ):
            _reject("invalid-epoch")
        self._validate_binding_shape(binding)
        self._validate_clock(clock, binding)
        self._assert_cas(cas)
        self._validate_new_lease(lease, binding, clock)
        head = self.head
        if head is None:
            expected_epoch = 1
        else:
            if head.phase not in SAFE_ADMISSION_TERMINALS:
                _reject("prior-lifecycle-not-terminal-safe")
            if head.phase is Phase.ROLLED_BACK:
                self._validate_safe_terminal_outcomes(head)
            expected_epoch = head.epoch + 1
        if epoch != expected_epoch:
            _reject("epoch-not-successor")
        if binding.lifecycle_id in self._used_lifecycle_ids:
            _reject("lifecycle-id-reused")
        if lease.lease_id in self._used_lease_ids:
            _reject("lease-id-reused")
        if lease.holder_id != binding.controller_digest:
            _reject("lease-holder-controller-mismatch")
        self._assert_new_fences(lease)

    def _validate_binding_shape(self, binding: LifecycleBinding) -> None:
        for value in (
            binding.lifecycle_id,
            binding.release_sha,
            binding.controller_digest,
            binding.policy_digest,
        ):
            if not isinstance(value, str) or not value.strip():
                _reject("invalid-binding")
        policy = binding.lease_policy
        if (
            not policy.trusted_clock_authority_id
            or not isinstance(policy.trusted_clock_authority_id, str)
            or isinstance(policy.max_lease_ttl, bool)
            or not isinstance(policy.max_lease_ttl, int)
            or policy.max_lease_ttl <= 0
            or policy.max_lease_ttl > MAX_INT64
            or isinstance(policy.max_renewal_horizon, bool)
            or not isinstance(policy.max_renewal_horizon, int)
            or policy.max_renewal_horizon < policy.max_lease_ttl
            or policy.max_renewal_horizon > MAX_INT64
        ):
            _reject("invalid-lease-policy")
        if not binding.resources:
            _reject("invalid-resource-set")
        names = binding.resource_set
        if names != tuple(sorted(set(names))):
            _reject("invalid-resource-set")
        kinds = tuple(resource.kind for resource in binding.resources)
        if len(kinds) != len(REQUIRED_RESOURCE_KINDS) or set(kinds) != REQUIRED_RESOURCE_KINDS:
            _reject("required-resource-domain-incomplete")
        for resource in binding.resources:
            if (
                not isinstance(resource.resource_id, str)
                or not resource.resource_id
                or not isinstance(resource.kind, ResourceKind)
            ):
                _reject("invalid-resource-contract")
            _validate_digest(resource.mediator_digest, "invalid-resource-mediator")
            _validate_digest(
                resource.fence_capability_digest, "invalid-resource-fence-capability"
            )
            if resource.fence_capability != FENCE_MEDIATOR_CAPABILITY:
                _reject("resource-fence-capability-invalid")
            if resource.fence_capability_digest != digest(
                {"capability": FENCE_MEDIATOR_CAPABILITY}
            ):
                _reject("resource-fence-capability-digest-mismatch")
            if resource.fence_enforcing is not True:
                _reject("resource-mediator-not-fence-enforcing")
            if resource.forward_compatible_allowed and resource.kind is not ResourceKind.DATABASE:
                _reject("forward-compatible-policy-invalid")

    def _validate_clock(
        self,
        clock: TrustedClockObservation,
        binding: LifecycleBinding,
        *,
        last_time: int | None = None,
        use_model_baseline: bool = True,
    ) -> None:
        if clock.authority_id != binding.lease_policy.trusted_clock_authority_id:
            _reject("clock-authority-mismatch")
        if clock.authority_id != self._clock_authority.authority_id:
            _reject("clock-authority-mismatch")
        if not self._clock_authority.verify(
            clock, require_current=use_model_baseline
        ):
            _reject("untrusted-clock-observation")
        _validate_digest(clock.evidence_digest, "invalid-clock-evidence")
        if clock.issued_at != clock.observed_at:
            _reject("clock-observation-not-current")
        baseline = self._last_observed_at if use_model_baseline else last_time
        if baseline is not None and clock.observed_at < baseline:
            _reject("trusted-time-regressed")

    def _validate_new_lease(
        self,
        lease: Lease,
        binding: LifecycleBinding,
        clock: TrustedClockObservation,
    ) -> None:
        if (
            not isinstance(lease.lease_id, str)
            or not lease.lease_id
            or not isinstance(lease.holder_id, str)
            or not lease.holder_id
            or not isinstance(lease.time_authority_id, str)
        ):
            _reject("invalid-lease")
        if lease.time_authority_id != binding.lease_policy.trusted_clock_authority_id:
            _reject("lease-time-authority-mismatch")
        if lease.issued_at != clock.observed_at or lease.renewal_origin != lease.issued_at:
            _reject("lease-issued-at-mismatch")
        self._validate_lease_tokens(lease, binding)
        self._validate_lease_window(lease, binding.lease_policy, clock.observed_at)

    def _validate_lease_tokens(
        self, lease: Lease, binding: LifecycleBinding
    ) -> None:
        if (
            isinstance(lease.fencing_token, bool)
            or not isinstance(lease.fencing_token, int)
            or lease.fencing_token < 0
            or lease.fencing_token > MAX_INT64
        ):
            _reject("invalid-lease")
        names = tuple(name for name, _ in lease.resource_fencing)
        if names != binding.resource_set:
            _reject("resource-fencing-set-mismatch")
        if any(
            isinstance(token, bool)
            or not isinstance(token, int)
            or token < 0
            or token > MAX_INT64
            for _, token in lease.resource_fencing
        ):
            _reject("invalid-resource-fencing")

    @staticmethod
    def _validate_lease_window(
        lease: Lease, policy: LeasePolicy, now: int
    ) -> None:
        for value in (lease.issued_at, lease.renewal_origin, lease.deadline, now):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > MAX_INT64
            ):
                _reject("invalid-lease-window")
        if lease.deadline <= now:
            _reject("lease-expired")
        if lease.issued_at != now:
            _reject("lease-issued-at-mismatch")
        if lease.deadline - lease.issued_at > policy.max_lease_ttl:
            _reject("lease-max-ttl-exceeded")
        if lease.deadline - lease.renewal_origin > policy.max_renewal_horizon:
            _reject("lease-renewal-horizon-exceeded")

    def _validate_recovery_authority(self, authority: RecoveryAuthority) -> None:
        if not isinstance(authority.authority_id, str) or not authority.authority_id:
            _reject("invalid-recovery-authority")
        _validate_digest(authority.authority_digest, "invalid-recovery-authority")
        _validate_digest(authority.reason_digest, "invalid-recovery-reason")

    def _validate_phase_proof(
        self,
        target: Phase,
        proof: PhaseProof | None,
        request_digest: str,
        cas: CasSuccessor,
        binding: LifecycleBinding,
        lease: Lease,
        history: Sequence[LifecycleRecord],
        epoch: int,
    ) -> None:
        if target in STARTED_PHASES:
            if not isinstance(proof, IntentBinding):
                _reject("intent-binding-required")
            for value in (
                proof.plan_digest,
                proof.input_digest,
                proof.effect_digest,
                proof.idempotency_digest,
                proof.expected_version_digest,
                proof.global_fence_digest,
                proof.resource_fence_digest,
            ):
                _validate_digest(value, "invalid-intent-binding")
            if proof.idempotency_digest != request_digest:
                _reject("intent-idempotency-binding-mismatch")
            if proof.expected_version_digest != expected_version_digest(cas):
                _reject("intent-expected-version-mismatch")
            if proof.global_fence_digest != global_fence_digest(lease):
                _reject("intent-global-fence-mismatch")
            if proof.resource_fence_digest != resource_fence_digest(binding, lease):
                _reject("intent-resource-fence-mismatch")
            return
        expected_intent = RESULT_TO_INTENT.get(target)
        if expected_intent is None:
            _reject("phase-proof-target-invalid")
        if target is Phase.SEALED_FINAL:
            if not isinstance(proof, FinalEvidence):
                _reject("final-evidence-required")
            self._validate_result_evidence(proof, expected_intent, history, epoch)
            expected_entries = evidence_manifest_entries(history, epoch)
            if proof.ordered_manifest != expected_entries:
                _reject("final-manifest-incomplete-or-unordered")
            if proof.manifest_root != final_manifest_root(proof):
                _reject("final-manifest-root-mismatch")
        else:
            if not isinstance(proof, ResultEvidence):
                _reject("result-evidence-required")
            self._validate_result_evidence(proof, expected_intent, history, epoch)

    def _validate_result_evidence(
        self,
        proof: ResultEvidence | FinalEvidence,
        expected_intent: Phase,
        history: Sequence[LifecycleRecord],
        epoch: int,
    ) -> None:
        for value in (
            proof.intent_record_hash,
            proof.result_digest,
            proof.evidence_digest,
            proof.persisted_ack_digest,
            proof.fsynced_ack_digest,
        ):
            _validate_digest(value, "invalid-result-evidence")
        intent = self._latest_phase_record(history, epoch, expected_intent)
        if intent is None or proof.intent_record_hash != intent.record_hash:
            _reject("result-intent-binding-mismatch")

    def _validate_resource_outcomes(
        self,
        outcomes: tuple[ResourceOutcomeEvidence, ...],
        binding: LifecycleBinding,
    ) -> tuple[ResourceOutcomeEvidence, ...]:
        names = tuple(item.resource_id for item in outcomes)
        if names != binding.resource_set:
            _reject("resource-outcomes-incomplete")
        by_name = {resource.resource_id: resource for resource in binding.resources}
        for item in outcomes:
            if not isinstance(item.outcome, ResourceOutcome):
                _reject("invalid-resource-outcome")
            _validate_digest(item.evidence_digest, "resource-outcome-evidence-missing")
            _validate_digest(
                item.mediator_receipt_digest, "resource-mediator-receipt-missing"
            )
            contract = by_name[item.resource_id]
            if item.outcome is ResourceOutcome.FORWARD_COMPATIBLE:
                if (
                    contract.kind is not ResourceKind.DATABASE
                    or not contract.forward_compatible_allowed
                ):
                    _reject("forward-compatible-outcome-not-allowed")
                for value in (item.schema_digest, item.checksum_digest, item.probe_digest):
                    if value is None:
                        _reject("forward-compatible-proof-incomplete")
                    _validate_digest(value, "forward-compatible-proof-incomplete")
            elif item.outcome is ResourceOutcome.RESTORED_VERIFIED:
                if contract.kind is ResourceKind.DATABASE:
                    for value in (
                        item.backup_digest,
                        item.schema_digest,
                        item.wal_or_lsn_digest,
                        item.checksum_digest,
                        item.probe_digest,
                    ):
                        if value is None:
                            _reject("database-restore-proof-incomplete")
                        _validate_digest(value, "database-restore-proof-incomplete")
                elif any(
                    value is not None
                    for value in (
                        item.backup_digest,
                        item.schema_digest,
                        item.wal_or_lsn_digest,
                        item.checksum_digest,
                        item.probe_digest,
                    )
                ):
                    _reject("resource-outcome-proof-not-canonical")
            elif any(
                value is not None
                for value in (
                    item.backup_digest,
                    item.schema_digest,
                    item.wal_or_lsn_digest,
                    item.checksum_digest,
                    item.probe_digest,
                )
            ):
                _reject("resource-outcome-proof-not-canonical")
        return outcomes

    def _validate_safe_terminal_outcomes(self, head: LifecycleRecord) -> None:
        outcomes = self._validate_resource_outcomes(
            head.resource_outcomes, head.binding
        )
        if any(item.outcome not in SAFE_RESOURCE_OUTCOMES for item in outcomes):
            _reject("rollback-resource-reconciliation-incomplete")

    def _assert_binding(self, binding: LifecycleBinding) -> None:
        self._validate_binding_shape(binding)
        expected = self._require_head().binding
        if binding.lifecycle_id != expected.lifecycle_id:
            _reject("lifecycle-binding-mismatch")
        if binding.release_sha != expected.release_sha:
            _reject("release-binding-mismatch")
        if binding.controller_digest != expected.controller_digest:
            _reject("controller-binding-mismatch")
        if binding.policy_digest != expected.policy_digest:
            _reject("policy-binding-mismatch")
        if binding.lease_policy != expected.lease_policy:
            _reject("lease-policy-binding-mismatch")
        if binding.resources != expected.resources:
            _reject("resource-set-mismatch")

    def _assert_context(self, binding: LifecycleBinding, lease: Lease) -> None:
        head = self._require_head()
        self._assert_binding(binding)
        self._validate_lease_tokens(lease, binding)
        if lease != head.lease:
            if lease.lease_id != head.lease.lease_id:
                _reject("lease-id-mismatch")
            if lease.holder_id != head.lease.holder_id:
                _reject("lease-holder-mismatch")
            if lease.fencing_token != head.lease.fencing_token:
                _reject("fencing-token-mismatch")
            if lease.resource_fencing != head.lease.resource_fencing:
                _reject("resource-fencing-mismatch")
            _reject("lease-window-mismatch")

    def _assert_new_fences(self, lease: Lease) -> None:
        if lease.fencing_token <= self._highest_fencing_token:
            _reject("fencing-token-not-increasing")
        for resource, token in lease.resource_fencing:
            if token <= self._resource_fencing.get(resource, -1):
                _reject("resource-fencing-token-not-increasing")

    def _record_new_lease(self, lease: Lease) -> None:
        self._used_lease_ids.add(lease.lease_id)
        self._highest_fencing_token = lease.fencing_token
        self._resource_fencing.update(dict(lease.resource_fencing))

    def _assert_cas(self, cas: CasSuccessor) -> None:
        for value in (
            cas.predecessor_generation,
            cas.predecessor_epoch,
            cas.successor_generation,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > MAX_INT64
            ):
                _reject("cas-shape-invalid")
        if cas.successor_generation < 1:
            _reject("cas-shape-invalid")
        _validate_digest(cas.predecessor_hash, "cas-shape-invalid")
        if (
            not isinstance(cas.predecessor_state, str)
            or not cas.predecessor_state
            or (
                cas.predecessor_lifecycle_id is not None
                and (
                    not isinstance(cas.predecessor_lifecycle_id, str)
                    or not cas.predecessor_lifecycle_id
                )
            )
        ):
            _reject("cas-shape-invalid")
        expected = self.cas_successor()
        checks = (
            (cas.predecessor_generation, expected.predecessor_generation, "cas-predecessor-generation-mismatch"),
            (cas.successor_generation, expected.successor_generation, "cas-successor-generation-mismatch"),
            (cas.predecessor_hash, expected.predecessor_hash, "cas-predecessor-hash-mismatch"),
            (cas.predecessor_state, expected.predecessor_state, "cas-predecessor-state-mismatch"),
            (cas.predecessor_lifecycle_id, expected.predecessor_lifecycle_id, "cas-predecessor-lifecycle-mismatch"),
            (cas.predecessor_epoch, expected.predecessor_epoch, "cas-predecessor-epoch-mismatch"),
        )
        for actual, wanted, code in checks:
            if actual != wanted:
                _reject(code)

    @staticmethod
    def _assert_lease_active(lease: Lease, now: int) -> None:
        if now >= lease.deadline:
            _reject("lease-expired")

    def _require_head(self) -> LifecycleRecord:
        if self.head is None:
            _reject("lifecycle-not-admitted")
        return self.head

    @staticmethod
    def _binding_replay_context(
        binding: LifecycleBinding, epoch: int | None
    ) -> dict[str, Any]:
        if type(binding) is not LifecycleBinding:
            _reject("event-replay-context-mismatch")
        try:
            return {
                "binding_digest": digest(binding),
                "controller_digest": binding.controller_digest,
                "epoch": epoch,
                "lifecycle_id": binding.lifecycle_id,
                "policy_digest": binding.policy_digest,
                "resource_set_digest": digest(binding.resources),
            }
        except (RecursionError, TypeError, ValueError):
            _reject("event-replay-context-mismatch")

    def _epoch_for_exact_binding(self, binding: LifecycleBinding) -> int | None:
        epochs = {
            record.epoch for record in self._records if record.binding == binding
        }
        if len(epochs) != 1:
            return None
        return next(iter(epochs))

    def _expected_replay_context(
        self,
        command: AdmissionCommand
        | TransitionCommand
        | LeaseRenewalCommand
        | ReconciliationAdmissionCommand
        | RollbackCompletionCommand,
    ) -> dict[str, Any]:
        if type(command) is AdmissionCommand:
            context = self._binding_replay_context(command.binding, command.epoch)
            context.update(
                event_kind="normal-admission",
                lease_digest=digest(command.lease),
            )
            return context
        if type(command) is TransitionCommand:
            context = self._binding_replay_context(
                command.binding, self._epoch_for_exact_binding(command.binding)
            )
            context.update(
                event_kind="phase-transition",
                lease_digest=digest(command.lease),
                phase=command.target,
                proof_digest=digest(command.proof),
            )
            return context
        if type(command) is LeaseRenewalCommand:
            context = self._binding_replay_context(
                command.binding, self._epoch_for_exact_binding(command.binding)
            )
            context.update(
                event_kind="lease-renewed",
                prior_lease_digest=digest(command.lease),
                new_deadline=command.new_deadline,
            )
            return context
        if type(command) is ReconciliationAdmissionCommand:
            context = self._binding_replay_context(
                command.binding, self._epoch_for_exact_binding(command.binding)
            )
            context.update(
                event_kind="reconciliation-admission",
                recovery_authority_digest=digest(command.recovery_authority),
                recovery_lease_digest=digest(command.recovery_lease),
            )
            return context
        if type(command) is RollbackCompletionCommand:
            context = self._binding_replay_context(
                command.binding, self._epoch_for_exact_binding(command.binding)
            )
            context.update(
                event_kind="rollback-result",
                lease_digest=digest(command.lease),
                proof_digest=digest(command.evidence),
                resource_outcomes_digest=digest(command.resource_outcomes),
            )
            return context
        _reject("event-replay-context-mismatch")

    def _persisted_replay_context(
        self, record: LifecycleRecord
    ) -> dict[str, Any]:
        context = self._binding_replay_context(record.binding, record.epoch)
        context["event_kind"] = record.event_kind
        if record.event_kind == "normal-admission":
            context["lease_digest"] = digest(record.lease)
        elif record.event_kind == "phase-transition":
            context.update(
                lease_digest=digest(record.lease),
                phase=record.phase,
                proof_digest=digest(record.proof),
            )
        elif record.event_kind == "lease-renewed":
            predecessor_index = record.generation - 2
            if predecessor_index < 0 or predecessor_index >= len(self._records):
                _reject("event-replay-context-mismatch")
            predecessor = self._records[predecessor_index]
            if predecessor.record_hash != record.predecessor_hash:
                _reject("event-replay-context-mismatch")
            context.update(
                prior_lease_digest=digest(predecessor.lease),
                new_deadline=record.lease.deadline,
            )
        elif record.event_kind == "reconciliation-admission":
            context.update(
                recovery_authority_digest=digest(record.recovery_authority),
                recovery_lease_digest=digest(record.lease),
            )
        elif record.event_kind == "rollback-result":
            context.update(
                lease_digest=digest(record.lease),
                proof_digest=digest(record.proof),
                resource_outcomes_digest=digest(record.resource_outcomes),
            )
        else:
            _reject("event-replay-context-mismatch")
        return context

    def _replay(
        self,
        command: AdmissionCommand
        | TransitionCommand
        | LeaseRenewalCommand
        | ReconciliationAdmissionCommand
        | RollbackCompletionCommand,
        *,
        expected_kind: str,
        expected_phase: Phase | None = None,
    ) -> LifecycleRecord | None:
        event_id = command.event_id
        request_transport_digest = command.request_transport_digest
        if not isinstance(event_id, str) or not event_id.strip():
            _reject("invalid-event-id")
        _validate_digest(
            request_transport_digest, "invalid-request-transport-digest"
        )
        prior = self._events.get(event_id)
        if prior is None:
            return None
        prior_digest, record = prior
        if request_transport_digest != prior_digest:
            _reject("event-id-conflict")
        if record.event_kind != expected_kind:
            _reject("event-replay-kind-mismatch")
        if expected_phase is not None and record.phase is not expected_phase:
            _reject("event-replay-phase-mismatch")
        try:
            expected_context = self._expected_replay_context(command)
            persisted_context = self._persisted_replay_context(record)
            if digest(expected_context) != digest(persisted_context):
                _reject("event-replay-context-mismatch")
        except LifecycleModelError:
            raise
        except (AttributeError, RecursionError, TypeError, ValueError):
            _reject("event-replay-context-mismatch")
        return record

    def _append(
        self,
        event_id: str,
        request_transport_digest: str,
        event_kind: str,
        epoch: int,
        binding: LifecycleBinding,
        lease: Lease,
        phase: Phase,
        state: str,
        clock: TrustedClockObservation,
        proof: PhaseProof | None,
        recovery_authority: RecoveryAuthority | None,
        reconciliation_from_phase: Phase | None,
        resource_outcomes: tuple[ResourceOutcomeEvidence, ...],
    ) -> LifecycleRecord:
        cas = self.cas_successor()
        payload = {
            "schema": SCHEMA,
            "generation": cas.successor_generation,
            "predecessor_hash": cas.predecessor_hash,
            "event_id": event_id,
            "request_transport_digest": request_transport_digest,
            "event_kind": event_kind,
            "epoch": epoch,
            "binding": binding,
            "lease": lease,
            "phase": phase,
            "state": state,
            "clock": clock,
            "proof": proof,
            "recovery_authority": recovery_authority,
            "reconciliation_from_phase": reconciliation_from_phase,
            "resource_outcomes": resource_outcomes,
        }
        record = LifecycleRecord(
            schema=SCHEMA,
            generation=cas.successor_generation,
            record_hash=digest(payload),
            predecessor_hash=cas.predecessor_hash,
            event_id=event_id,
            request_transport_digest=request_transport_digest,
            event_kind=event_kind,
            epoch=epoch,
            binding=binding,
            lease=lease,
            phase=phase,
            state=state,
            clock=clock,
            proof=proof,
            recovery_authority=recovery_authority,
            reconciliation_from_phase=reconciliation_from_phase,
            resource_outcomes=resource_outcomes,
        )
        # The record is simultaneously the chain append and event-index entry.
        self._records.append(record)
        self._events[event_id] = (request_transport_digest, record)
        self._last_observed_at = clock.observed_at
        try:
            if event_kind == "normal-admission":
                self._used_lifecycle_ids.add(binding.lifecycle_id)
            if event_kind in ("normal-admission", "reconciliation-admission"):
                self._record_new_lease(lease)
        except BaseException:
            # The external chain append is the commit point.  If local
            # derived-cache maintenance faults after that point, rebuild
            # from the committed chain so a lost response cannot make a
            # lower fence appear fresh in this process.
            self._rebuild_indexes()
            raise
        return record

    def _rebuild_indexes(self) -> None:
        self._events.clear()
        self._used_lifecycle_ids.clear()
        self._used_lease_ids.clear()
        self._highest_fencing_token = -1
        self._resource_fencing.clear()
        for record in self._records:
            self._events[record.event_id] = (
                record.request_transport_digest,
                record,
            )
            if record.event_kind == "normal-admission":
                self._used_lifecycle_ids.add(record.binding.lifecycle_id)
            if record.event_kind in ("normal-admission", "reconciliation-admission"):
                self._used_lease_ids.add(record.lease.lease_id)
                self._highest_fencing_token = record.lease.fencing_token
                self._resource_fencing.update(dict(record.lease.resource_fencing))
        self._last_observed_at = (
            self._records[-1].observed_at if self._records else None
        )

    @staticmethod
    def _record_payload(record: LifecycleRecord) -> dict[str, Any]:
        return {
            "schema": record.schema,
            "generation": record.generation,
            "predecessor_hash": record.predecessor_hash,
            "event_id": record.event_id,
            "request_transport_digest": record.request_transport_digest,
            "event_kind": record.event_kind,
            "epoch": record.epoch,
            "binding": record.binding,
            "lease": record.lease,
            "phase": record.phase,
            "state": record.state,
            "clock": record.clock,
            "proof": record.proof,
            "recovery_authority": record.recovery_authority,
            "reconciliation_from_phase": record.reconciliation_from_phase,
            "resource_outcomes": record.resource_outcomes,
        }

    @staticmethod
    def _latest_phase_record(
        history: Sequence[LifecycleRecord], epoch: int, phase: Phase
    ) -> LifecycleRecord | None:
        for record in reversed(history):
            if (
                record.epoch == epoch
                and record.phase is phase
                and record.event_kind == "phase-transition"
                and isinstance(record.proof, IntentBinding)
            ):
                return record
        return None

    @staticmethod
    def _cas_for_prior(prior: LifecycleRecord) -> CasSuccessor:
        return CasSuccessor(
            prior.generation,
            prior.record_hash,
            prior.state,
            prior.binding.lifecycle_id,
            prior.epoch,
            prior.generation + 1,
        )


def describe_contract() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "authority": "offline-structural-semantic-reference-only",
        "cryptographic_authority": False,
        "external_operations": list(EXTERNAL_OPERATIONS),
        "workflow_operations": list(WORKFLOW_OPERATIONS),
        "preflight_mutates": False,
        "release_run_owner": "installed-controller",
        "raw_resource_credential_effects_allowed": RAW_RESOURCE_CREDENTIAL_EFFECTS_ALLOWED,
        "resource_effect_path": "fence-enforcing-mediators-only",
        "required_mediator_capability": FENCE_MEDIATOR_CAPABILITY,
        "required_resource_kinds": sorted(kind.value for kind in REQUIRED_RESOURCE_KINDS),
        "success_path": [phase.value for phase in SUCCESS_PATH],
        "recovery_admission": Phase.RECONCILIATION_ADMITTED.value,
        "normal_admission_safe_terminals": sorted(
            phase.value for phase in SAFE_ADMISSION_TERMINALS
        ),
        "event_index_storage": "same-external-cas-record-as-chain-append",
        "replay_return_requires": [
            "event-id",
            "request-transport-digest",
            "event-kind",
            "phase-when-applicable",
            "exact-immutable-command-context",
        ],
        "replay_command_context": (
            "binding-policy-controller-resources-epoch-lease-and-effect-inputs;"
            "clock-and-cas-excluded"
        ),
        "persisted_clock_receipts": "restart-verifiable-authenticated-harness",
        "genesis_hash": GENESIS_HASH,
    }


def main() -> int:
    print(json.dumps(describe_contract(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
