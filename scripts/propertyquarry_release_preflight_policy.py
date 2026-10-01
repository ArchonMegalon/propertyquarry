#!/usr/bin/env python3
"""Non-authoritative PropertyQuarry lifecycle-v2 release-preflight model.

This module is an offline conformance model.  It cannot grant release
authority, verify a signature by itself, read a candidate checkout, execute a
security script, contact a service, or perform any production effect.  An
installed, root-owned supervisor must supply trusted expectations plus
signature and artifact-verification callbacks.  Both callbacks must return the
literal boolean ``True`` for every receipt.  They are required to be pure:
this model supplies immutable detached views, but cannot prevent a callback
from causing effects through external state.  Production must isolate those
callbacks behind authenticated, bounded, side-effect-free verifier services.

The API accepts already-decoded strict JSON objects.  Transport code must
reject invalid UTF-8, duplicate object keys, non-finite numbers, excessive
nested depth, and trailing data before calling this validator; a decoded
object cannot reveal duplicate keys erased by a permissive parser.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Callable, Mapping, Sequence


SCHEMA = "propertyquarry.release-preflight-policy"
VERSION = 2
RECEIPT_SCHEMA = "propertyquarry.release-preflight-check-receipt"
REQUIRED_CHECK_SET_SCHEMA = "propertyquarry.release-preflight-required-check-set"
AUTHORITATIVE = False
PERFORMS_EFFECTS = False

READY = "ready"
NOT_READY = "not-ready"
INDETERMINATE = "indeterminate"

RESOURCE_KINDS = (
    "database",
    "launch-authority",
    "monitoring-delivery",
    "overlay",
    "public-tour",
    "runtime",
    "traffic",
)

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_MEDIA_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_SIGNATURE_RE = re.compile(r"[A-Za-z0-9_-]{16,8192}={0,2}\Z")
_MAX_INT64 = 2**63 - 1
_MAX_JSON_DEPTH = 64
_EXACT_CHECK_COUNT = 28


class PreflightPolicyError(ValueError):
    """A deterministic fail-closed policy rejection."""


def _fail(path: str, message: str) -> None:
    raise PreflightPolicyError(f"{path}: {message}")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def digest_json(value: object) -> str:
    """Return the deterministic SHA-256 digest of a JSON-shaped value."""

    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _exact_json_equal(actual: object, expected: object) -> bool:
    """Compare JSON values without Python's bool/int or int/float aliases."""

    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(
            _exact_json_equal(actual[key], expected[key]) for key in expected
        )
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _exact_json_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _strict_json_copy(value: object, path: str, *, depth: int = 0) -> object:
    """Detach an exact JSON value so callbacks cannot mutate caller input."""

    if depth > _MAX_JSON_DEPTH:
        _fail(path, "exceeds maximum decoded JSON depth")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            _fail(path, "must not contain surrogate code points")
        return value
    if type(value) is list:
        return [
            _strict_json_copy(item, f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            _fail(path, "object keys must be strings")
        return {
            key: _strict_json_copy(item, f"{path}.{key}", depth=depth + 1)
            for key, item in value.items()
        }
    _fail(path, "must contain exact JSON types")


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    subject_kind: str
    media_type: str
    dependencies: tuple[str, ...]
    maximum_age_seconds: int
    pass_claims: tuple[tuple[str, object], ...]
    failure_code: str
    unavailable_code: str
    resource_kind: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "subject_kind": self.subject_kind,
            "media_type": self.media_type,
            "dependencies": list(self.dependencies),
            "maximum_age_seconds": self.maximum_age_seconds,
            "pass_claims": dict(self.pass_claims),
            "failure_code": self.failure_code,
            "unavailable_code": self.unavailable_code,
            "resource_kind": self.resource_kind,
        }


def _spec(
    check_id: str,
    subject_kind: str,
    media_suffix: str,
    dependencies: Sequence[str] = (),
    *,
    pass_claims: Mapping[str, object],
    resource_kind: str | None = None,
    maximum_age_seconds: int = 600,
) -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        subject_kind=subject_kind,
        media_type=f"application/vnd.propertyquarry.{media_suffix}+json",
        dependencies=tuple(dependencies),
        maximum_age_seconds=maximum_age_seconds,
        pass_claims=tuple(pass_claims.items()),
        failure_code=f"{check_id}-failed",
        unavailable_code=f"{check_id}-evidence-unavailable",
        resource_kind=resource_kind,
    )


_BASE_SPECS = (
    _spec(
        "release-git-object",
        "candidate",
        "release-git-object",
        pass_claims={"candidate_object_verified": True, "release_object_verified": True},
    ),
    _spec(
        "immutable-image",
        "image",
        "immutable-image",
        ("release-git-object",),
        pass_claims={"digest_pull_verified": True, "mutable_tag_rejected": True, "independent_build": True},
    ),
    _spec(
        "image-provenance",
        "image",
        "image-provenance",
        ("release-git-object", "immutable-image"),
        pass_claims={"builder_approved": True, "source_matches": True, "materials_verified": True},
    ),
    _spec(
        "image-sbom",
        "image",
        "image-sbom",
        ("immutable-image",),
        pass_claims={"complete": True, "format": "spdx-json", "image_matches": True},
    ),
    _spec(
        "image-vulnerability-threshold",
        "image",
        "image-vulnerability-threshold",
        ("image-provenance", "image-sbom"),
        pass_claims={"blocking_high": 0, "blocking_critical": 0, "threshold": "zero-high-critical"},
    ),
    _spec(
        "dependency-integrity",
        "release",
        "dependency-integrity",
        ("release-git-object",),
        pass_claims={"lockfiles_verified": True, "resolved_tree_verified": True, "unapproved_sources": 0},
    ),
    _spec(
        "controller-binary",
        "controller",
        "controller-binary",
        pass_claims={"installed_root_owned": True, "candidate_executable": False, "binary_verified": True},
    ),
    _spec(
        "root-policy",
        "root-policy",
        "root-policy",
        ("controller-binary",),
        pass_claims={"installed_root_owned": True, "candidate_mutable": False, "policy_verified": True},
    ),
    _spec(
        "workflow-identity",
        "workflow",
        "workflow-identity",
        ("controller-binary", "root-policy"),
        pass_claims={"identity_verified": True, "identity_mode": "blob-or-commit", "workflow_matches": True},
    ),
    _spec(
        "lifecycle-head",
        "lifecycle-head",
        "lifecycle-head",
        ("root-policy", "workflow-identity"),
        pass_claims={"safe_head": True, "pending_reconciliation": False, "active_writer": False},
    ),
)


def _resource_spec(resource_kind: str) -> CheckSpec:
    return _spec(
        f"resource-{resource_kind}-readiness",
        f"resource:{resource_kind}",
        "resource-mediator-readiness",
        ("lifecycle-head", "root-policy"),
        pass_claims={
            "resource_kind": resource_kind,
            "mediator_ready": True,
            "global_fence_ready": True,
            "resource_fence_ready": True,
            "stale_fence_rejected": True,
        },
        resource_kind=resource_kind,
    )


_RESOURCE_SPECS = tuple(_resource_spec(resource) for resource in RESOURCE_KINDS)

_TAIL_SPECS = (
    _spec(
        "database-identity",
        "resource:database",
        "database-identity",
        ("resource-database-readiness",),
        pass_claims={"target_identity_verified": True, "database_identity_verified": True},
    ),
    _spec(
        "database-backup",
        "resource:database",
        "database-backup",
        ("database-identity",),
        pass_claims={"backup_verified": True, "recovery_point_available": True, "target_bound": True},
    ),
    _spec(
        "database-recovery",
        "resource:database",
        "database-recovery",
        ("database-backup",),
        pass_claims={"restore_tested": True, "recovery_target_bound": True, "restored_identity_verified": True},
    ),
    _spec(
        "database-migration-policy",
        "resource:database",
        "database-migration-policy",
        ("database-recovery", "dependency-integrity"),
        pass_claims={
            "destructive_pending": False,
            "safe_outcomes": "unchanged|forward-compatible|restored-verified",
            "rollback_policy_verified": True,
        },
    ),
    _spec(
        "public-origin-traffic",
        "resource:traffic",
        "public-origin-traffic",
        ("resource-traffic-readiness", "resource-runtime-readiness", "immutable-image"),
        pass_claims={"https_origin_verified": True, "public_identity_verified": True, "traffic_control_ready": True},
    ),
    _spec(
        "overlay",
        "resource:overlay",
        "overlay-readiness",
        ("resource-overlay-readiness", "public-origin-traffic"),
        pass_claims={"cas_ready": True, "active_identity_verified": True, "rollback_state_verified": True},
    ),
    _spec(
        "public-tour-volume-catalog",
        "resource:public-tour",
        "public-tour-volume-catalog",
        ("resource-public-tour-readiness", "public-origin-traffic"),
        pass_claims={"volume_verified": True, "catalog_verified": True, "target_binding_verified": True},
    ),
    _spec(
        "monitoring-delivery-continuity",
        "resource:monitoring-delivery",
        "monitoring-delivery-continuity",
        ("resource-monitoring-delivery-readiness", "public-origin-traffic"),
        pass_claims={"monitoring_ready": True, "delivery_continuity_verified": True, "alert_path_verified": True},
    ),
    _spec(
        "capacity-readiness",
        "resource:runtime",
        "capacity-readiness",
        ("resource-runtime-readiness", "monitoring-delivery-continuity"),
        pass_claims={"capacity_ready": True, "headroom_verified": True, "limits_verified": True},
    ),
    _spec(
        "rollback-readiness",
        "release",
        "rollback-readiness",
        (
            "database-migration-policy",
            "public-origin-traffic",
            "overlay",
            "public-tour-volume-catalog",
            "monitoring-delivery-continuity",
            "capacity-readiness",
        ),
        pass_claims={"rollback_ready": True, "rollback_artifact_verified": True, "all_resources_covered": True},
    ),
    _spec(
        "watchdog-readiness",
        "controller",
        "watchdog-readiness",
        ("controller-binary", "lifecycle-head", "rollback-readiness"),
        pass_claims={"watchdog_ready": True, "kill_reap_ready": True, "lease_expiry_containment_ready": True},
    ),
)

CHECK_SPECS = _BASE_SPECS + _RESOURCE_SPECS + _TAIL_SPECS
REQUIRED_CHECK_IDS = tuple(spec.check_id for spec in CHECK_SPECS)


def _validate_check_graph(
    specs: Sequence[CheckSpec], resource_kinds: Sequence[str]
) -> None:
    """Validate the closed check graph, including topological dependency order."""

    if type(specs) not in {tuple, list} or len(specs) != _EXACT_CHECK_COUNT:
        _fail("check-set", f"must contain exactly {_EXACT_CHECK_COUNT} checks")
    if type(resource_kinds) not in {tuple, list} or tuple(resource_kinds) != RESOURCE_KINDS:
        _fail("resource-kinds", "must match the exact ordered seven-resource set")
    if len(resource_kinds) != 7 or len(set(resource_kinds)) != 7:
        _fail("resource-kinds", "must contain exactly seven unique resource kinds")

    seen: set[str] = set()
    declared_resources: list[str] = []
    for index, spec in enumerate(specs):
        path = f"check-set[{index}]"
        if type(spec) is not CheckSpec:
            _fail(path, "must be CheckSpec")
        if type(spec.check_id) is not str or not _ID_RE.fullmatch(spec.check_id):
            _fail(f"{path}.check_id", "must be a bounded identifier")
        if spec.check_id in seen:
            _fail(f"{path}.check_id", "duplicate check identifier")
        if type(spec.dependencies) is not tuple or len(set(spec.dependencies)) != len(spec.dependencies):
            _fail(f"{path}.dependencies", "must be a unique dependency tuple")
        for dependency in spec.dependencies:
            if type(dependency) is not str or dependency not in seen:
                _fail(
                    f"{path}.dependencies",
                    "dependencies must exist earlier in topological order",
                )
        if type(spec.maximum_age_seconds) is not int or not 0 < spec.maximum_age_seconds <= _MAX_INT64:
            _fail(f"{path}.maximum_age_seconds", "must be a positive integer")
        if spec.resource_kind is not None:
            if spec.resource_kind not in resource_kinds:
                _fail(f"{path}.resource_kind", "is outside the closed resource set")
            declared_resources.append(spec.resource_kind)
        seen.add(spec.check_id)
    if tuple(declared_resources) != tuple(resource_kinds):
        _fail("check-set", "must declare one readiness check per resource in exact order")


_validate_check_graph(CHECK_SPECS, RESOURCE_KINDS)
CHECK_SPEC_BY_ID = MappingProxyType({spec.check_id: spec for spec in CHECK_SPECS})


def required_check_set_document() -> dict[str, object]:
    """Return a fresh JSON-shaped copy of the closed lifecycle-v2 check set."""

    return {
        "schema": REQUIRED_CHECK_SET_SCHEMA,
        "version": VERSION,
        "checks": [spec.document() for spec in CHECK_SPECS],
    }


REQUIRED_CHECK_SET_DIGEST = digest_json(required_check_set_document())


@dataclass(frozen=True)
class SubjectExpectation:
    kind: str
    digest: str


@dataclass(frozen=True)
class ArtifactExpectation:
    digest: str
    size: int
    media_type: str


@dataclass(frozen=True)
class VerifierExpectation:
    identifier: str
    digest: str
    trust_domain: str = "root-installed-verifier"


@dataclass(frozen=True)
class IssuerExpectation:
    identifier: str
    digest: str
    key_id: str
    signature_algorithm: str = "ed25519"
    trust_domain: str = "external-release-authority"


@dataclass(frozen=True)
class PreflightExpectations:
    candidate_git_object_digest: str
    release_git_object_digest: str
    image_digest: str
    controller_digest: str
    policy_digest: str
    workflow_identity_digest: str
    clock_authority_id: str
    clock_authority_digest: str
    trusted_now: int
    subjects: Mapping[str, SubjectExpectation]
    artifacts: Mapping[str, ArtifactExpectation]
    approved_verifiers: Mapping[str, VerifierExpectation]
    approved_issuers: Mapping[str, IssuerExpectation]


@dataclass(frozen=True)
class PreflightDecision:
    disposition: str
    required_check_set_digest: str
    receipt_set_digest: str
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    indeterminate_checks: tuple[str, ...]


SignatureVerifier = Callable[[bytes, Mapping[str, object], IssuerExpectation], object]
ArtifactVerifier = Callable[[str, Mapping[str, object], ArtifactExpectation], object]


def _object(value: object, path: str, keys: set[str] | frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        _fail(path, "must be a strict JSON object")
    if not all(type(key) is str for key in value):
        _fail(path, "object keys must be strings")
    actual = set(value)
    if actual != set(keys):
        missing = sorted(set(keys) - actual)
        unknown = sorted(actual - set(keys))
        _fail(path, f"closed schema mismatch; missing={missing}, unknown={unknown}")
    return value


def _array(value: object, path: str) -> list[object]:
    if type(value) is not list:
        _fail(path, "must be a strict JSON array")
    return value


def _string(value: object, path: str, *, maximum: int = 8192) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(path, "must be a bounded non-empty string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(path, "must not contain surrogate code points")
    return value


def _identifier(value: object, path: str) -> str:
    text = _string(value, path, maximum=256)
    if not _ID_RE.fullmatch(text):
        _fail(path, "must be a bounded identifier")
    return text


def _digest(value: object, path: str) -> str:
    text = _string(value, path, maximum=71)
    if not _DIGEST_RE.fullmatch(text):
        _fail(path, "must be a lowercase sha256 digest")
    return text


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > _MAX_INT64:
        _fail(path, f"must be an integer in [{minimum}, 2^63-1]")
    return value


def _validate_expectation_keys(value: Mapping[str, object], path: str) -> None:
    if (
        type(value) is not dict
        or not all(type(key) is str for key in value)
        or set(value) != set(REQUIRED_CHECK_IDS)
    ):
        _fail(path, "must contain exactly the versioned required check IDs")


def _validate_expectations(expectations: PreflightExpectations) -> None:
    if type(expectations) is not PreflightExpectations:
        _fail("expectations", "must be PreflightExpectations")
    core_digests = (
        expectations.candidate_git_object_digest,
        expectations.release_git_object_digest,
        expectations.image_digest,
        expectations.controller_digest,
        expectations.policy_digest,
        expectations.workflow_identity_digest,
        expectations.clock_authority_digest,
    )
    for index, value in enumerate(core_digests):
        _digest(value, f"expectations.core[{index}]")
    _identifier(expectations.clock_authority_id, "expectations.clock_authority_id")
    _integer(expectations.trusted_now, "expectations.trusted_now")
    _validate_expectation_keys(expectations.subjects, "expectations.subjects")
    _validate_expectation_keys(expectations.artifacts, "expectations.artifacts")
    _validate_expectation_keys(expectations.approved_verifiers, "expectations.approved_verifiers")
    _validate_expectation_keys(expectations.approved_issuers, "expectations.approved_issuers")

    if expectations.clock_authority_digest in set(core_digests[:-1]):
        _fail("expectations.clock_authority_digest", "must be distinct from bound release identities")
    bound_identity_digests = set(core_digests)
    verifier_identifiers: set[str] = set()
    verifier_digests: set[str] = set()
    issuer_identifiers: set[str] = set()
    issuer_digests: set[str] = set()
    for check_id in REQUIRED_CHECK_IDS:
        spec = CHECK_SPEC_BY_ID[check_id]
        subject = expectations.subjects[check_id]
        if type(subject) is not SubjectExpectation:
            _fail(f"expectations.subjects.{check_id}", "must be SubjectExpectation")
        subject_kind = _identifier(subject.kind, f"expectations.subjects.{check_id}.kind")
        if subject_kind != spec.subject_kind:
            _fail(f"expectations.subjects.{check_id}", "subject kind does not match the closed check set")
        _digest(subject.digest, f"expectations.subjects.{check_id}.digest")
        artifact = expectations.artifacts[check_id]
        if type(artifact) is not ArtifactExpectation:
            _fail(f"expectations.artifacts.{check_id}", "must be ArtifactExpectation")
        _digest(artifact.digest, f"expectations.artifacts.{check_id}.digest")
        _integer(artifact.size, f"expectations.artifacts.{check_id}.size", minimum=1)
        artifact_media_type = _string(
            artifact.media_type,
            f"expectations.artifacts.{check_id}.media_type",
            maximum=255,
        )
        if artifact_media_type != spec.media_type or not _MEDIA_RE.fullmatch(artifact_media_type):
            _fail(f"expectations.artifacts.{check_id}.media_type", "does not match the closed check media type")
        verifier = expectations.approved_verifiers[check_id]
        if type(verifier) is not VerifierExpectation:
            _fail(f"expectations.approved_verifiers.{check_id}", "must be VerifierExpectation")
        _identifier(verifier.identifier, f"expectations.approved_verifiers.{check_id}.identifier")
        _digest(verifier.digest, f"expectations.approved_verifiers.{check_id}.digest")
        verifier_trust_domain = _identifier(
            verifier.trust_domain,
            f"expectations.approved_verifiers.{check_id}.trust_domain",
        )
        if verifier_trust_domain != "root-installed-verifier":
            _fail(f"expectations.approved_verifiers.{check_id}.trust_domain", "must be root-installed-verifier")
        issuer = expectations.approved_issuers[check_id]
        if type(issuer) is not IssuerExpectation:
            _fail(f"expectations.approved_issuers.{check_id}", "must be IssuerExpectation")
        _identifier(issuer.identifier, f"expectations.approved_issuers.{check_id}.identifier")
        _identifier(issuer.key_id, f"expectations.approved_issuers.{check_id}.key_id")
        _digest(issuer.digest, f"expectations.approved_issuers.{check_id}.digest")
        issuer_trust_domain = _identifier(
            issuer.trust_domain,
            f"expectations.approved_issuers.{check_id}.trust_domain",
        )
        signature_algorithm = _identifier(
            issuer.signature_algorithm,
            f"expectations.approved_issuers.{check_id}.signature_algorithm",
        )
        if issuer_trust_domain != "external-release-authority" or signature_algorithm != "ed25519":
            _fail(f"expectations.approved_issuers.{check_id}", "issuer trust domain or algorithm is not approved")
        if verifier.identifier == issuer.identifier or verifier.digest == issuer.digest:
            _fail(f"expectations.{check_id}", "verifier and issuer must be distinct")
        if verifier.digest in bound_identity_digests or issuer.digest in bound_identity_digests:
            _fail(f"expectations.{check_id}", "self-attested verifier or issuer identity is forbidden")
        if verifier.identifier == expectations.clock_authority_id or issuer.identifier == expectations.clock_authority_id:
            _fail(f"expectations.{check_id}", "clock, verifier, and issuer identities must be distinct")
        verifier_identifiers.add(verifier.identifier)
        verifier_digests.add(verifier.digest)
        issuer_identifiers.add(issuer.identifier)
        issuer_digests.add(issuer.digest)

    if (verifier_identifiers & issuer_identifiers) or (verifier_digests & issuer_digests):
        _fail("expectations", "verifier and issuer trust roots must be globally distinct")

    subjects = expectations.subjects
    exact_subjects = {
        "release-git-object": expectations.candidate_git_object_digest,
        "immutable-image": expectations.image_digest,
        "image-provenance": expectations.image_digest,
        "image-sbom": expectations.image_digest,
        "image-vulnerability-threshold": expectations.image_digest,
        "dependency-integrity": expectations.release_git_object_digest,
        "controller-binary": expectations.controller_digest,
        "root-policy": expectations.policy_digest,
        "workflow-identity": expectations.workflow_identity_digest,
        "rollback-readiness": expectations.release_git_object_digest,
        "watchdog-readiness": expectations.controller_digest,
    }
    for check_id, expected_digest in exact_subjects.items():
        if subjects[check_id].digest != expected_digest:
            _fail(f"expectations.subjects.{check_id}.digest", "does not match its bound release identity")
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
        resource_check = f"resource-{resource}-readiness"
        if subjects[check_id].digest != subjects[resource_check].digest:
            _fail(f"expectations.subjects.{check_id}.digest", "does not match the resource readiness subject")


def _snapshot_expectations(expectations: PreflightExpectations) -> PreflightExpectations:
    """Detach mutable expectation maps before invoking any external callback."""

    expectation_maps = {
        "subjects": expectations.subjects,
        "artifacts": expectations.artifacts,
        "approved_verifiers": expectations.approved_verifiers,
        "approved_issuers": expectations.approved_issuers,
    }
    for name, value in expectation_maps.items():
        if type(value) is not dict:
            _fail(f"expectations.{name}", "must be a strict mapping")

    return PreflightExpectations(
        candidate_git_object_digest=expectations.candidate_git_object_digest,
        release_git_object_digest=expectations.release_git_object_digest,
        image_digest=expectations.image_digest,
        controller_digest=expectations.controller_digest,
        policy_digest=expectations.policy_digest,
        workflow_identity_digest=expectations.workflow_identity_digest,
        clock_authority_id=expectations.clock_authority_id,
        clock_authority_digest=expectations.clock_authority_digest,
        trusted_now=expectations.trusted_now,
        subjects=dict(expectation_maps["subjects"]),
        artifacts=dict(expectation_maps["artifacts"]),
        approved_verifiers=dict(expectation_maps["approved_verifiers"]),
        approved_issuers=dict(expectation_maps["approved_issuers"]),
    )


def _identity_object(
    value: object,
    path: str,
    expected: VerifierExpectation | IssuerExpectation,
) -> dict[str, object]:
    obj = _object(value, path, {"id", "digest", "trust_domain"})
    if _identifier(obj["id"], f"{path}.id") != expected.identifier:
        _fail(f"{path}.id", "does not match the approved identity")
    if _digest(obj["digest"], f"{path}.digest") != expected.digest:
        _fail(f"{path}.digest", "does not match the approved identity")
    if _identifier(obj["trust_domain"], f"{path}.trust_domain") != expected.trust_domain:
        _fail(f"{path}.trust_domain", "does not match the approved trust domain")
    return obj


def _expected_pass_claims(spec: CheckSpec, artifact_digest: str) -> dict[str, object]:
    claims = dict(spec.pass_claims)
    if spec.resource_kind is not None:
        claims["mediator_digest"] = artifact_digest
    return claims


def _validate_receipt(
    receipt: object,
    index: int,
    expectations: PreflightExpectations,
    signature_verifier: SignatureVerifier,
    artifact_verifier: ArtifactVerifier,
    observation_ids: set[str],
    prior_statuses: Mapping[str, str],
) -> tuple[str, str]:
    path = f"receipts[{index}]"
    obj = _object(
        receipt,
        path,
        {
            "schema",
            "version",
            "check_id",
            "required_check_set_digest",
            "subject",
            "bindings",
            "verifier",
            "issuer",
            "observation",
            "artifact",
            "dependencies",
            "status",
            "result",
            "claims",
            "signature",
        },
    )
    schema = _string(obj["schema"], f"{path}.schema", maximum=128)
    if schema != RECEIPT_SCHEMA or type(obj["version"]) is not int or obj["version"] != VERSION:
        _fail(path, "receipt schema or version mismatch")
    check_id = _identifier(obj["check_id"], f"{path}.check_id")
    expected_id = REQUIRED_CHECK_IDS[index]
    if check_id != expected_id:
        _fail(f"{path}.check_id", f"expected closed check {expected_id}")
    spec = CHECK_SPEC_BY_ID[check_id]
    if _digest(obj["required_check_set_digest"], f"{path}.required_check_set_digest") != REQUIRED_CHECK_SET_DIGEST:
        _fail(f"{path}.required_check_set_digest", "required check-set digest mismatch")

    subject = _object(obj["subject"], f"{path}.subject", {"kind", "digest"})
    expected_subject = expectations.subjects[check_id]
    if _identifier(subject["kind"], f"{path}.subject.kind") != expected_subject.kind:
        _fail(f"{path}.subject.kind", "subject kind mismatch")
    if _digest(subject["digest"], f"{path}.subject.digest") != expected_subject.digest:
        _fail(f"{path}.subject.digest", "subject digest mismatch")

    bindings = _object(
        obj["bindings"],
        f"{path}.bindings",
        {
            "candidate_git_object_digest",
            "release_git_object_digest",
            "image_digest",
            "controller_digest",
            "policy_digest",
            "workflow_identity_digest",
        },
    )
    expected_bindings = {
        "candidate_git_object_digest": expectations.candidate_git_object_digest,
        "release_git_object_digest": expectations.release_git_object_digest,
        "image_digest": expectations.image_digest,
        "controller_digest": expectations.controller_digest,
        "policy_digest": expectations.policy_digest,
        "workflow_identity_digest": expectations.workflow_identity_digest,
    }
    for name, expected in expected_bindings.items():
        if _digest(bindings[name], f"{path}.bindings.{name}") != expected:
            _fail(f"{path}.bindings.{name}", "binding mismatch")

    verifier = expectations.approved_verifiers[check_id]
    issuer = expectations.approved_issuers[check_id]
    _identity_object(obj["verifier"], f"{path}.verifier", verifier)
    _identity_object(obj["issuer"], f"{path}.issuer", issuer)

    observation = _object(
        obj["observation"],
        f"{path}.observation",
        {"authority_id", "authority_digest", "observation_id", "observed_at", "valid_until"},
    )
    if _identifier(observation["authority_id"], f"{path}.observation.authority_id") != expectations.clock_authority_id:
        _fail(f"{path}.observation.authority_id", "trusted clock authority mismatch")
    if _digest(observation["authority_digest"], f"{path}.observation.authority_digest") != expectations.clock_authority_digest:
        _fail(f"{path}.observation.authority_digest", "trusted clock authority digest mismatch")
    observation_id = _identifier(observation["observation_id"], f"{path}.observation.observation_id")
    if observation_id in observation_ids:
        _fail(f"{path}.observation.observation_id", "duplicate observation identity")
    observation_ids.add(observation_id)
    observed_at = _integer(observation["observed_at"], f"{path}.observation.observed_at")
    valid_until = _integer(observation["valid_until"], f"{path}.observation.valid_until")
    if observed_at > expectations.trusted_now:
        _fail(f"{path}.observation.observed_at", "future observation")
    if valid_until < expectations.trusted_now:
        _fail(f"{path}.observation.valid_until", "stale observation")
    if expectations.trusted_now - observed_at > spec.maximum_age_seconds:
        _fail(f"{path}.observation.observed_at", "observation exceeds maximum age")
    if valid_until < observed_at or valid_until - observed_at > spec.maximum_age_seconds:
        _fail(f"{path}.observation.valid_until", "freshness window exceeds policy")

    artifact = _object(obj["artifact"], f"{path}.artifact", {"digest", "size", "media_type"})
    expected_artifact = expectations.artifacts[check_id]
    if _digest(artifact["digest"], f"{path}.artifact.digest") != expected_artifact.digest:
        _fail(f"{path}.artifact.digest", "artifact digest mismatch")
    if _integer(artifact["size"], f"{path}.artifact.size", minimum=1) != expected_artifact.size:
        _fail(f"{path}.artifact.size", "artifact size mismatch")
    if _string(artifact["media_type"], f"{path}.artifact.media_type", maximum=255) != expected_artifact.media_type:
        _fail(f"{path}.artifact.media_type", "artifact media type mismatch")

    dependencies = _array(obj["dependencies"], f"{path}.dependencies")
    parsed_dependencies = tuple(_identifier(item, f"{path}.dependencies[{offset}]") for offset, item in enumerate(dependencies))
    if len(set(parsed_dependencies)) != len(parsed_dependencies):
        _fail(f"{path}.dependencies", "duplicate dependency")
    if parsed_dependencies != spec.dependencies:
        _fail(f"{path}.dependencies", "dependencies do not match the closed check set")

    status = _identifier(obj["status"], f"{path}.status")
    if status not in {"pass", "fail", "indeterminate"}:
        _fail(f"{path}.status", "must be pass, fail, or indeterminate")
    if status == "pass":
        nonpassing_dependencies = [
            dependency
            for dependency in parsed_dependencies
            if prior_statuses.get(dependency) != "pass"
        ]
        if nonpassing_dependencies:
            _fail(
                f"{path}.dependencies",
                f"pass requires passing dependencies; nonpassing={nonpassing_dependencies}",
            )
    result = _object(obj["result"], f"{path}.result", {"kind", "code", "detail_digest"})
    if _identifier(result["kind"], f"{path}.result.kind") != status:
        _fail(f"{path}.result.kind", "must equal receipt status")
    expected_code = {
        "pass": "verified",
        "fail": spec.failure_code,
        "indeterminate": spec.unavailable_code,
    }[status]
    if _identifier(result["code"], f"{path}.result.code") != expected_code:
        _fail(f"{path}.result.code", "is not the typed result code for this check")
    if _digest(result["detail_digest"], f"{path}.result.detail_digest") != expected_artifact.digest:
        _fail(f"{path}.result.detail_digest", "must bind the verified artifact")

    if status == "pass":
        expected_claims = _expected_pass_claims(spec, expected_artifact.digest)
    elif status == "fail":
        expected_claims = {"failure_type": spec.failure_code}
    else:
        expected_claims = {"unavailable_type": spec.unavailable_code}
    claims = _object(obj["claims"], f"{path}.claims", set(expected_claims))
    if not _exact_json_equal(claims, expected_claims):
        _fail(f"{path}.claims", "does not match the closed typed claims for status")

    signature = _object(
        obj["signature"],
        f"{path}.signature",
        {"algorithm", "key_id", "value", "payload_digest"},
    )
    algorithm = _identifier(signature["algorithm"], f"{path}.signature.algorithm")
    key_id = _identifier(signature["key_id"], f"{path}.signature.key_id")
    if algorithm != issuer.signature_algorithm or key_id != issuer.key_id:
        _fail(f"{path}.signature", "signature identity mismatch")
    value = _string(signature["value"], f"{path}.signature.value")
    if not _SIGNATURE_RE.fullmatch(value):
        _fail(f"{path}.signature.value", "must be bounded base64url signature text")
    unsigned = dict(obj)
    del unsigned["signature"]
    payload = _canonical_bytes(unsigned)
    payload_digest = digest_json(unsigned)
    if _digest(signature["payload_digest"], f"{path}.signature.payload_digest") != payload_digest:
        _fail(f"{path}.signature.payload_digest", "does not bind the canonical unsigned receipt")

    try:
        artifact_verified = artifact_verifier(
            check_id,
            MappingProxyType(dict(artifact)),
            expected_artifact,
        )
    except Exception:
        _fail(f"{path}.artifact", "artifact verifier raised")
    if artifact_verified is not True:
        _fail(f"{path}.artifact", "artifact verifier must return literal True")
    try:
        signature_verified = signature_verifier(
            payload,
            MappingProxyType(dict(signature)),
            issuer,
        )
    except Exception:
        _fail(f"{path}.signature", "signature verifier raised")
    if signature_verified is not True:
        _fail(f"{path}.signature", "signature verifier must return literal True")
    return check_id, status


def validate_preflight_receipts(
    receipts: object,
    expectations: PreflightExpectations,
    *,
    signature_verifier: SignatureVerifier,
    artifact_verifier: ArtifactVerifier,
) -> PreflightDecision:
    """Validate the complete closed receipt set and return its disposition.

    ``ready`` requires every exact check to carry a verified ``pass`` result.
    A typed ``fail`` produces ``not-ready``.  In the absence of failures, a
    typed ``indeterminate`` (trusted evidence unavailable) produces
    ``indeterminate``.  Any structural, trust, binding, freshness, callback,
    or closed-set error raises :class:`PreflightPolicyError` instead.
    """

    if type(expectations) is not PreflightExpectations:
        _fail("expectations", "must be PreflightExpectations")
    expectations = _snapshot_expectations(expectations)
    _validate_expectations(expectations)
    if not callable(signature_verifier) or not callable(artifact_verifier):
        _fail("callbacks", "signature and artifact verifier callbacks are mandatory")
    items = _array(_strict_json_copy(receipts, "receipts"), "receipts")
    receipt_set_digest = digest_json(items)
    identifiers: list[str] = []
    for index, item in enumerate(items):
        if type(item) is not dict or type(item.get("check_id")) is not str:
            _fail(f"receipts[{index}].check_id", "missing or invalid check identifier")
        identifiers.append(item["check_id"])
    if len(set(identifiers)) != len(identifiers):
        _fail("receipts", "duplicate check receipt")
    unknown = sorted(set(identifiers) - set(REQUIRED_CHECK_IDS))
    missing = sorted(set(REQUIRED_CHECK_IDS) - set(identifiers))
    if unknown or missing:
        _fail("receipts", f"closed check-set mismatch; missing={missing}, unknown={unknown}")
    if tuple(identifiers) != REQUIRED_CHECK_IDS:
        _fail("receipts", "receipts must use canonical required-check order")

    passed: list[str] = []
    failed: list[str] = []
    indeterminate: list[str] = []
    observation_ids: set[str] = set()
    prior_statuses: dict[str, str] = {}
    for index, receipt in enumerate(items):
        check_id, status = _validate_receipt(
            receipt,
            index,
            expectations,
            signature_verifier,
            artifact_verifier,
            observation_ids,
            prior_statuses,
        )
        prior_statuses[check_id] = status
        {"pass": passed, "fail": failed, "indeterminate": indeterminate}[status].append(check_id)

    if failed:
        disposition = NOT_READY
    elif indeterminate:
        disposition = INDETERMINATE
    else:
        disposition = READY
    return PreflightDecision(
        disposition=disposition,
        required_check_set_digest=REQUIRED_CHECK_SET_DIGEST,
        receipt_set_digest=receipt_set_digest,
        passed_checks=tuple(passed),
        failed_checks=tuple(failed),
        indeterminate_checks=tuple(indeterminate),
    )


def describe_contract() -> dict[str, object]:
    """Describe the deliberately non-authoritative, effect-free boundary."""

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "authoritative": AUTHORITATIVE,
        "performs_effects": PERFORMS_EFFECTS,
        "required_check_count": len(REQUIRED_CHECK_IDS),
        "resource_kinds": list(RESOURCE_KINDS),
        "callback_contract": "external-trusted-pure-literal-true",
        "transport_contract": "external-strict-json-decoder-required",
    }


__all__ = [
    "AUTHORITATIVE",
    "ArtifactExpectation",
    "CHECK_SPECS",
    "CHECK_SPEC_BY_ID",
    "INDETERMINATE",
    "IssuerExpectation",
    "NOT_READY",
    "PERFORMS_EFFECTS",
    "PreflightDecision",
    "PreflightExpectations",
    "PreflightPolicyError",
    "READY",
    "RECEIPT_SCHEMA",
    "REQUIRED_CHECK_IDS",
    "REQUIRED_CHECK_SET_DIGEST",
    "RESOURCE_KINDS",
    "SubjectExpectation",
    "VERSION",
    "VerifierExpectation",
    "digest_json",
    "describe_contract",
    "required_check_set_document",
    "validate_preflight_receipts",
]
