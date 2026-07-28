from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest

from scripts import propertyquarry_release_evidence as evidence


def _d(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _resource_fences(binding: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "kind": item["kind"],
            "resource_id": item["resource_id"],
            "fence_token": item["fence_token"],
        }
        for item in binding["resources"]
    ]


def _outcomes(
    phase: str,
    binding: dict[str, object],
    evidence_digest: str,
) -> list[dict[str, object]]:
    values = {kind: "unchanged" for kind in evidence.RESOURCE_KINDS}
    if phase == "containment":
        values.update({"database": "fenced", "traffic": "contained"})
    elif phase in {"deploy", "live-verification", "activation"}:
        values.update(
            {"database": "restored-verified", "runtime": "deployed-verified", "traffic": "contained"}
        )
    elif phase == "overlay-activation":
        values.update(
            {
                "database": "restored-verified",
                "runtime": "deployed-verified",
                "traffic": "contained",
                "overlay": "activated-verified",
            }
        )
    elif phase == "finalization":
        values.update(
            {
                "database": "restored-verified",
                "runtime": "deployed-verified",
                "traffic": "activated-verified",
                "overlay": "activated-verified",
                "public-tour": "activated-verified",
                "launch-authority": "sealed-verified",
                "monitoring-delivery": "continuity-verified",
            }
        )
    return [
        {
            "kind": item["kind"],
            "resource_id": item["resource_id"],
            "outcome": values[item["kind"]],
            "evidence_digest": evidence_digest,
        }
        for item in binding["resources"]
    ]


def _details(
    evidence_type: str,
    state: str,
    record: dict[str, object],
    binding: dict[str, object],
    preflight: dict[str, object],
    ancestry: list[dict[str, object]],
    first_generation: int,
) -> dict[str, object]:
    if evidence_type == "signed-request":
        return {
            "request_id": binding["request_id"],
            "nonce_digest": binding["request_nonce_digest"],
            "transport_digest": binding["request_transport_digest"],
            "envelope_digest": binding["request_envelope_digest"],
        }
    if evidence_type == "ready-preflight":
        return {
            "request_id": preflight["request_id"],
            "response_transport_digest": preflight["response_transport_digest"],
            "response_envelope_digest": preflight["response_envelope_digest"],
            "observed_seal_digest": preflight["observed_lifecycle_seal_digest"],
            "evaluated_at": preflight["evaluated_at"],
            "valid_until": preflight["valid_until"],
            "required_check_set_digest": preflight["required_check_set_digest"],
        }
    if evidence_type == "replay-consumption":
        return {
            "request_id": binding["request_id"],
            "nonce_digest": binding["request_nonce_digest"],
            "receipt_digest": _d("replay-receipt"),
        }
    if evidence_type == "lease-fence-acquisition":
        return {
            "lease_id": binding["lease_id"],
            "lease_holder": binding["lease_holder"],
            "global_fence_token": binding["global_fence_token"],
            "resource_fences": _resource_fences(binding),
            "receipt_digest": _d("lease-receipt"),
        }
    if evidence_type == "phase-intent":
        return {
            key: record[key]
            for key in (
                "plan_digest",
                "input_digest",
                "planned_effect_digest",
                "recovery_target_digest",
                "external_idempotency_key",
                "expected_resource_versions_digest",
            )
        }
    if evidence_type == "target-fence-proof":
        return {
            "global_fence_token": binding["global_fence_token"],
            "resource_fences": _resource_fences(binding),
            "proof_digest": _d(f"fence-proof:{state}"),
        }
    if evidence_type == "writer-drain":
        return {"writer_count": 0, "receipt_digest": _d("writer-drain")}
    if evidence_type == "database-fence":
        database = next(item for item in binding["resources"] if item["kind"] == "database")
        return {
            "resource_id": database["resource_id"],
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": database["fence_token"],
            "proof_digest": _d("database-fence"),
        }
    if evidence_type == "recovery-target":
        return {"target_digest": record["recovery_target_digest"], "verification_digest": _d("recovery-target")}
    if evidence_type == "candidate-artifact":
        return {
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "artifact_manifest_digest": _d("candidate-artifact-manifest"),
        }
    if evidence_type == "deployment-identity":
        return {
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "config_digest": _d("config"),
            "replica_set_digest": _d("replicas"),
        }
    if evidence_type == "database-outcome":
        return {
            "resource_id": next(item["resource_id"] for item in binding["resources"] if item["kind"] == "database"),
            "outcome": "restored-verified",
            "database_identity_digest": _d("database-identity"),
            "pre_schema_digest": _d("schema-before"),
            "post_schema_digest": _d("schema-before"),
            "probes_digest": _d("database-probes"),
            "backup_identity_digest": _d("database-backup"),
            "recovery_position": {"kind": "lsn", "digest": _d("database-lsn")},
            "restore_checksum_digest": _d("restore-checksum"),
        }
    if evidence_type == "runtime-health":
        return {
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "probe_digest": _d("runtime-health"),
        }
    if evidence_type == "live-identity":
        return {
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "config_digest": _d("config"),
            "replica_set_digest": _d("replicas"),
        }
    if evidence_type in {"public-probe", "authenticated-probe"}:
        return {"probe_set_digest": _d(f"{evidence_type}:set"), "response_digest": _d(f"{evidence_type}:response")}
    if evidence_type == "slo-alert-observation":
        return {"observation_digest": _d("slo-observation"), "alerts_clear": True}
    if evidence_type == "activation-account-state":
        return {
            "persona_digest": _d("persona"),
            "broker_digest": _d("broker"),
            "idempotency_key": record["external_idempotency_key"],
            "before_state_digest": _d("account-before"),
            "after_state_digest": _d("account-after"),
            "release_digest": binding["release_digest"],
        }
    if evidence_type == "activation-effect-audit":
        return {
            "effect_digest": record["effect_digest"],
            "unauthorized_provider_effect_count": 0,
            "unauthorized_send_effect_count": 0,
        }
    if evidence_type == "overlay-cas":
        return {
            "prior_snapshot_digest": _d("overlay-before"),
            "staged_snapshot_digest": _d("overlay-staged"),
            "active_snapshot_digest": _d("overlay-active"),
            "cas_receipt_digest": _d("overlay-cas"),
            "success": True,
        }
    if evidence_type == "overlay-active-revalidation":
        return {"active_snapshot_digest": _d("overlay-active"), "probe_digest": _d("overlay-probe")}
    if evidence_type == "complete-ancestry":
        return {
            "predecessor_ancestry_root": evidence.canonical_digest(ancestry),
            "predecessor_entry_count": len(ancestry),
            "first_generation": first_generation,
            "predecessor_generation": ancestry[-1]["generation"],
            "predecessor_seal_digest": ancestry[-1]["seal_digest"],
        }
    if evidence_type == "gold-result":
        return {"result_digest": _d("gold-result"), "gold": True}
    if evidence_type == "final-authority":
        resource = next(item for item in binding["resources"] if item["kind"] == "launch-authority")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "launch-authority")
        return {
            "authority_digest": _d(f"artifact:{state}:{evidence_type}"),
            "launch_success": True,
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    if evidence_type == "monitoring-continuity":
        resource = next(item for item in binding["resources"] if item["kind"] == "monitoring-delivery")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "monitoring-delivery")
        return {
            "before_digest": _d("monitoring-before"),
            "after_digest": _d("monitoring-after"),
            "gap_seconds": 0,
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    if evidence_type == "runtime-final-state":
        resource = next(item for item in binding["resources"] if item["kind"] == "runtime")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "runtime")
        return {
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "config_digest": _d("config"),
            "replica_set_digest": _d("replicas"),
            "health_probe_digest": _d("terminal-runtime-health"),
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    if evidence_type == "traffic-final-state":
        resource = next(item for item in binding["resources"] if item["kind"] == "traffic")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "traffic")
        return {
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "release_digest": binding["release_digest"],
            "route_selection_digest": _d("terminal-traffic-selection"),
            "public_probe_digest": _d("terminal-traffic-probe"),
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    if evidence_type == "overlay-final-state":
        resource = next(item for item in binding["resources"] if item["kind"] == "overlay")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "overlay")
        return {
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "active_snapshot_digest": _d("overlay-active"),
            "active_revalidation_digest": _d("terminal-overlay-revalidation"),
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    if evidence_type == "public-tour-final-state":
        resource = next(item for item in binding["resources"] if item["kind"] == "public-tour")
        outcome = next(item for item in record["resource_outcomes"] if item["kind"] == "public-tour")
        return {
            "resource_id": resource["resource_id"],
            "outcome": outcome["outcome"],
            "volume_digest": _d("public-tour-volume"),
            "catalog_digest": _d("public-tour-catalog"),
            "route_probe_digest": _d("public-tour-route-probe"),
            "global_fence_token": binding["global_fence_token"],
            "resource_fence_token": resource["fence_token"],
        }
    raise AssertionError(evidence_type)


def _valid_fixture():
    resources = [
        {"kind": kind, "resource_id": f"{kind}:prod", "fence_token": 200 + index}
        for index, kind in enumerate(evidence.RESOURCE_KINDS)
    ]
    resource_set = [{"kind": item["kind"], "resource_id": item["resource_id"]} for item in resources]
    binding = {
        "authority_id": "authority:prod",
        "namespace": "propertyquarry:release",
        "target_id": "propertyquarry:prod",
        "environment": "production",
        "lifecycle_id": "lifecycle:2026-07-17",
        "epoch_id": "epoch:42",
        "request_id": "request:release-run:42",
        "request_nonce_digest": _d("release-request-nonce"),
        "request_transport_digest": _d("release-request-transport"),
        "request_envelope_digest": _d("release-request-envelope"),
        "release_digest": _d("release"),
        "image_digest": _d("image"),
        "controller_digest": _d("controller"),
        "policy_digest": _d("policy"),
        "resource_set_digest": evidence.canonical_digest(resource_set),
        "lease_id": "lease:42",
        "lease_holder": "controller:holder:42",
        "lease_deadline": "2026-07-17T01:00:00Z",
        "global_fence_token": 9001,
        "resources": resources,
    }
    preflight = {
        "request_id": "request:preflight:41",
        "nonce_digest": _d("preflight-nonce"),
        "request_transport_digest": _d("preflight-request-transport"),
        "request_envelope_digest": _d("preflight-request-envelope"),
        "response_transport_digest": _d("preflight-response-transport"),
        "response_envelope_digest": _d("preflight-response-envelope"),
        "observed_lifecycle_seal_digest": _d("prior-terminal-seal"),
        "release_digest": binding["release_digest"],
        "controller_digest": binding["controller_digest"],
        "policy_digest": binding["policy_digest"],
        "evaluated_at": "2026-07-17T00:00:00Z",
        "valid_until": "2026-07-17T00:05:00Z",
        "required_check_set_digest": _d("closed-preflight-checks"),
    }
    all_evidence_types = set().union(*evidence.REQUIRED_EVIDENCE.values())
    approved_verifiers = {kind: _d(f"verifier:{kind}") for kind in all_evidence_types}
    storage_identity = _d("controller-journal-filesystem")
    first_generation = 100
    ancestry: list[dict[str, object]] = []
    entries: list[dict[str, object]] = []
    trusted_fsync: dict[str, tuple[str, str, str]] = {}
    previous_seal = preflight["observed_lifecycle_seal_digest"]
    previous_evidence_root = None
    active_intent = None
    active_intent_generation = None
    active_intent_seal = None
    resource_proofs: dict[str, str] = {}

    for index, state in enumerate(evidence.SUCCESS_STATES):
        generation = first_generation + index
        record_type, phase = evidence.STATE_PHASE[state]
        phase_types = sorted(evidence.REQUIRED_EVIDENCE[state])
        artifacts = [_d(f"artifact:{state}:{kind}") for kind in phase_types]
        if record_type == "intent":
            record = {
                "record_type": "intent",
                "phase": phase,
                "plan_digest": _d(f"plan:{phase}"),
                "input_digest": _d(f"input:{phase}"),
                "planned_effect_digest": _d(f"planned-effect:{phase}"),
                "recovery_target_digest": _d(f"recovery:{phase}"),
                "external_idempotency_key": f"idempotency:{phase}:42",
                "expected_resource_versions_digest": _d(f"versions:{phase}"),
                "global_fence_token": binding["global_fence_token"],
                "resource_fences": _resource_fences(binding),
            }
        else:
            if state == "admitted":
                source = {
                    "plan_digest": _d("plan:admission"),
                    "input_digest": _d("input:admission"),
                    "planned_effect_digest": _d("planned-effect:admission"),
                    "recovery_target_digest": _d("recovery:admission"),
                    "external_idempotency_key": "idempotency:admission:42",
                    "expected_resource_versions_digest": _d("versions:admission"),
                }
                intent_generation = evidence.ADMISSION_INTENT_GENERATION
                intent_seal = previous_seal
            else:
                source = active_intent
                intent_generation = active_intent_generation
                intent_seal = active_intent_seal
            artifact_by_type = dict(zip(phase_types, artifacts))
            if state == "admitted":
                resource_proofs = {
                    kind: artifact_by_type["lease-fence-acquisition"] for kind in evidence.RESOURCE_KINDS
                }
            elif state == "contained":
                resource_proofs.update(
                    {kind: artifact_by_type["target-fence-proof"] for kind in evidence.RESOURCE_KINDS}
                )
                resource_proofs["database"] = artifact_by_type["database-fence"]
                resource_proofs["traffic"] = artifact_by_type["writer-drain"]
            elif state == "deployed":
                resource_proofs.update(
                    {
                        "database": artifact_by_type["database-outcome"],
                        "runtime": artifact_by_type["runtime-health"],
                        "traffic": artifact_by_type["target-fence-proof"],
                        "overlay": artifact_by_type["target-fence-proof"],
                        "public-tour": artifact_by_type["target-fence-proof"],
                        "launch-authority": artifact_by_type["target-fence-proof"],
                        "monitoring-delivery": artifact_by_type["target-fence-proof"],
                    }
                )
            elif state == "live-verified":
                resource_proofs["runtime"] = artifact_by_type["live-identity"]
            elif state == "overlay-activated":
                resource_proofs["overlay"] = artifact_by_type["overlay-cas"]
            elif state == "sealed-final":
                for kind, evidence_type in evidence.TERMINAL_RESOURCE_EVIDENCE.items():
                    resource_proofs[kind] = artifact_by_type[evidence_type]
            resource_outcomes = _outcomes(phase, binding, artifacts[0])
            for outcome in resource_outcomes:
                outcome["evidence_digest"] = resource_proofs[outcome["kind"]]
            record = {
                "record_type": "result",
                "phase": phase,
                "intent_generation": intent_generation,
                "intent_seal_digest": intent_seal,
                **{
                    key: source[key]
                    for key in (
                        "plan_digest",
                        "input_digest",
                        "planned_effect_digest",
                        "recovery_target_digest",
                        "external_idempotency_key",
                        "expected_resource_versions_digest",
                    )
                },
                "effect_digest": evidence.canonical_digest(
                    {"planned_effect_digest": source["planned_effect_digest"], "evidence_artifact_digests": sorted(artifacts)}
                ),
                "outcome_digest": evidence.canonical_digest(resource_outcomes),
                "resource_outcomes": resource_outcomes,
            }

        items = []
        for type_index, evidence_type in enumerate(phase_types):
            items.append(
                {
                    "type": evidence_type,
                    "subject": f"{state}:{evidence_type}",
                    "status": "pass",
                    "lifecycle_id": binding["lifecycle_id"],
                    "release_digest": binding["release_digest"],
                    "image_digest": binding["image_digest"],
                    "environment": binding["environment"],
                    # Deliberately reverse timestamps: ledger generation, not time, orders evidence.
                    "observation_time": f"2026-07-17T00:{59 - index:02d}:{type_index:02d}Z",
                    "verifier_binary_digest": approved_verifiers[evidence_type],
                    "verifier_policy_digest": binding["policy_digest"],
                    "artifact_digest": artifacts[type_index],
                    "byte_size": 1000 + type_index,
                    "media_type": "application/json",
                    "dependencies": [] if previous_evidence_root is None else [previous_evidence_root],
                    "details": _details(
                        evidence_type,
                        state,
                        record,
                        binding,
                        preflight,
                        ancestry,
                        first_generation,
                    ),
                }
            )
        evidence_root = evidence.canonical_digest(items)
        phase_root = evidence.canonical_digest(
            {"generation": generation, "state": state, "record": record, "evidence_root": evidence_root}
        )
        file_receipt = _d(f"file-fsync:{state}:{phase_root}")
        directory_receipt = _d(f"directory-fsync:{state}:{phase_root}")
        persistence = {
            "method": "file-fsync+directory-fsync-v1",
            "phase_root": phase_root,
            "evidence_root": evidence_root,
            "storage_identity_digest": storage_identity,
            "persisted_bytes": 4096 + index,
            "file_fsync_receipt_digest": file_receipt,
            "directory_fsync_receipt_digest": directory_receipt,
            "fsync_completed_before_cas": True,
        }
        trusted_fsync[state] = (file_receipt, directory_receipt, phase_root)
        entry = {
            "generation": generation,
            "state": state,
            "binding": deepcopy(binding),
            "record": record,
            "evidence": items,
            "evidence_root": evidence_root,
            "phase_root": phase_root,
            "persistence": persistence,
            "cas": {
                "event_id": f"event:{generation}:{state}",
                "authority_observed_at": f"2026-07-17T00:01:{index:02d}Z",
                "previous_seal_digest": previous_seal,
                "state_digest": _d("placeholder"),
                "seal_digest": _d("placeholder"),
            },
        }
        entry["cas"]["state_digest"] = evidence._state_digest(entry)
        entry["cas"]["seal_digest"] = evidence._seal_digest(entry, entry["cas"])
        ancestry_item = {"generation": generation, "state": state, **entry["cas"]}
        ancestry.append(deepcopy(ancestry_item))
        entries.append(entry)
        previous_seal = entry["cas"]["seal_digest"]
        previous_evidence_root = evidence_root
        if record_type == "intent":
            active_intent = record
            active_intent_generation = generation
            active_intent_seal = previous_seal
        elif state != "admitted":
            active_intent = None
            active_intent_generation = None
            active_intent_seal = None

    manifest = {
        "schema": evidence.SCHEMA,
        "version": evidence.VERSION,
        "binding": binding,
        "first_generation": first_generation,
        "last_generation": first_generation + len(entries) - 1,
        "entries": entries,
        "manifest_root": _d("placeholder"),
        "final_seal": {},
        "signature": {},
    }
    manifest["manifest_root"] = evidence.compute_manifest_root(manifest)
    manifest["final_seal"] = {
        "manifest_root": manifest["manifest_root"],
        "entry_count": len(entries),
        "first_generation": first_generation,
        "last_generation": manifest["last_generation"],
        "terminal_state": "sealed-final",
        "terminal_seal_digest": ancestry[-1]["seal_digest"],
        "preflight": preflight,
        "ancestry": deepcopy(ancestry),
        "ancestry_root": evidence.canonical_digest(ancestry),
        "final_authority_digest": next(
            item["artifact_digest"]
            for item in entries[-1]["evidence"]
            if item["type"] == "final-authority"
        ),
    }
    final_seal_digest = evidence.canonical_digest(manifest["final_seal"])
    signed_payload = evidence._canonical(
        {"final_seal_digest": final_seal_digest, "manifest_root": manifest["manifest_root"]}
    )
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": "propertyquarry-release-key:1",
        "signed_manifest_root": manifest["manifest_root"],
        "signed_final_seal_digest": final_seal_digest,
        "value": hashlib.sha256(b"trusted-test-key:" + signed_payload).hexdigest(),
    }
    expectations = evidence.EvidenceExpectations(
        binding=deepcopy(binding),
        preflight=deepcopy(preflight),
        ancestry=deepcopy(ancestry),
        allowed_resource_outcomes={kind: evidence.FIXED_OUTCOMES[kind] for kind in evidence.RESOURCE_KINDS},
        approved_verifier_digests=approved_verifiers,
        storage_identity_digest=storage_identity,
        gold_result_digest=_d("gold-result"),
        signature_algorithm="ed25519",
        signature_key_id="propertyquarry-release-key:1",
    )

    def signature_verifier(payload, signature):
        return signature["value"] == hashlib.sha256(b"trusted-test-key:" + payload).hexdigest()

    def fsync_verifier(state, persistence):
        return trusted_fsync.get(state) == (
            persistence["file_fsync_receipt_digest"],
            persistence["directory_fsync_receipt_digest"],
            persistence["phase_root"],
        )

    return manifest, expectations, signature_verifier, fsync_verifier


def _validate(fixture) -> evidence.ValidatedEvidenceManifest:
    manifest, expectations, signature_verifier, fsync_verifier = fixture
    return evidence.validate_manifest(
        manifest,
        expectations,
        signature_verifier=signature_verifier,
        fsync_verifier=fsync_verifier,
    )


def test_complete_manifest_validates_and_ledger_generation_overrides_reverse_timestamps() -> None:
    fixture = _valid_fixture()

    validated = _validate(fixture)

    assert validated.first_generation == 100
    assert validated.last_generation == 112
    assert validated.manifest_root == fixture[0]["manifest_root"]
    assert validated.terminal_seal_digest == fixture[0]["final_seal"]["terminal_seal_digest"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["entries"].pop(4),
        lambda manifest: manifest["entries"].__setitem__(slice(3, 5), list(reversed(manifest["entries"][3:5]))),
        lambda manifest: manifest["entries"][3].__setitem__("generation", manifest["entries"][2]["generation"]),
        lambda manifest: manifest["entries"][5].__setitem__("state", "invented-result"),
        lambda manifest: manifest["entries"][2].__setitem__("unknown", True),
    ],
    ids=["missing", "reordered", "duplicate-generation", "unknown-state", "unknown-field"],
)
def test_missing_reordered_duplicate_or_unknown_entries_fail_closed(mutate) -> None:
    fixture = _valid_fixture()
    mutate(fixture[0])

    with pytest.raises(evidence.EvidenceValidationError):
        _validate(fixture)


def test_unknown_or_missing_typed_evidence_fails_closed() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][2]["evidence"][0]["type"] = "arbitrary-digest"

    with pytest.raises(evidence.EvidenceValidationError, match="unknown evidence discriminator"):
        _validate(fixture)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["binding"].__setitem__("controller_digest", _d("stale-controller")),
        lambda manifest: manifest["entries"][6]["binding"].__setitem__("lease_id", "lease:stale"),
        lambda manifest: manifest["entries"][7]["record"].__setitem__("global_fence_token", 9000),
        lambda manifest: manifest["entries"][7]["record"]["resource_fences"][0].__setitem__("fence_token", 1),
        lambda manifest: manifest["entries"][0]["evidence"][0].__setitem__("lifecycle_id", "lifecycle:stale"),
    ],
    ids=["controller", "lease", "global-fence", "resource-fence", "lifecycle"],
)
def test_stale_identity_or_fence_substitution_fails_closed(mutate) -> None:
    fixture = _valid_fixture()
    mutate(fixture[0])

    with pytest.raises(evidence.EvidenceValidationError):
        _validate(fixture)


def test_artifact_digest_substitution_breaks_ordered_evidence_root() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][8]["evidence"][0]["artifact_digest"] = _d("substituted-artifact")

    with pytest.raises(evidence.EvidenceValidationError, match="result evidence|evidence_root"):
        _validate(fixture)


def test_unresolved_database_outcome_is_never_success_evidence() -> None:
    fixture = _valid_fixture()
    deployed = fixture[0]["entries"][4]
    db_item = next(item for item in deployed["evidence"] if item["type"] == "database-outcome")
    db_item["details"]["outcome"] = "unresolved"

    with pytest.raises(evidence.EvidenceValidationError, match="unresolved"):
        _validate(fixture)


def test_restored_database_requires_backup_schema_lsn_checksum_and_probes() -> None:
    fixture = _valid_fixture()
    deployed = fixture[0]["entries"][4]
    db_item = next(item for item in deployed["evidence"] if item["type"] == "database-outcome")
    del db_item["details"]["restore_checksum_digest"]

    with pytest.raises(evidence.EvidenceValidationError, match="closed schema mismatch"):
        _validate(fixture)


def test_manifest_root_rejects_truncation_even_if_range_is_untouched() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"] = fixture[0]["entries"][:-1]

    with pytest.raises(evidence.EvidenceValidationError, match="truncated"):
        _validate(fixture)


def test_manifest_root_rejects_entry_reordering() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][9], fixture[0]["entries"][10] = (
        fixture[0]["entries"][10],
        fixture[0]["entries"][9],
    )

    with pytest.raises(evidence.EvidenceValidationError, match="ordered"):
        _validate(fixture)


@pytest.mark.parametrize("field", ["fsync_completed_before_cas", "file_fsync_receipt_digest"])
def test_fake_fsync_acknowledgement_fails_closed(field: str) -> None:
    fixture = _valid_fixture()
    persistence = fixture[0]["entries"][4]["persistence"]
    persistence[field] = False if field == "fsync_completed_before_cas" else _d("fake-fsync")

    with pytest.raises(evidence.EvidenceValidationError, match="fsync"):
        _validate(fixture)


def test_fsync_callback_is_mandatory_trust_not_self_attestation() -> None:
    fixture = _valid_fixture()

    with pytest.raises(evidence.EvidenceValidationError, match="trusted fsync verifier rejected"):
        evidence.validate_manifest(
            fixture[0],
            fixture[1],
            signature_verifier=fixture[2],
            fsync_verifier=lambda _state, _receipt: False,
        )


def test_terminal_seal_substitution_fails_against_full_trusted_ancestry() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][-1]["cas"]["seal_digest"] = _d("substituted-terminal-seal")

    with pytest.raises(evidence.EvidenceValidationError, match="seal_digest"):
        _validate(fixture)


def test_final_seal_must_bind_exact_preflight_and_complete_ancestry() -> None:
    fixture = _valid_fixture()
    fixture[0]["final_seal"]["preflight"]["request_id"] = "request:wrong-preflight"

    with pytest.raises(evidence.EvidenceValidationError, match="exact ready preflight"):
        _validate(fixture)


def test_preflight_and_release_run_must_have_distinct_request_and_nonce() -> None:
    fixture = _valid_fixture()
    fixture[1].preflight["nonce_digest"] = fixture[1].binding["request_nonce_digest"]

    with pytest.raises(evidence.EvidenceValidationError, match="distinct request IDs and nonces"):
        _validate(fixture)


def test_database_outcome_must_be_authorized_by_the_bound_policy() -> None:
    fixture = list(_valid_fixture())
    allowed = dict(fixture[1].allowed_resource_outcomes)
    allowed["database"] = frozenset({"unchanged", "fenced"})
    fixture[1] = replace(fixture[1], allowed_resource_outcomes=allowed)

    with pytest.raises(evidence.EvidenceValidationError, match="trusted policy"):
        _validate(tuple(fixture))


def test_signature_must_cover_the_exact_final_seal_wrapper() -> None:
    fixture = _valid_fixture()
    fixture[0]["signature"]["signed_final_seal_digest"] = _d("wrong-final-seal")

    with pytest.raises(evidence.EvidenceValidationError, match="does not bind the final seal"):
        _validate(fixture)


@pytest.mark.parametrize(
    "invalid_time",
    ["2026-07-16T23:59:59Z", "2026-07-17T00:05:00Z", "2026-07-17T00:06:00Z"],
    ids=["before-evaluation", "at-expiry", "after-expiry"],
)
def test_admission_authority_time_must_be_inside_ready_preflight_validity(invalid_time: str) -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][0]["cas"]["authority_observed_at"] = invalid_time
    fixture[1].ancestry[0]["authority_observed_at"] = invalid_time

    with pytest.raises(evidence.EvidenceValidationError, match="ready-preflight validity interval"):
        _validate(fixture)


def test_authority_observed_lifecycle_times_cannot_regress() -> None:
    fixture = _valid_fixture()
    regressed = "2026-07-17T00:01:02Z"
    fixture[0]["entries"][5]["cas"]["authority_observed_at"] = regressed
    fixture[1].ancestry[5]["authority_observed_at"] = regressed

    with pytest.raises(evidence.EvidenceValidationError, match="lifecycle time regressed"):
        _validate(fixture)


def test_every_successor_must_be_authority_observed_before_lease_expiry() -> None:
    fixture = _valid_fixture()
    expired = "2026-07-17T01:00:01Z"
    fixture[0]["entries"][-1]["cas"]["authority_observed_at"] = expired
    fixture[1].ancestry[-1]["authority_observed_at"] = expired

    with pytest.raises(evidence.EvidenceValidationError, match="after lease expiry"):
        _validate(fixture)


def test_authority_observed_time_is_cryptographically_seal_bound() -> None:
    fixture = _valid_fixture()
    changed = "2026-07-17T00:01:02.5Z"
    fixture[0]["entries"][2]["cas"]["authority_observed_at"] = changed
    fixture[1].ancestry[2]["authority_observed_at"] = changed

    with pytest.raises(evidence.EvidenceValidationError, match="seal_digest"):
        _validate(fixture)


def test_admission_requires_genesis_sentinel_intent_generation() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][0]["record"]["intent_generation"] = 99

    with pytest.raises(evidence.EvidenceValidationError, match="sentinel generation 0"):
        _validate(fixture)


def test_admission_intent_seal_must_equal_preflight_observed_lifecycle_seal() -> None:
    fixture = _valid_fixture()
    fixture[0]["entries"][0]["record"]["intent_seal_digest"] = _d("other-safe-root")

    with pytest.raises(evidence.EvidenceValidationError, match="seal observed by the ready preflight"):
        _validate(fixture)


def test_distinct_phases_cannot_reuse_external_idempotency_key() -> None:
    fixture = _valid_fixture()
    containment_key = fixture[0]["entries"][1]["record"]["external_idempotency_key"]
    fixture[0]["entries"][3]["record"]["external_idempotency_key"] = containment_key

    with pytest.raises(evidence.EvidenceValidationError, match="reuses an idempotency key"):
        _validate(fixture)


def test_gold_result_digest_must_match_exact_trusted_result() -> None:
    fixture = _valid_fixture()
    terminal = fixture[0]["entries"][-1]
    gold = next(item for item in terminal["evidence"] if item["type"] == "gold-result")
    gold["details"]["result_digest"] = _d("substituted-gold-result")

    with pytest.raises(evidence.EvidenceValidationError, match="exact trusted Gold result"):
        _validate(fixture)


def test_final_authority_digest_must_match_outer_launch_artifact() -> None:
    fixture = _valid_fixture()
    terminal = fixture[0]["entries"][-1]
    final_authority = next(
        item for item in terminal["evidence"] if item["type"] == "final-authority"
    )
    final_authority["details"]["authority_digest"] = _d(
        "substituted-launch-authority"
    )

    with pytest.raises(
        evidence.EvidenceValidationError,
        match="outer launch-authority artifact digest",
    ):
        _validate(fixture)


def test_protocol_version_rejects_float_equal_to_integer_version() -> None:
    fixture = _valid_fixture()
    fixture[0]["version"] = 2.0

    with pytest.raises(evidence.EvidenceValidationError, match="must be an integer"):
        _validate(fixture)


def test_database_result_must_point_to_typed_database_evidence() -> None:
    fixture = _valid_fixture()
    deployed = fixture[0]["entries"][4]
    candidate = next(item for item in deployed["evidence"] if item["type"] == "candidate-artifact")
    database_outcome = next(item for item in deployed["record"]["resource_outcomes"] if item["kind"] == "database")
    database_outcome["evidence_digest"] = candidate["artifact_digest"]

    with pytest.raises(evidence.EvidenceValidationError, match="cannot be proved by typed candidate-artifact"):
        _validate(fixture)


def test_terminal_resource_outcomes_bind_exact_closed_typed_artifacts() -> None:
    fixture = _valid_fixture()
    terminal = fixture[0]["entries"][-1]
    artifacts = {item["type"]: item["artifact_digest"] for item in terminal["evidence"]}

    assert {
        outcome["kind"]: next(
            evidence_type
            for evidence_type, artifact_digest in artifacts.items()
            if artifact_digest == outcome["evidence_digest"]
        )
        for outcome in terminal["record"]["resource_outcomes"]
    } == evidence.TERMINAL_RESOURCE_EVIDENCE


@pytest.mark.parametrize("resource_kind", evidence.RESOURCE_KINDS)
def test_terminal_resource_outcome_rejects_cross_kind_typed_artifact(resource_kind: str) -> None:
    fixture = _valid_fixture()
    terminal = fixture[0]["entries"][-1]
    artifacts = {item["type"]: item["artifact_digest"] for item in terminal["evidence"]}
    kinds = list(evidence.RESOURCE_KINDS)
    other_kind = kinds[(kinds.index(resource_kind) + 1) % len(kinds)]
    substituted_type = evidence.TERMINAL_RESOURCE_EVIDENCE[other_kind]
    outcome = next(item for item in terminal["record"]["resource_outcomes"] if item["kind"] == resource_kind)
    outcome["evidence_digest"] = artifacts[substituted_type]

    with pytest.raises(evidence.EvidenceValidationError, match="cannot be proved|must bind current typed"):
        _validate(fixture)


def test_signature_callback_is_mandatory_trust_not_manifest_claim() -> None:
    fixture = _valid_fixture()

    with pytest.raises(evidence.EvidenceValidationError, match="trusted signature verifier rejected"):
        evidence.validate_manifest(
            fixture[0],
            fixture[1],
            signature_verifier=lambda _payload, _signature: False,
            fsync_verifier=fixture[3],
        )


def test_closed_top_level_schema_rejects_unknown_field() -> None:
    fixture = _valid_fixture()
    fixture[0]["authority_override"] = True

    with pytest.raises(evidence.EvidenceValidationError, match="unknown=.*authority_override"):
        _validate(fixture)
