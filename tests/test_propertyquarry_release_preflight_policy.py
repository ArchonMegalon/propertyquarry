from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import MappingProxyType

import pytest

from scripts import propertyquarry_release_preflight_policy as policy


def _digest(label: str) -> str:
    return policy.digest_json({"identity": label})


def _expectations() -> policy.PreflightExpectations:
    candidate = _digest("candidate")
    release = _digest("release")
    image = _digest("image")
    controller = _digest("controller")
    root_policy = _digest("root-policy")
    workflow = _digest("workflow")

    subjects = {
        spec.check_id: policy.SubjectExpectation(
            kind=spec.subject_kind,
            digest=_digest(f"subject:{spec.check_id}"),
        )
        for spec in policy.CHECK_SPECS
    }
    exact_subjects = {
        "release-git-object": candidate,
        "immutable-image": image,
        "image-provenance": image,
        "image-sbom": image,
        "image-vulnerability-threshold": image,
        "dependency-integrity": release,
        "controller-binary": controller,
        "root-policy": root_policy,
        "workflow-identity": workflow,
        "rollback-readiness": release,
        "watchdog-readiness": controller,
    }
    for check_id, digest in exact_subjects.items():
        subjects[check_id] = replace(subjects[check_id], digest=digest)
    related_resources = {
        "database-identity": "database",
        "database-backup": "database",
        "database-recovery": "database",
        "database-migration-policy": "database",
        "public-origin-traffic": "traffic",
        "overlay": "overlay",
        "public-tour-volume-catalog": "public-tour",
        "monitoring-delivery-continuity": "monitoring-delivery",
        "capacity-readiness": "runtime",
    }
    for check_id, resource in related_resources.items():
        subjects[check_id] = replace(
            subjects[check_id],
            digest=subjects[f"resource-{resource}-readiness"].digest,
        )

    artifacts = {
        spec.check_id: policy.ArtifactExpectation(
            digest=_digest(f"artifact:{spec.check_id}"),
            size=1000 + index,
            media_type=spec.media_type,
        )
        for index, spec in enumerate(policy.CHECK_SPECS)
    }
    verifiers = {
        spec.check_id: policy.VerifierExpectation(
            identifier=f"root-verifier-{index}",
            digest=_digest(f"root-verifier:{index}"),
        )
        for index, spec in enumerate(policy.CHECK_SPECS)
    }
    issuers = {
        spec.check_id: policy.IssuerExpectation(
            identifier=f"external-issuer-{index}",
            digest=_digest(f"external-issuer:{index}"),
            key_id=f"release-key-{index}",
        )
        for index, spec in enumerate(policy.CHECK_SPECS)
    }
    return policy.PreflightExpectations(
        candidate_git_object_digest=candidate,
        release_git_object_digest=release,
        image_digest=image,
        controller_digest=controller,
        policy_digest=root_policy,
        workflow_identity_digest=workflow,
        clock_authority_id="trusted-clock-v1",
        clock_authority_digest=_digest("trusted-clock"),
        trusted_now=1_000_000,
        subjects=subjects,
        artifacts=artifacts,
        approved_verifiers=verifiers,
        approved_issuers=issuers,
    )


def _resign(receipt: dict[str, object]) -> None:
    unsigned = deepcopy(receipt)
    del unsigned["signature"]
    signature = receipt["signature"]
    assert type(signature) is dict
    signature["payload_digest"] = policy.digest_json(unsigned)


def _receipts(
    expectations: policy.PreflightExpectations,
) -> list[dict[str, object]]:
    bindings = {
        "candidate_git_object_digest": expectations.candidate_git_object_digest,
        "release_git_object_digest": expectations.release_git_object_digest,
        "image_digest": expectations.image_digest,
        "controller_digest": expectations.controller_digest,
        "policy_digest": expectations.policy_digest,
        "workflow_identity_digest": expectations.workflow_identity_digest,
    }
    receipts: list[dict[str, object]] = []
    for index, spec in enumerate(policy.CHECK_SPECS):
        artifact = expectations.artifacts[spec.check_id]
        verifier = expectations.approved_verifiers[spec.check_id]
        issuer = expectations.approved_issuers[spec.check_id]
        claims = dict(spec.pass_claims)
        if spec.resource_kind is not None:
            claims["mediator_digest"] = artifact.digest
        receipt: dict[str, object] = {
            "schema": policy.RECEIPT_SCHEMA,
            "version": policy.VERSION,
            "check_id": spec.check_id,
            "required_check_set_digest": policy.REQUIRED_CHECK_SET_DIGEST,
            "subject": {
                "kind": expectations.subjects[spec.check_id].kind,
                "digest": expectations.subjects[spec.check_id].digest,
            },
            "bindings": dict(bindings),
            "verifier": {
                "id": verifier.identifier,
                "digest": verifier.digest,
                "trust_domain": verifier.trust_domain,
            },
            "issuer": {
                "id": issuer.identifier,
                "digest": issuer.digest,
                "trust_domain": issuer.trust_domain,
            },
            "observation": {
                "authority_id": expectations.clock_authority_id,
                "authority_digest": expectations.clock_authority_digest,
                "observation_id": f"observation-{index}",
                "observed_at": expectations.trusted_now - 1,
                "valid_until": expectations.trusted_now + 100,
            },
            "artifact": {
                "digest": artifact.digest,
                "size": artifact.size,
                "media_type": artifact.media_type,
            },
            "dependencies": list(spec.dependencies),
            "status": "pass",
            "result": {
                "kind": "pass",
                "code": "verified",
                "detail_digest": artifact.digest,
            },
            "claims": claims,
            "signature": {
                "algorithm": issuer.signature_algorithm,
                "key_id": issuer.key_id,
                "value": "A" * 64,
                "payload_digest": _digest("placeholder"),
            },
        }
        _resign(receipt)
        receipts.append(receipt)
    return receipts


def _set_status(receipt: dict[str, object], status: str) -> None:
    check_id = receipt["check_id"]
    assert type(check_id) is str
    spec = policy.CHECK_SPEC_BY_ID[check_id]
    artifact = receipt["artifact"]
    assert type(artifact) is dict
    if status == "pass":
        code = "verified"
        claims = dict(spec.pass_claims)
        if spec.resource_kind is not None:
            claims["mediator_digest"] = artifact["digest"]
    elif status == "fail":
        code = spec.failure_code
        claims = {"failure_type": spec.failure_code}
    else:
        assert status == "indeterminate"
        code = spec.unavailable_code
        claims = {"unavailable_type": spec.unavailable_code}
    receipt["status"] = status
    receipt["result"] = {
        "kind": status,
        "code": code,
        "detail_digest": artifact["digest"],
    }
    receipt["claims"] = claims
    _resign(receipt)


def _signature_verifier(
    payload: bytes,
    signature: MappingProxyType[str, object],
    issuer: policy.IssuerExpectation,
) -> bool:
    assert policy.digest_json(json.loads(payload)) == signature["payload_digest"]
    assert signature["key_id"] == issuer.key_id
    return True


def _artifact_verifier(
    check_id: str,
    artifact: MappingProxyType[str, object],
    expected: policy.ArtifactExpectation,
) -> bool:
    assert check_id in policy.REQUIRED_CHECK_IDS
    assert artifact["digest"] == expected.digest
    assert artifact["size"] == expected.size
    assert artifact["media_type"] == expected.media_type
    return True


def _validate(
    receipts: object,
    expectations: policy.PreflightExpectations,
    *,
    signature_verifier=_signature_verifier,
    artifact_verifier=_artifact_verifier,
) -> policy.PreflightDecision:
    return policy.validate_preflight_receipts(
        receipts,
        expectations,
        signature_verifier=signature_verifier,
        artifact_verifier=artifact_verifier,
    )


def test_closed_contract_has_exact_28_checks_and_seven_resources() -> None:
    assert len(policy.REQUIRED_CHECK_IDS) == 28
    assert len(set(policy.REQUIRED_CHECK_IDS)) == 28
    assert policy.RESOURCE_KINDS == (
        "database",
        "launch-authority",
        "monitoring-delivery",
        "overlay",
        "public-tour",
        "runtime",
        "traffic",
    )
    positions = {check_id: index for index, check_id in enumerate(policy.REQUIRED_CHECK_IDS)}
    for spec in policy.CHECK_SPECS:
        assert all(positions[dependency] < positions[spec.check_id] for dependency in spec.dependencies)
    contract = policy.describe_contract()
    assert contract == {
        "schema": "propertyquarry.release-preflight-policy",
        "version": 2,
        "authoritative": False,
        "performs_effects": False,
        "required_check_count": 28,
        "resource_kinds": list(policy.RESOURCE_KINDS),
        "callback_contract": "external-trusted-pure-literal-true",
        "transport_contract": "external-strict-json-decoder-required",
    }


def test_required_check_document_is_fresh_and_digest_stable() -> None:
    document = policy.required_check_set_document()
    assert policy.digest_json(document) == policy.REQUIRED_CHECK_SET_DIGEST
    checks = document["checks"]
    assert type(checks) is list
    checks.clear()
    assert len(policy.required_check_set_document()["checks"]) == 28
    assert policy.digest_json(policy.required_check_set_document()) == policy.REQUIRED_CHECK_SET_DIGEST


def test_ready_receipt_set_is_fully_verified_and_digest_bound() -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    decision = _validate(receipts, expectations)
    assert decision.disposition == policy.READY
    assert decision.passed_checks == policy.REQUIRED_CHECK_IDS
    assert decision.failed_checks == ()
    assert decision.indeterminate_checks == ()
    assert decision.required_check_set_digest == policy.REQUIRED_CHECK_SET_DIGEST
    assert decision.receipt_set_digest == policy.digest_json(receipts)


@pytest.mark.parametrize(
    ("status", "disposition", "field"),
    [
        ("fail", policy.NOT_READY, "failed_checks"),
        ("indeterminate", policy.INDETERMINATE, "indeterminate_checks"),
    ],
)
def test_typed_terminal_statuses_produce_closed_dispositions(
    status: str, disposition: str, field: str
) -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    _set_status(receipts[-1], status)
    decision = _validate(receipts, expectations)
    assert decision.disposition == disposition
    assert getattr(decision, field) == ("watchdog-readiness",)


def test_pass_cannot_follow_a_nonpassing_dependency() -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    _set_status(receipts[0], "fail")
    with pytest.raises(policy.PreflightPolicyError, match="pass requires passing dependencies"):
        _validate(receipts, expectations)


def test_closed_set_rejects_missing_duplicate_unknown_and_reordered_receipts() -> None:
    expectations = _expectations()
    valid = _receipts(expectations)
    variants = [
        valid[:-1],
        valid + [deepcopy(valid[-1])],
        [deepcopy(valid[1]), deepcopy(valid[0]), *deepcopy(valid[2:])],
        deepcopy(valid),
    ]
    variants[-1][0]["check_id"] = "unknown-check"
    for receipts in variants:
        with pytest.raises(policy.PreflightPolicyError):
            _validate(receipts, expectations)


@pytest.mark.parametrize(
    "mutation",
    [
        "float-version",
        "bool-as-one",
        "false-as-zero",
        "extra-field",
        "wrong-binding",
        "wrong-subject",
        "wrong-artifact",
        "duplicate-observation",
    ],
)
def test_hostile_type_aliases_and_binding_mutations_fail_closed(mutation: str) -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    target = receipts[0]
    if mutation == "float-version":
        target["version"] = 2.0
    elif mutation == "bool-as-one":
        claims = target["claims"]
        assert type(claims) is dict
        claims["candidate_object_verified"] = 1
    elif mutation == "false-as-zero":
        target = receipts[4]
        claims = target["claims"]
        assert type(claims) is dict
        claims["blocking_high"] = False
    elif mutation == "extra-field":
        target["candidate_controlled"] = True
    elif mutation == "wrong-binding":
        bindings = target["bindings"]
        assert type(bindings) is dict
        bindings["image_digest"] = _digest("attacker-image")
    elif mutation == "wrong-subject":
        subject = target["subject"]
        assert type(subject) is dict
        subject["digest"] = _digest("attacker-subject")
    elif mutation == "wrong-artifact":
        artifact = target["artifact"]
        assert type(artifact) is dict
        artifact["size"] = artifact["size"] + 1
    else:
        first = receipts[0]["observation"]
        second = receipts[1]["observation"]
        assert type(first) is dict and type(second) is dict
        second["observation_id"] = first["observation_id"]
        target = receipts[1]
    _resign(target)
    with pytest.raises(policy.PreflightPolicyError):
        _validate(receipts, expectations)


@pytest.mark.parametrize("freshness", ["future", "stale", "oversized-window"])
def test_trusted_clock_freshness_is_bounded(freshness: str) -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    observation = receipts[0]["observation"]
    assert type(observation) is dict
    if freshness == "future":
        observation["observed_at"] = expectations.trusted_now + 1
    elif freshness == "stale":
        observation["valid_until"] = expectations.trusted_now - 1
    else:
        observation["observed_at"] = expectations.trusted_now - 1
        observation["valid_until"] = expectations.trusted_now + 600
    _resign(receipts[0])
    with pytest.raises(policy.PreflightPolicyError):
        _validate(receipts, expectations)


@pytest.mark.parametrize(
    "callback_kind",
    ["artifact-false", "artifact-one", "artifact-raises", "signature-false", "signature-one", "signature-raises"],
)
def test_callbacks_must_return_literal_true_and_exceptions_fail_closed(
    callback_kind: str,
) -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)

    def artifact(*args):  # type: ignore[no-untyped-def]
        if callback_kind == "artifact-raises":
            raise RuntimeError("boom")
        if callback_kind == "artifact-one":
            return 1
        return callback_kind != "artifact-false"

    def signature(*args):  # type: ignore[no-untyped-def]
        if callback_kind == "signature-raises":
            raise RuntimeError("boom")
        if callback_kind == "signature-one":
            return 1
        return callback_kind != "signature-false"

    with pytest.raises(policy.PreflightPolicyError):
        _validate(
            receipts,
            expectations,
            artifact_verifier=artifact,
            signature_verifier=signature,
        )


def test_callbacks_receive_immutable_views_and_cannot_change_decision_digest() -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    original = deepcopy(receipts)

    def artifact(check_id, value, expected):  # type: ignore[no-untyped-def]
        assert isinstance(value, MappingProxyType)
        with pytest.raises(TypeError):
            value["size"] = 0
        return True

    def signature(payload, value, issuer):  # type: ignore[no-untyped-def]
        assert isinstance(value, MappingProxyType)
        with pytest.raises(TypeError):
            value["value"] = "attacker"
        return True

    decision = _validate(
        receipts,
        expectations,
        artifact_verifier=artifact,
        signature_verifier=signature,
    )
    assert receipts == original
    assert decision.receipt_set_digest == policy.digest_json(original)


def test_expectation_maps_are_snapshotted_before_callbacks() -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    mutated = False

    def artifact(check_id, value, expected):  # type: ignore[no-untyped-def]
        nonlocal mutated
        if not mutated:
            expectations.subjects.clear()
            expectations.artifacts.clear()
            expectations.approved_verifiers.clear()
            expectations.approved_issuers.clear()
            mutated = True
        return True

    assert _validate(
        receipts,
        expectations,
        artifact_verifier=artifact,
    ).disposition == policy.READY


def test_invalid_expectation_mapping_fails_with_typed_policy_error() -> None:
    expectations = replace(_expectations(), subjects=None)  # type: ignore[arg-type]
    with pytest.raises(policy.PreflightPolicyError, match="strict mapping"):
        _validate([], expectations)


def test_global_trust_roles_cannot_collide_by_id_or_digest() -> None:
    base = _expectations()
    first, second = policy.REQUIRED_CHECK_IDS[:2]
    for field in ("identifier", "digest"):
        issuers = dict(base.approved_issuers)
        issuers[second] = replace(
            issuers[second],
            **{field: getattr(base.approved_verifiers[first], field)},
        )
        expectations = replace(base, approved_issuers=issuers)
        with pytest.raises(policy.PreflightPolicyError, match="globally distinct"):
            _validate(_receipts(expectations), expectations)


def test_bound_release_identity_cannot_be_its_own_trust_root() -> None:
    base = _expectations()
    first = policy.REQUIRED_CHECK_IDS[0]
    verifiers = dict(base.approved_verifiers)
    verifiers[first] = replace(
        verifiers[first], digest=base.candidate_git_object_digest
    )
    expectations = replace(base, approved_verifiers=verifiers)
    with pytest.raises(policy.PreflightPolicyError, match="self-attested"):
        _validate(_receipts(expectations), expectations)


def test_graph_validator_rejects_cycles_and_non_topological_order() -> None:
    cyclic = list(policy.CHECK_SPECS)
    cyclic[0] = replace(cyclic[0], dependencies=(cyclic[-1].check_id,))
    with pytest.raises(policy.PreflightPolicyError, match="topological"):
        policy._validate_check_graph(cyclic, policy.RESOURCE_KINDS)
    with pytest.raises(policy.PreflightPolicyError, match="topological"):
        policy._validate_check_graph(tuple(reversed(policy.CHECK_SPECS)), policy.RESOURCE_KINDS)


def test_decoded_non_json_types_depth_and_surrogates_fail_closed() -> None:
    expectations = _expectations()
    receipts = _receipts(expectations)
    variants = []

    float_value = deepcopy(receipts)
    float_value[0]["version"] = float("nan")
    variants.append(float_value)

    surrogate = deepcopy(receipts)
    surrogate[0]["check_id"] = "bad\ud800"
    variants.append(surrogate)

    too_deep = deepcopy(receipts)
    nested: object = "leaf"
    for _ in range(70):
        nested = [nested]
    too_deep[0]["claims"] = {"candidate_object_verified": nested}
    variants.append(too_deep)

    for variant in variants:
        with pytest.raises(policy.PreflightPolicyError):
            _validate(variant, expectations)
