"""Offline conformance validation for PropertyQuarry release evidence v2.

This module is deliberately *not* release authority.  It validates a closed
JSON-shaped manifest against identities and lifecycle ancestry supplied by a
trusted caller.  Cryptographic signature verification and proof that fsync
really occurred are mandatory caller-provided operations; no manifest field is
allowed to self-attest either fact.

The API accepts an already-decoded object.  A caller receiving transport bytes
must first use the lifecycle transport's strict UTF-8/duplicate-key/non-finite
decoder; information erased by a permissive JSON parser cannot be recovered by
an object validator.

Protocol v1 is unrelated and is neither imported nor extended here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Callable, Mapping, Sequence


SCHEMA = "propertyquarry.release.evidence-manifest"
VERSION = 2
ADMISSION_INTENT_GENERATION = 0

SUCCESS_STATES = (
    "admitted",
    "containment-started",
    "contained",
    "deploy-started",
    "deployed",
    "live-verification-started",
    "live-verified",
    "activation-started",
    "activation-verified",
    "overlay-activation-started",
    "overlay-activated",
    "finalization-started",
    "sealed-final",
)

STATE_PHASE = {
    "admitted": ("result", "admission"),
    "containment-started": ("intent", "containment"),
    "contained": ("result", "containment"),
    "deploy-started": ("intent", "deploy"),
    "deployed": ("result", "deploy"),
    "live-verification-started": ("intent", "live-verification"),
    "live-verified": ("result", "live-verification"),
    "activation-started": ("intent", "activation"),
    "activation-verified": ("result", "activation"),
    "overlay-activation-started": ("intent", "overlay-activation"),
    "overlay-activated": ("result", "overlay-activation"),
    "finalization-started": ("intent", "finalization"),
    "sealed-final": ("result", "finalization"),
}

RESOURCE_KINDS = (
    "database",
    "launch-authority",
    "monitoring-delivery",
    "overlay",
    "public-tour",
    "runtime",
    "traffic",
)

FIXED_OUTCOMES = {
    "database": frozenset(
        {
            "unchanged",
            "fenced",
            "forward-compatible",
            "restored-verified",
            "forward-repair-verified",
        }
    ),
    "launch-authority": frozenset({"unchanged", "sealed-verified"}),
    "monitoring-delivery": frozenset({"unchanged", "continuity-verified"}),
    "overlay": frozenset({"unchanged", "activated-verified", "restored-verified"}),
    "public-tour": frozenset({"unchanged", "activated-verified", "restored-verified"}),
    "runtime": frozenset({"unchanged", "deployed-verified", "restored-verified"}),
    "traffic": frozenset({"unchanged", "contained", "activated-verified", "restored-verified"}),
}

# An outcome may cite only evidence whose discriminator can actually prove that
# resource kind and outcome.  This table is deliberately closed.  Later phases
# may carry forward an earlier compatible artifact by digest, but may never
# relabel an ancestry, Gold, or another resource's artifact as proof.
OUTCOME_EVIDENCE_TYPES = {
    ("database", "unchanged"): frozenset({"lease-fence-acquisition", "database-outcome"}),
    ("database", "fenced"): frozenset({"database-fence"}),
    ("database", "forward-compatible"): frozenset({"database-outcome"}),
    ("database", "restored-verified"): frozenset({"database-outcome"}),
    ("database", "forward-repair-verified"): frozenset({"database-outcome"}),
    ("launch-authority", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("launch-authority", "sealed-verified"): frozenset({"final-authority"}),
    ("monitoring-delivery", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("monitoring-delivery", "continuity-verified"): frozenset({"monitoring-continuity"}),
    ("overlay", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("overlay", "activated-verified"): frozenset({"overlay-cas", "overlay-active-revalidation", "overlay-final-state"}),
    ("overlay", "restored-verified"): frozenset({"overlay-final-state"}),
    ("public-tour", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("public-tour", "activated-verified"): frozenset({"public-tour-final-state"}),
    ("public-tour", "restored-verified"): frozenset({"public-tour-final-state"}),
    ("runtime", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("runtime", "deployed-verified"): frozenset(
        {"deployment-identity", "runtime-health", "live-identity", "runtime-final-state"}
    ),
    ("runtime", "restored-verified"): frozenset({"runtime-final-state"}),
    ("traffic", "unchanged"): frozenset({"lease-fence-acquisition", "target-fence-proof"}),
    ("traffic", "contained"): frozenset({"writer-drain", "target-fence-proof"}),
    ("traffic", "activated-verified"): frozenset({"traffic-final-state"}),
    ("traffic", "restored-verified"): frozenset({"traffic-final-state"}),
}

TERMINAL_RESOURCE_EVIDENCE = {
    "database": "database-outcome",
    "launch-authority": "final-authority",
    "monitoring-delivery": "monitoring-continuity",
    "overlay": "overlay-final-state",
    "public-tour": "public-tour-final-state",
    "runtime": "runtime-final-state",
    "traffic": "traffic-final-state",
}

REQUIRED_EVIDENCE = {
    "admitted": frozenset(
        {"signed-request", "ready-preflight", "replay-consumption", "lease-fence-acquisition"}
    ),
    "containment-started": frozenset({"phase-intent", "target-fence-proof"}),
    "contained": frozenset(
        {"writer-drain", "database-fence", "recovery-target", "target-fence-proof"}
    ),
    "deploy-started": frozenset({"phase-intent", "target-fence-proof"}),
    "deployed": frozenset(
        {
            "candidate-artifact",
            "deployment-identity",
            "database-outcome",
            "runtime-health",
            "target-fence-proof",
        }
    ),
    "live-verification-started": frozenset({"phase-intent", "target-fence-proof"}),
    "live-verified": frozenset(
        {"live-identity", "public-probe", "authenticated-probe", "slo-alert-observation"}
    ),
    "activation-started": frozenset({"phase-intent", "target-fence-proof"}),
    "activation-verified": frozenset({"activation-account-state", "activation-effect-audit"}),
    "overlay-activation-started": frozenset({"phase-intent", "target-fence-proof"}),
    "overlay-activated": frozenset({"overlay-cas", "overlay-active-revalidation"}),
    "finalization-started": frozenset({"phase-intent", "target-fence-proof"}),
    "sealed-final": frozenset(
        {
            "complete-ancestry",
            "gold-result",
            "final-authority",
            "monitoring-continuity",
            "database-outcome",
            "overlay-final-state",
            "public-tour-final-state",
            "runtime-final-state",
            "target-fence-proof",
            "traffic-final-state",
        }
    ),
}

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_TIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z"
)


class EvidenceValidationError(ValueError):
    """Raised when a v2 evidence manifest fails closed validation."""


@dataclass(frozen=True)
class EvidenceExpectations:
    """Trusted values the untrusted manifest must match exactly."""

    binding: Mapping[str, object]
    preflight: Mapping[str, object]
    ancestry: Sequence[Mapping[str, object]]
    allowed_resource_outcomes: Mapping[str, frozenset[str]]
    approved_verifier_digests: Mapping[str, str]
    storage_identity_digest: str
    gold_result_digest: str
    signature_algorithm: str
    signature_key_id: str


@dataclass(frozen=True)
class ValidatedEvidenceManifest:
    manifest_root: str
    ancestry_root: str
    first_generation: int
    last_generation: int
    terminal_seal_digest: str


SignatureVerifier = Callable[[bytes, Mapping[str, object]], bool]
FsyncVerifier = Callable[[str, Mapping[str, object]], bool]


def _fail(path: str, message: str) -> None:
    raise EvidenceValidationError(f"{path}: {message}")


def _object(value: object, path: str, keys: set[str] | frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        _fail(path, "must be a JSON object")
    result = value
    actual = set(result)
    if actual != set(keys):
        missing = sorted(set(keys) - actual)
        unknown = sorted(actual - set(keys))
        _fail(path, f"closed schema mismatch; missing={missing}, unknown={unknown}")
    if not all(type(key) is str for key in result):
        _fail(path, "object keys must be strings")
    return result


def _list(value: object, path: str) -> list[object]:
    if type(value) is not list:
        _fail(path, "must be a JSON array")
    return value


def _string(value: object, path: str) -> str:
    if type(value) is not str or not value:
        _fail(path, "must be a non-empty string")
    return value


def _identifier(value: object, path: str) -> str:
    text = _string(value, path)
    if not _ID_RE.fullmatch(text):
        _fail(path, "is not a bounded identifier")
    return text


def _digest(value: object, path: str) -> str:
    text = _string(value, path)
    if not _DIGEST_RE.fullmatch(text):
        _fail(path, "must be a lowercase sha256 digest")
    return text


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        _fail(path, f"must be an integer in [{minimum}, 2^63-1]")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _time(value: object, path: str) -> str:
    text = _string(value, path)
    if not _TIME_RE.fullmatch(text):
        _fail(path, "must be an RFC3339 timestamp with an explicit offset")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError(f"{path}: invalid RFC3339 timestamp") from exc
    return text


def _time_value(value: object, path: str) -> datetime:
    text = _time(value, path)
    # _time already requires an explicit offset, so every returned value is
    # timezone-aware and safely comparable on the authority timeline.
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvidenceValidationError(f"canonical JSON failed: {exc}") from exc


def canonical_digest(value: object) -> str:
    """Return the contract's deterministic content digest for a JSON value."""

    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


_BINDING_KEYS = frozenset(
    {
        "authority_id",
        "namespace",
        "target_id",
        "environment",
        "lifecycle_id",
        "epoch_id",
        "request_id",
        "request_nonce_digest",
        "request_transport_digest",
        "request_envelope_digest",
        "release_digest",
        "image_digest",
        "controller_digest",
        "policy_digest",
        "resource_set_digest",
        "lease_id",
        "lease_holder",
        "lease_deadline",
        "global_fence_token",
        "resources",
    }
)
_RESOURCE_KEYS = frozenset({"kind", "resource_id", "fence_token"})
_PREFLIGHT_KEYS = frozenset(
    {
        "request_id",
        "nonce_digest",
        "request_transport_digest",
        "request_envelope_digest",
        "response_transport_digest",
        "response_envelope_digest",
        "observed_lifecycle_seal_digest",
        "release_digest",
        "controller_digest",
        "policy_digest",
        "evaluated_at",
        "valid_until",
        "required_check_set_digest",
    }
)
_ANCESTRY_KEYS = frozenset(
    {
        "generation",
        "state",
        "event_id",
        "authority_observed_at",
        "previous_seal_digest",
        "state_digest",
        "seal_digest",
    }
)


def _validate_resources(value: object, path: str) -> list[dict[str, object]]:
    raw = _list(value, path)
    resources: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        resource = _object(item, f"{path}[{index}]", _RESOURCE_KEYS)
        kind = _identifier(resource["kind"], f"{path}[{index}].kind")
        resource_id = _identifier(resource["resource_id"], f"{path}[{index}].resource_id")
        _integer(resource["fence_token"], f"{path}[{index}].fence_token", minimum=1)
        if kind not in RESOURCE_KINDS:
            _fail(f"{path}[{index}].kind", "unknown resource kind")
        if (kind, resource_id) in seen:
            _fail(path, "duplicate resource identity")
        seen.add((kind, resource_id))
        resources.append(resource)
    if tuple(sorted(kind for kind, _ in seen)) != RESOURCE_KINDS:
        _fail(path, f"must bind exactly the resource kinds {RESOURCE_KINDS}")
    if [(r["kind"], r["resource_id"]) for r in resources] != sorted(
        (r["kind"], r["resource_id"]) for r in resources
    ):
        _fail(path, "resources must be in canonical kind/resource order")
    return resources


def _validate_binding(value: object, path: str) -> dict[str, object]:
    binding = _object(value, path, _BINDING_KEYS)
    for key in (
        "authority_id",
        "namespace",
        "target_id",
        "environment",
        "lifecycle_id",
        "epoch_id",
        "request_id",
        "lease_id",
        "lease_holder",
    ):
        _identifier(binding[key], f"{path}.{key}")
    for key in (
        "request_nonce_digest",
        "request_transport_digest",
        "request_envelope_digest",
        "release_digest",
        "image_digest",
        "controller_digest",
        "policy_digest",
        "resource_set_digest",
    ):
        _digest(binding[key], f"{path}.{key}")
    _time(binding["lease_deadline"], f"{path}.lease_deadline")
    _integer(binding["global_fence_token"], f"{path}.global_fence_token", minimum=1)
    resources = _validate_resources(binding["resources"], f"{path}.resources")
    resource_identity = [{"kind": r["kind"], "resource_id": r["resource_id"]} for r in resources]
    if binding["resource_set_digest"] != canonical_digest(resource_identity):
        _fail(f"{path}.resource_set_digest", "does not bind the canonical resource set")
    return binding


def _validate_preflight(value: object, path: str) -> dict[str, object]:
    preflight = _object(value, path, _PREFLIGHT_KEYS)
    _identifier(preflight["request_id"], f"{path}.request_id")
    for key in (
        "nonce_digest",
        "request_transport_digest",
        "request_envelope_digest",
        "response_transport_digest",
        "response_envelope_digest",
        "observed_lifecycle_seal_digest",
        "release_digest",
        "controller_digest",
        "policy_digest",
        "required_check_set_digest",
    ):
        _digest(preflight[key], f"{path}.{key}")
    _time(preflight["evaluated_at"], f"{path}.evaluated_at")
    _time(preflight["valid_until"], f"{path}.valid_until")
    return preflight


def _validate_ancestry_item(value: object, path: str) -> dict[str, object]:
    item = _object(value, path, _ANCESTRY_KEYS)
    _integer(item["generation"], f"{path}.generation", minimum=1)
    state = _identifier(item["state"], f"{path}.state")
    if state not in SUCCESS_STATES:
        _fail(f"{path}.state", "unknown lifecycle state")
    _identifier(item["event_id"], f"{path}.event_id")
    _time(item["authority_observed_at"], f"{path}.authority_observed_at")
    for key in ("previous_seal_digest", "state_digest", "seal_digest"):
        _digest(item[key], f"{path}.{key}")
    return item


_INTENT_KEYS = frozenset(
    {
        "record_type",
        "phase",
        "plan_digest",
        "input_digest",
        "planned_effect_digest",
        "recovery_target_digest",
        "external_idempotency_key",
        "expected_resource_versions_digest",
        "global_fence_token",
        "resource_fences",
    }
)
_RESULT_KEYS = frozenset(
    {
        "record_type",
        "phase",
        "intent_generation",
        "intent_seal_digest",
        "plan_digest",
        "input_digest",
        "planned_effect_digest",
        "recovery_target_digest",
        "external_idempotency_key",
        "expected_resource_versions_digest",
        "effect_digest",
        "outcome_digest",
        "resource_outcomes",
    }
)
_OUTCOME_KEYS = frozenset({"kind", "resource_id", "outcome", "evidence_digest"})


def _resource_fence_projection(binding: Mapping[str, object]) -> list[dict[str, object]]:
    return [
        {"kind": r["kind"], "resource_id": r["resource_id"], "fence_token": r["fence_token"]}
        for r in binding["resources"]  # type: ignore[union-attr]
    ]


def _validate_outcomes(
    value: object,
    path: str,
    binding: Mapping[str, object],
    expectations: EvidenceExpectations,
    phase: str,
) -> list[dict[str, object]]:
    raw = _list(value, path)
    resources = binding["resources"]
    if len(raw) != len(resources):  # type: ignore[arg-type]
        _fail(path, "must contain exactly one outcome for every bound resource")
    outcomes: list[dict[str, object]] = []
    for index, (item, resource) in enumerate(zip(raw, resources)):  # type: ignore[arg-type]
        outcome = _object(item, f"{path}[{index}]", _OUTCOME_KEYS)
        kind = _identifier(outcome["kind"], f"{path}[{index}].kind")
        resource_id = _identifier(outcome["resource_id"], f"{path}[{index}].resource_id")
        name = _identifier(outcome["outcome"], f"{path}[{index}].outcome")
        _digest(outcome["evidence_digest"], f"{path}[{index}].evidence_digest")
        if (kind, resource_id) != (resource["kind"], resource["resource_id"]):
            _fail(f"{path}[{index}]", "resource identity/order does not match the binding")
        allowed_by_policy = expectations.allowed_resource_outcomes.get(kind)
        if allowed_by_policy is None or name not in FIXED_OUTCOMES[kind] or name not in allowed_by_policy:
            _fail(f"{path}[{index}].outcome", "outcome is not allowed by kind and trusted policy")
        outcomes.append(outcome)

    by_kind = {item["kind"]: item["outcome"] for item in outcomes}
    safe_db = {"unchanged", "forward-compatible", "restored-verified", "forward-repair-verified"}
    if phase == "admission" and any(item["outcome"] != "unchanged" for item in outcomes):
        _fail(path, "admission may report only unchanged resources")
    if phase in {"containment", "deploy", "live-verification", "activation", "overlay-activation"}:
        if by_kind["traffic"] != "contained":
            _fail(path, "pre-final phases must retain contained traffic")
    if phase == "containment" and by_kind["database"] != "fenced":
        _fail(path, "containment must prove a fenced database")
    if phase in {"deploy", "live-verification", "activation", "overlay-activation", "finalization"}:
        if by_kind["database"] not in safe_db:
            _fail(path, "successful phase has no safe database outcome")
        if by_kind["runtime"] != "deployed-verified":
            _fail(path, "successful phase must bind the deployed runtime")
    if phase in {"overlay-activation", "finalization"} and by_kind["overlay"] != "activated-verified":
        _fail(path, "overlay phase must bind the activated overlay")
    if phase == "finalization":
        required = {
            "traffic": "activated-verified",
            "public-tour": "activated-verified",
            "launch-authority": "sealed-verified",
            "monitoring-delivery": "continuity-verified",
        }
        for kind, outcome in required.items():
            if by_kind[kind] != outcome:
                _fail(path, f"finalization requires {kind}={outcome}")
    return outcomes


def _validate_record(
    value: object,
    path: str,
    state: str,
    binding: Mapping[str, object],
    expectations: EvidenceExpectations,
    prior_intent: Mapping[str, object] | None,
    prior_intent_generation: int | None,
    prior_intent_seal: str | None,
) -> dict[str, object]:
    record_type, phase = STATE_PHASE[state]
    keys = _INTENT_KEYS if record_type == "intent" else _RESULT_KEYS
    record = _object(value, path, keys)
    if record["record_type"] != record_type or record["phase"] != phase:
        _fail(path, f"must be the {record_type} record for phase {phase}")
    for key in ("plan_digest", "input_digest", "planned_effect_digest"):
        _digest(record[key], f"{path}.{key}")
    if record_type == "intent":
        for key in ("recovery_target_digest", "expected_resource_versions_digest"):
            _digest(record[key], f"{path}.{key}")
        _identifier(record["external_idempotency_key"], f"{path}.external_idempotency_key")
        if record["global_fence_token"] != binding["global_fence_token"]:
            _fail(f"{path}.global_fence_token", "stale or substituted global fence")
        if record["resource_fences"] != _resource_fence_projection(binding):
            _fail(f"{path}.resource_fences", "stale or substituted per-resource fences")
        _validate_resources(record["resource_fences"], f"{path}.resource_fences")
    else:
        _integer(record["intent_generation"], f"{path}.intent_generation", minimum=0)
        _digest(record["intent_seal_digest"], f"{path}.intent_seal_digest")
        for key in ("recovery_target_digest", "expected_resource_versions_digest", "effect_digest", "outcome_digest"):
            _digest(record[key], f"{path}.{key}")
        _identifier(record["external_idempotency_key"], f"{path}.external_idempotency_key")
        _validate_outcomes(record["resource_outcomes"], f"{path}.resource_outcomes", binding, expectations, phase)
        if phase == "admission":
            if prior_intent is not None:
                _fail(path, "admission cannot be paired with a manifest intent")
        else:
            if prior_intent is None or prior_intent_generation is None or prior_intent_seal is None:
                _fail(path, "result is missing its immediately preceding phase intent")
            if record["intent_generation"] != prior_intent_generation:
                _fail(f"{path}.intent_generation", "does not bind the phase intent generation")
            if record["intent_seal_digest"] != prior_intent_seal:
                _fail(f"{path}.intent_seal_digest", "does not bind the phase intent seal")
            for key in (
                "plan_digest",
                "input_digest",
                "planned_effect_digest",
                "recovery_target_digest",
                "external_idempotency_key",
                "expected_resource_versions_digest",
            ):
                if record[key] != prior_intent[key]:
                    _fail(f"{path}.{key}", "does not bind the exact phase intent")
    return record


_EVIDENCE_KEYS = frozenset(
    {
        "type",
        "subject",
        "status",
        "lifecycle_id",
        "release_digest",
        "image_digest",
        "environment",
        "observation_time",
        "verifier_binary_digest",
        "verifier_policy_digest",
        "artifact_digest",
        "byte_size",
        "media_type",
        "dependencies",
        "details",
    }
)

_DETAIL_KEYS = {
    "signed-request": frozenset({"request_id", "nonce_digest", "transport_digest", "envelope_digest"}),
    "ready-preflight": frozenset(
        {
            "request_id",
            "response_transport_digest",
            "response_envelope_digest",
            "observed_seal_digest",
            "evaluated_at",
            "valid_until",
            "required_check_set_digest",
        }
    ),
    "replay-consumption": frozenset({"request_id", "nonce_digest", "receipt_digest"}),
    "lease-fence-acquisition": frozenset(
        {"lease_id", "lease_holder", "global_fence_token", "resource_fences", "receipt_digest"}
    ),
    "phase-intent": frozenset(
        {
            "plan_digest",
            "input_digest",
            "planned_effect_digest",
            "recovery_target_digest",
            "external_idempotency_key",
            "expected_resource_versions_digest",
        }
    ),
    "target-fence-proof": frozenset({"global_fence_token", "resource_fences", "proof_digest"}),
    "writer-drain": frozenset({"writer_count", "receipt_digest"}),
    "database-fence": frozenset({"resource_id", "global_fence_token", "resource_fence_token", "proof_digest"}),
    "recovery-target": frozenset({"target_digest", "verification_digest"}),
    "candidate-artifact": frozenset({"release_digest", "image_digest", "artifact_manifest_digest"}),
    "deployment-identity": frozenset({"release_digest", "image_digest", "config_digest", "replica_set_digest"}),
    "runtime-health": frozenset({"release_digest", "image_digest", "probe_digest"}),
    "live-identity": frozenset(
        {"release_digest", "image_digest", "config_digest", "replica_set_digest"}
    ),
    "public-probe": frozenset({"probe_set_digest", "response_digest"}),
    "authenticated-probe": frozenset({"probe_set_digest", "response_digest"}),
    "slo-alert-observation": frozenset({"observation_digest", "alerts_clear"}),
    "activation-account-state": frozenset(
        {"persona_digest", "broker_digest", "idempotency_key", "before_state_digest", "after_state_digest", "release_digest"}
    ),
    "activation-effect-audit": frozenset(
        {"effect_digest", "unauthorized_provider_effect_count", "unauthorized_send_effect_count"}
    ),
    "overlay-cas": frozenset(
        {"prior_snapshot_digest", "staged_snapshot_digest", "active_snapshot_digest", "cas_receipt_digest", "success"}
    ),
    "overlay-active-revalidation": frozenset({"active_snapshot_digest", "probe_digest"}),
    "complete-ancestry": frozenset(
        {
            "predecessor_ancestry_root",
            "predecessor_entry_count",
            "first_generation",
            "predecessor_generation",
            "predecessor_seal_digest",
        }
    ),
    "gold-result": frozenset({"result_digest", "gold"}),
    "final-authority": frozenset(
        {
            "authority_digest",
            "launch_success",
            "resource_id",
            "outcome",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
    "monitoring-continuity": frozenset(
        {
            "before_digest",
            "after_digest",
            "gap_seconds",
            "resource_id",
            "outcome",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
    "runtime-final-state": frozenset(
        {
            "resource_id",
            "outcome",
            "release_digest",
            "image_digest",
            "config_digest",
            "replica_set_digest",
            "health_probe_digest",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
    "traffic-final-state": frozenset(
        {
            "resource_id",
            "outcome",
            "release_digest",
            "route_selection_digest",
            "public_probe_digest",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
    "overlay-final-state": frozenset(
        {
            "resource_id",
            "outcome",
            "active_snapshot_digest",
            "active_revalidation_digest",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
    "public-tour-final-state": frozenset(
        {
            "resource_id",
            "outcome",
            "volume_digest",
            "catalog_digest",
            "route_probe_digest",
            "global_fence_token",
            "resource_fence_token",
        }
    ),
}

_DB_COMMON_KEYS = frozenset(
    {"resource_id", "outcome", "database_identity_digest", "pre_schema_digest", "post_schema_digest", "probes_digest"}
)
_DB_RESTORED_KEYS = _DB_COMMON_KEYS | frozenset(
    {"backup_identity_digest", "recovery_position", "restore_checksum_digest"}
)
_DB_FORWARD_KEYS = _DB_COMMON_KEYS | frozenset({"prior_runtime_probe_digest", "candidate_runtime_probe_digest"})
_DB_REPAIR_KEYS = _DB_COMMON_KEYS | frozenset(
    {"policy_authorization_digest", "repair_digest", "equivalence_probe_digest"}
)


def _validate_database_details(
    value: object,
    path: str,
    binding: Mapping[str, object],
    record: Mapping[str, object],
) -> None:
    if type(value) is not dict or "outcome" not in value:
        _fail(path, "must be a discriminated database-outcome object")
    outcome = value["outcome"]
    keys = {
        "unchanged": _DB_COMMON_KEYS,
        "forward-compatible": _DB_FORWARD_KEYS,
        "restored-verified": _DB_RESTORED_KEYS,
        "forward-repair-verified": _DB_REPAIR_KEYS,
    }.get(outcome)
    if keys is None:
        _fail(f"{path}.outcome", "unresolved or unknown database outcome is forbidden in a success manifest")
    details = _object(value, path, keys)
    database = next(r for r in binding["resources"] if r["kind"] == "database")  # type: ignore[union-attr]
    if details["resource_id"] != database["resource_id"]:
        _fail(f"{path}.resource_id", "does not bind the database resource")
    for key in keys - {"resource_id", "outcome", "recovery_position"}:
        _digest(details[key], f"{path}.{key}")
    db_outcome = next(
        item for item in record["resource_outcomes"] if item["kind"] == "database"  # type: ignore[union-attr]
    )
    if outcome != db_outcome["outcome"]:
        _fail(f"{path}.outcome", "does not match the phase database outcome")
    if outcome in {"unchanged", "restored-verified"} and details["post_schema_digest"] != details["pre_schema_digest"]:
        _fail(path, "unchanged/restored database must match the pre-operation schema")
    if outcome == "restored-verified":
        recovery = _object(details["recovery_position"], f"{path}.recovery_position", {"kind", "digest"})
        if recovery["kind"] not in {"wal", "lsn", "equivalent"}:
            _fail(f"{path}.recovery_position.kind", "must be wal, lsn, or equivalent")
        _digest(recovery["digest"], f"{path}.recovery_position.digest")


def _validate_evidence_details(
    evidence_type: str,
    value: object,
    path: str,
    binding: Mapping[str, object],
    preflight: Mapping[str, object],
    expectations: EvidenceExpectations,
    record: Mapping[str, object],
) -> dict[str, object]:
    if evidence_type == "database-outcome":
        _validate_database_details(value, path, binding, record)
        return value  # type: ignore[return-value]
    keys = _DETAIL_KEYS.get(evidence_type)
    if keys is None:
        _fail(path, "unknown evidence discriminator")
    details = _object(value, path, keys)
    for key, item in details.items():
        if key.endswith("_digest") or key in {"transport_digest", "envelope_digest", "observed_seal_digest"}:
            _digest(item, f"{path}.{key}")

    if evidence_type == "signed-request":
        exact = {
            "request_id": binding["request_id"],
            "nonce_digest": binding["request_nonce_digest"],
            "transport_digest": binding["request_transport_digest"],
            "envelope_digest": binding["request_envelope_digest"],
        }
        if details != exact:
            _fail(path, "does not bind the exact release request")
    elif evidence_type == "ready-preflight":
        exact = {
            "request_id": preflight["request_id"],
            "response_transport_digest": preflight["response_transport_digest"],
            "response_envelope_digest": preflight["response_envelope_digest"],
            "observed_seal_digest": preflight["observed_lifecycle_seal_digest"],
            "evaluated_at": preflight["evaluated_at"],
            "valid_until": preflight["valid_until"],
            "required_check_set_digest": preflight["required_check_set_digest"],
        }
        if details != exact:
            _fail(path, "does not bind the exact ready preflight")
    elif evidence_type == "replay-consumption":
        if details["request_id"] != binding["request_id"] or details["nonce_digest"] != binding["request_nonce_digest"]:
            _fail(path, "does not bind request/replay identities")
    elif evidence_type == "lease-fence-acquisition":
        if (
            details["lease_id"] != binding["lease_id"]
            or details["lease_holder"] != binding["lease_holder"]
            or details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fences"] != _resource_fence_projection(binding)
        ):
            _fail(path, "does not bind exact lease and fences")
    elif evidence_type == "phase-intent":
        for key in (
            "plan_digest",
            "input_digest",
            "planned_effect_digest",
            "recovery_target_digest",
            "external_idempotency_key",
            "expected_resource_versions_digest",
        ):
            if details[key] != record[key]:
                _fail(f"{path}.{key}", "does not bind the exact intent record")
    elif evidence_type == "target-fence-proof":
        if (
            details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fences"] != _resource_fence_projection(binding)
        ):
            _fail(path, "contains stale or substituted fencing proof")
    elif evidence_type == "writer-drain":
        if _integer(details["writer_count"], f"{path}.writer_count") != 0:
            _fail(f"{path}.writer_count", "writers were not drained")
    elif evidence_type == "recovery-target":
        if details["target_digest"] != record["recovery_target_digest"]:
            _fail(f"{path}.target_digest", "does not bind the phase recovery target")
    elif evidence_type == "database-fence":
        database = next(r for r in binding["resources"] if r["kind"] == "database")  # type: ignore[union-attr]
        if (
            details["resource_id"] != database["resource_id"]
            or details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fence_token"] != database["fence_token"]
        ):
            _fail(path, "does not bind the database fence")
    elif evidence_type in {"candidate-artifact", "deployment-identity", "runtime-health", "live-identity"}:
        if details["release_digest"] != binding["release_digest"] or details["image_digest"] != binding["image_digest"]:
            _fail(path, "does not bind the exact release and image")
    elif evidence_type == "activation-account-state":
        if (
            details["release_digest"] != binding["release_digest"]
            or details["idempotency_key"] != record["external_idempotency_key"]
        ):
            _fail(path, "does not bind the exact release")
    elif evidence_type == "activation-effect-audit":
        if (
            details["effect_digest"] != record["effect_digest"]
            or _integer(
                details["unauthorized_provider_effect_count"],
                f"{path}.unauthorized_provider_effect_count",
            )
            != 0
            or _integer(details["unauthorized_send_effect_count"], f"{path}.unauthorized_send_effect_count") != 0
        ):
            _fail(path, "unauthorized activation effects are nonzero")
    elif evidence_type == "slo-alert-observation":
        if not _boolean(details["alerts_clear"], f"{path}.alerts_clear"):
            _fail(f"{path}.alerts_clear", "alerts are not clear")
    elif evidence_type == "overlay-cas":
        if not _boolean(details["success"], f"{path}.success"):
            _fail(f"{path}.success", "overlay CAS did not succeed")
    elif evidence_type == "gold-result":
        if not _boolean(details["gold"], f"{path}.gold"):
            _fail(f"{path}.gold", "Gold result is not successful")
        if details["result_digest"] != expectations.gold_result_digest:
            _fail(f"{path}.result_digest", "does not match the exact trusted Gold result")
    elif evidence_type == "final-authority":
        if not _boolean(details["launch_success"], f"{path}.launch_success"):
            _fail(f"{path}.launch_success", "final authority is not successful")
        resource = next(
            item for item in binding["resources"] if item["kind"] == "launch-authority"  # type: ignore[union-attr]
        )
        outcome = next(
            item for item in record["resource_outcomes"] if item["kind"] == "launch-authority"  # type: ignore[union-attr]
        )
        if (
            details["resource_id"] != resource["resource_id"]
            or details["outcome"] != outcome["outcome"]
            or details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fence_token"] != resource["fence_token"]
        ):
            _fail(path, "does not bind the exact terminal launch-authority state and fences")
    elif evidence_type == "monitoring-continuity":
        if _integer(details["gap_seconds"], f"{path}.gap_seconds") != 0:
            _fail(f"{path}.gap_seconds", "monitoring continuity has a gap")
        resource = next(
            item for item in binding["resources"] if item["kind"] == "monitoring-delivery"  # type: ignore[union-attr]
        )
        outcome = next(
            item for item in record["resource_outcomes"] if item["kind"] == "monitoring-delivery"  # type: ignore[union-attr]
        )
        if (
            details["resource_id"] != resource["resource_id"]
            or details["outcome"] != outcome["outcome"]
            or details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fence_token"] != resource["fence_token"]
        ):
            _fail(path, "does not bind the exact terminal monitoring-delivery state and fences")
    elif evidence_type in {
        "runtime-final-state",
        "traffic-final-state",
        "overlay-final-state",
        "public-tour-final-state",
    }:
        resource_kind = evidence_type.removesuffix("-final-state")
        resource = next(
            item for item in binding["resources"] if item["kind"] == resource_kind  # type: ignore[union-attr]
        )
        outcome = next(
            item for item in record["resource_outcomes"] if item["kind"] == resource_kind  # type: ignore[union-attr]
        )
        if (
            details["resource_id"] != resource["resource_id"]
            or details["outcome"] != outcome["outcome"]
            or details["global_fence_token"] != binding["global_fence_token"]
            or details["resource_fence_token"] != resource["fence_token"]
        ):
            _fail(path, f"does not bind the exact terminal {resource_kind} state and fences")
        if evidence_type in {"runtime-final-state", "traffic-final-state"}:
            if details["release_digest"] != binding["release_digest"]:
                _fail(f"{path}.release_digest", "does not bind the exact release")
        if evidence_type == "runtime-final-state" and details["image_digest"] != binding["image_digest"]:
            _fail(f"{path}.image_digest", "does not bind the exact image")
    return details


def _validate_evidence(
    value: object,
    path: str,
    state: str,
    binding: Mapping[str, object],
    preflight: Mapping[str, object],
    expectations: EvidenceExpectations,
    record: Mapping[str, object],
    required_dependency: str | None,
    subjects: set[str],
    artifact_digests: set[str],
) -> list[dict[str, object]]:
    raw = _list(value, path)
    if not raw:
        _fail(path, "success evidence cannot be empty")
    evidence_items: list[dict[str, object]] = []
    types: list[str] = []
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        evidence = _object(item, item_path, _EVIDENCE_KEYS)
        evidence_type = _identifier(evidence["type"], f"{item_path}.type")
        subject = _identifier(evidence["subject"], f"{item_path}.subject")
        if evidence_type not in _DETAIL_KEYS and evidence_type != "database-outcome":
            _fail(f"{item_path}.type", "unknown evidence discriminator")
        if subject in subjects:
            _fail(f"{item_path}.subject", "semantic subject is not unique")
        subjects.add(subject)
        if evidence["status"] != "pass":
            _fail(f"{item_path}.status", "successful release evidence must have status=pass")
        exact_common = {
            "lifecycle_id": binding["lifecycle_id"],
            "release_digest": binding["release_digest"],
            "image_digest": binding["image_digest"],
            "environment": binding["environment"],
            "verifier_policy_digest": binding["policy_digest"],
        }
        for key, expected in exact_common.items():
            if evidence[key] != expected:
                _fail(f"{item_path}.{key}", "stale or substituted identity")
        _time(evidence["observation_time"], f"{item_path}.observation_time")
        verifier = _digest(evidence["verifier_binary_digest"], f"{item_path}.verifier_binary_digest")
        if verifier != expectations.approved_verifier_digests.get(evidence_type):
            _fail(f"{item_path}.verifier_binary_digest", "verifier is not the trusted binary for this evidence type")
        artifact = _digest(evidence["artifact_digest"], f"{item_path}.artifact_digest")
        if artifact in artifact_digests:
            _fail(f"{item_path}.artifact_digest", "evidence artifact digest is not unique")
        artifact_digests.add(artifact)
        _integer(evidence["byte_size"], f"{item_path}.byte_size", minimum=1)
        media_type = _string(evidence["media_type"], f"{item_path}.media_type")
        if "/" not in media_type or len(media_type) > 127:
            _fail(f"{item_path}.media_type", "must be a bounded media type")
        dependencies = _list(evidence["dependencies"], f"{item_path}.dependencies")
        for dep_index, dependency in enumerate(dependencies):
            _digest(dependency, f"{item_path}.dependencies[{dep_index}]")
        if len(dependencies) != len(set(dependencies)):
            _fail(f"{item_path}.dependencies", "dependencies must be unique")
        if required_dependency is None:
            if dependencies:
                _fail(f"{item_path}.dependencies", "first-generation evidence cannot depend on an absent predecessor")
        elif required_dependency not in dependencies:
            _fail(f"{item_path}.dependencies", "must bind the immediately preceding generation evidence root")
        _validate_evidence_details(
            evidence_type,
            evidence["details"],
            f"{item_path}.details",
            binding,
            preflight,
            expectations,
            record,
        )
        types.append(evidence_type)
        evidence_items.append(evidence)
    if len(types) != len(set(types)) or frozenset(types) != REQUIRED_EVIDENCE[state]:
        _fail(path, f"must contain exactly the closed evidence set {sorted(REQUIRED_EVIDENCE[state])}")
    return evidence_items


_PERSISTENCE_KEYS = frozenset(
    {
        "method",
        "phase_root",
        "evidence_root",
        "storage_identity_digest",
        "persisted_bytes",
        "file_fsync_receipt_digest",
        "directory_fsync_receipt_digest",
        "fsync_completed_before_cas",
    }
)


def _validate_persistence(
    value: object,
    path: str,
    state: str,
    phase_root: str,
    evidence_root: str,
    expectations: EvidenceExpectations,
    fsync_verifier: FsyncVerifier,
) -> dict[str, object]:
    persistence = _object(value, path, _PERSISTENCE_KEYS)
    if persistence["method"] != "file-fsync+directory-fsync-v1":
        _fail(f"{path}.method", "unsupported durability acknowledgement")
    if persistence["phase_root"] != phase_root or persistence["evidence_root"] != evidence_root:
        _fail(path, "durability acknowledgement does not bind the exact phase evidence")
    _digest(persistence["storage_identity_digest"], f"{path}.storage_identity_digest")
    if persistence["storage_identity_digest"] != expectations.storage_identity_digest:
        _fail(f"{path}.storage_identity_digest", "untrusted storage identity")
    _integer(persistence["persisted_bytes"], f"{path}.persisted_bytes", minimum=1)
    for key in ("file_fsync_receipt_digest", "directory_fsync_receipt_digest"):
        _digest(persistence[key], f"{path}.{key}")
    if not _boolean(persistence["fsync_completed_before_cas"], f"{path}.fsync_completed_before_cas"):
        _fail(f"{path}.fsync_completed_before_cas", "phase result was not durably fsynced before CAS")
    try:
        verified = fsync_verifier(state, persistence)
    except Exception as exc:
        raise EvidenceValidationError(f"{path}: trusted fsync verifier failed: {exc}") from exc
    if verified is not True:
        _fail(path, "trusted fsync verifier rejected the acknowledgement")
    return persistence


_ENTRY_KEYS = frozenset(
    {"generation", "state", "binding", "record", "evidence", "evidence_root", "phase_root", "persistence", "cas"}
)
_CAS_KEYS = frozenset(
    {"event_id", "authority_observed_at", "previous_seal_digest", "state_digest", "seal_digest"}
)


def _state_digest(entry: Mapping[str, object]) -> str:
    return canonical_digest(
        {
            "generation": entry["generation"],
            "state": entry["state"],
            "binding": entry["binding"],
            "record": entry["record"],
            "evidence_root": entry["evidence_root"],
            "phase_root": entry["phase_root"],
            "persistence": entry["persistence"],
        }
    )


def _seal_digest(entry: Mapping[str, object], cas: Mapping[str, object]) -> str:
    binding = entry["binding"]
    return canonical_digest(
        {
            "authority_id": binding["authority_id"],  # type: ignore[index]
            "namespace": binding["namespace"],  # type: ignore[index]
            "target_id": binding["target_id"],  # type: ignore[index]
            "lifecycle_id": binding["lifecycle_id"],  # type: ignore[index]
            "epoch_id": binding["epoch_id"],  # type: ignore[index]
            "generation": entry["generation"],
            "state": entry["state"],
            "event_id": cas["event_id"],
            "authority_observed_at": cas["authority_observed_at"],
            "previous_seal_digest": cas["previous_seal_digest"],
            "state_digest": cas["state_digest"],
        }
    )


def compute_manifest_root(manifest: Mapping[str, object]) -> str:
    """Compute the ordered root, excluding the seal and signature wrappers."""

    keys = {"schema", "version", "binding", "first_generation", "last_generation", "entries"}
    if not keys.issubset(manifest):
        raise EvidenceValidationError("manifest root input is incomplete")
    return canonical_digest({key: manifest[key] for key in sorted(keys)})


def validate_manifest(
    manifest: object,
    expectations: EvidenceExpectations,
    *,
    signature_verifier: SignatureVerifier,
    fsync_verifier: FsyncVerifier,
) -> ValidatedEvidenceManifest:
    """Validate one complete successful lifecycle-v2 evidence manifest.

    Both verifier callbacks are mandatory.  Returning truthy values other than
    the literal ``True`` is rejected.  Validation proves conformance only; the
    caller remains responsible for obtaining expectations from external trust
    authorities and for authenticating the returned terminal receipt.
    """

    root = _object(
        manifest,
        "$",
        {
            "schema",
            "version",
            "binding",
            "first_generation",
            "last_generation",
            "entries",
            "manifest_root",
            "final_seal",
            "signature",
        },
    )
    if root["schema"] != SCHEMA:
        _fail("$", "unknown evidence-manifest protocol")
    if _integer(root["version"], "$.version", minimum=VERSION) != VERSION:
        _fail("$.version", "unknown evidence-manifest protocol version")

    expected_binding = _validate_binding(dict(expectations.binding), "expectations.binding")
    binding = _validate_binding(root["binding"], "$.binding")
    if binding != expected_binding:
        _fail("$.binding", "does not exactly match trusted lifecycle/request/controller/policy/lease/fence identities")

    preflight = _validate_preflight(dict(expectations.preflight), "expectations.preflight")
    if preflight["release_digest"] != binding["release_digest"]:
        _fail("expectations.preflight.release_digest", "does not bind the release")
    if preflight["controller_digest"] != binding["controller_digest"] or preflight["policy_digest"] != binding["policy_digest"]:
        _fail("expectations.preflight", "does not bind controller/policy identities")
    if preflight["request_id"] == binding["request_id"] or preflight["nonce_digest"] == binding["request_nonce_digest"]:
        _fail("expectations.preflight", "preflight and release-run must use distinct request IDs and nonces")
    preflight_evaluated_at = _time_value(preflight["evaluated_at"], "expectations.preflight.evaluated_at")
    preflight_valid_until = _time_value(preflight["valid_until"], "expectations.preflight.valid_until")
    if preflight_evaluated_at >= preflight_valid_until:
        _fail("expectations.preflight", "validity interval is empty or reversed")
    lease_deadline = _time_value(binding["lease_deadline"], "$.binding.lease_deadline")
    _digest(expectations.gold_result_digest, "expectations.gold_result_digest")

    expected_ancestry = [
        _validate_ancestry_item(dict(item), f"expectations.ancestry[{index}]")
        for index, item in enumerate(expectations.ancestry)
    ]
    if len(expected_ancestry) != len(SUCCESS_STATES):
        _fail("expectations.ancestry", "must contain the complete successful lifecycle")

    first_generation = _integer(root["first_generation"], "$.first_generation", minimum=1)
    last_generation = _integer(root["last_generation"], "$.last_generation", minimum=1)
    if last_generation - first_generation + 1 != len(SUCCESS_STATES):
        _fail("$", "generation range is not the complete contiguous successful lifecycle")
    entries = _list(root["entries"], "$.entries")
    if len(entries) != len(SUCCESS_STATES):
        _fail("$.entries", "manifest is truncated or contains extra lifecycle entries")

    seen_generations: set[int] = set()
    seen_event_ids: set[str] = set()
    subjects: set[str] = set()
    artifact_digests: set[str] = set()
    artifact_types_by_digest: dict[str, str] = {}
    ancestry_projection: list[dict[str, object]] = []
    previous_seal = preflight["observed_lifecycle_seal_digest"]
    previous_evidence_root: str | None = None
    prior_intent: Mapping[str, object] | None = None
    prior_intent_generation: int | None = None
    prior_intent_seal: str | None = None
    previous_authority_time: datetime | None = None
    phase_idempotency_keys: dict[str, str] = {}

    for index, raw_entry in enumerate(entries):
        path = f"$.entries[{index}]"
        entry = _object(raw_entry, path, _ENTRY_KEYS)
        generation = _integer(entry["generation"], f"{path}.generation", minimum=1)
        expected_generation = first_generation + index
        if generation != expected_generation:
            _fail(f"{path}.generation", "entries must be ordered by unique contiguous ledger generation")
        if generation in seen_generations:
            _fail(f"{path}.generation", "duplicate ledger generation")
        seen_generations.add(generation)
        state = _identifier(entry["state"], f"{path}.state")
        if state != SUCCESS_STATES[index]:
            _fail(f"{path}.state", "missing, reordered, repeated, or unknown lifecycle phase")
        entry_binding = _validate_binding(entry["binding"], f"{path}.binding")
        if entry_binding != binding:
            _fail(f"{path}.binding", "stale or substituted lifecycle/fence identity")

        record_type, _ = STATE_PHASE[state]
        record = _validate_record(
            entry["record"],
            f"{path}.record",
            state,
            binding,
            expectations,
            prior_intent,
            prior_intent_generation,
            prior_intent_seal,
        )
        if state == "admitted":
            if record["intent_generation"] != ADMISSION_INTENT_GENERATION:
                _fail(
                    f"{path}.record.intent_generation",
                    f"admission must use sentinel generation {ADMISSION_INTENT_GENERATION}",
                )
            if record["intent_seal_digest"] != preflight["observed_lifecycle_seal_digest"]:
                _fail(
                    f"{path}.record.intent_seal_digest",
                    "admission must bind the lifecycle seal observed by the ready preflight",
                )
        if record_type == "intent":
            idempotency_key = record["external_idempotency_key"]
            earlier_phase = phase_idempotency_keys.get(idempotency_key)
            if earlier_phase is not None and earlier_phase != record["phase"]:
                _fail(
                    f"{path}.record.external_idempotency_key",
                    f"reuses an idempotency key from distinct phase {earlier_phase}",
                )
            phase_idempotency_keys[idempotency_key] = record["phase"]
        evidence = _validate_evidence(
            entry["evidence"],
            f"{path}.evidence",
            state,
            binding,
            preflight,
            expectations,
            record,
            previous_evidence_root,
            subjects,
            artifact_digests,
        )
        for item in evidence:
            artifact_types_by_digest[item["artifact_digest"]] = item["type"]
        if record_type == "result":
            phase_artifacts = sorted(item["artifact_digest"] for item in evidence)
            current_artifact_by_type = {item["type"]: item["artifact_digest"] for item in evidence}
            for outcome_index, outcome in enumerate(record["resource_outcomes"]):
                proof_type = artifact_types_by_digest.get(outcome["evidence_digest"])
                if proof_type is None:
                    _fail(
                        f"{path}.record.resource_outcomes[{outcome_index}].evidence_digest",
                        "resource outcome cites an unknown or future evidence artifact",
                    )
                allowed_types = OUTCOME_EVIDENCE_TYPES.get((outcome["kind"], outcome["outcome"]))
                if allowed_types is None or proof_type not in allowed_types:
                    _fail(
                        f"{path}.record.resource_outcomes[{outcome_index}].evidence_digest",
                        f"{outcome['kind']}={outcome['outcome']} cannot be proved by typed {proof_type} evidence",
                    )
                if state == "sealed-final":
                    terminal_type = TERMINAL_RESOURCE_EVIDENCE[outcome["kind"]]
                    if (
                        proof_type != terminal_type
                        or outcome["evidence_digest"] != current_artifact_by_type[terminal_type]
                    ):
                        _fail(
                            f"{path}.record.resource_outcomes[{outcome_index}].evidence_digest",
                            f"terminal {outcome['kind']} outcome must bind current typed {terminal_type} evidence",
                        )
            expected_effect_digest = canonical_digest(
                {
                    "planned_effect_digest": record["planned_effect_digest"],
                    "evidence_artifact_digests": phase_artifacts,
                }
            )
            if record["effect_digest"] != expected_effect_digest:
                _fail(f"{path}.record.effect_digest", "does not bind the exact result evidence")
            if record["outcome_digest"] != canonical_digest(record["resource_outcomes"]):
                _fail(f"{path}.record.outcome_digest", "does not bind the exact ordered resource outcomes")
        if state == "overlay-activated":
            by_type = {item["type"]: item["details"] for item in evidence}
            if by_type["overlay-cas"]["active_snapshot_digest"] != by_type["overlay-active-revalidation"]["active_snapshot_digest"]:
                _fail(f"{path}.evidence", "overlay revalidation does not bind the CAS-active snapshot")
        evidence_root = _digest(entry["evidence_root"], f"{path}.evidence_root")
        if evidence_root != canonical_digest(evidence):
            _fail(f"{path}.evidence_root", "does not bind the exact ordered evidence array")
        phase_root = _digest(entry["phase_root"], f"{path}.phase_root")
        expected_phase_root = canonical_digest(
            {"generation": generation, "state": state, "record": record, "evidence_root": evidence_root}
        )
        if phase_root != expected_phase_root:
            _fail(f"{path}.phase_root", "does not bind the exact phase record/evidence")
        persistence = _validate_persistence(
            entry["persistence"],
            f"{path}.persistence",
            state,
            phase_root,
            evidence_root,
            expectations,
            fsync_verifier,
        )

        cas = _object(entry["cas"], f"{path}.cas", _CAS_KEYS)
        event_id = _identifier(cas["event_id"], f"{path}.cas.event_id")
        if event_id in seen_event_ids:
            _fail(f"{path}.cas.event_id", "duplicate external CAS event ID")
        seen_event_ids.add(event_id)
        authority_time = _time_value(cas["authority_observed_at"], f"{path}.cas.authority_observed_at")
        if previous_authority_time is not None and authority_time < previous_authority_time:
            _fail(f"{path}.cas.authority_observed_at", "trusted lifecycle time regressed")
        if authority_time >= lease_deadline:
            _fail(f"{path}.cas.authority_observed_at", "lifecycle successor was observed at or after lease expiry")
        if state == "admitted" and not (preflight_evaluated_at <= authority_time < preflight_valid_until):
            _fail(
                f"{path}.cas.authority_observed_at",
                "admission was not externally committed inside the ready-preflight validity interval",
            )
        for key in ("previous_seal_digest", "state_digest", "seal_digest"):
            _digest(cas[key], f"{path}.cas.{key}")
        if cas["previous_seal_digest"] != previous_seal:
            _fail(f"{path}.cas.previous_seal_digest", "ancestry is forked or reordered")
        if cas["state_digest"] != _state_digest(entry):
            _fail(f"{path}.cas.state_digest", "does not bind the exact persisted phase state")
        if cas["seal_digest"] != _seal_digest(entry, cas):
            _fail(f"{path}.cas.seal_digest", "does not bind the external CAS successor")
        ancestry_item = {"generation": generation, "state": state, **cas}
        ancestry_projection.append(ancestry_item)
        if ancestry_item != expected_ancestry[index]:
            _fail(f"{path}.cas", "does not match the exact trusted full ancestry")

        if record_type == "intent":
            prior_intent = record
            prior_intent_generation = generation
            prior_intent_seal = cas["seal_digest"]
        elif state != "admitted":
            prior_intent = None
            prior_intent_generation = None
            prior_intent_seal = None
        previous_evidence_root = evidence_root
        previous_seal = cas["seal_digest"]
        previous_authority_time = authority_time

    if last_generation != first_generation + len(entries) - 1:
        _fail("$.last_generation", "does not match the ordered entries")
    if ancestry_projection != expected_ancestry:
        _fail("$.entries", "does not reproduce the complete trusted ancestry")

    manifest_root = _digest(root["manifest_root"], "$.manifest_root")
    computed_root = compute_manifest_root(root)
    if manifest_root != computed_root:
        _fail("$.manifest_root", "does not bind the complete ordered manifest")
    ancestry_root = canonical_digest(ancestry_projection)

    final_seal = _object(
        root["final_seal"],
        "$.final_seal",
        {
            "manifest_root",
            "entry_count",
            "first_generation",
            "last_generation",
            "terminal_state",
            "terminal_seal_digest",
            "preflight",
            "ancestry",
            "ancestry_root",
            "final_authority_digest",
        },
    )
    _integer(final_seal["entry_count"], "$.final_seal.entry_count", minimum=1)
    _integer(final_seal["first_generation"], "$.final_seal.first_generation", minimum=1)
    _integer(final_seal["last_generation"], "$.final_seal.last_generation", minimum=1)
    _identifier(final_seal["terminal_state"], "$.final_seal.terminal_state")
    if final_seal["manifest_root"] != manifest_root:
        _fail("$.final_seal.manifest_root", "does not bind the complete manifest")
    if (
        final_seal["entry_count"] != len(entries)
        or final_seal["first_generation"] != first_generation
        or final_seal["last_generation"] != last_generation
        or final_seal["terminal_state"] != "sealed-final"
        or final_seal["terminal_seal_digest"] != previous_seal
    ):
        _fail("$.final_seal", "does not bind the exact terminal range/state/seal")
    if final_seal["preflight"] != preflight:
        _fail("$.final_seal.preflight", "does not bind the exact ready preflight")
    if final_seal["ancestry"] != ancestry_projection or final_seal["ancestry_root"] != ancestry_root:
        _fail("$.final_seal.ancestry", "does not bind the exact ordered full ancestry")
    final_authority_digest = _digest(final_seal["final_authority_digest"], "$.final_seal.final_authority_digest")

    terminal_evidence = entries[-1]["evidence"]
    evidence_by_type = {item["type"]: item for item in terminal_evidence}
    ancestry_details = evidence_by_type["complete-ancestry"]["details"]
    predecessor_ancestry = ancestry_projection[:-1]
    expected_ancestry_details = {
        "predecessor_ancestry_root": canonical_digest(predecessor_ancestry),
        "predecessor_entry_count": len(predecessor_ancestry),
        "first_generation": first_generation,
        "predecessor_generation": last_generation - 1,
        "predecessor_seal_digest": ancestry_projection[-2]["seal_digest"],
    }
    if ancestry_details != expected_ancestry_details:
        _fail("$.entries[-1].evidence.complete-ancestry", "does not bind full ancestry")
    authority_details = evidence_by_type["final-authority"]["details"]
    if authority_details["authority_digest"] != final_authority_digest:
        _fail("$.final_seal.final_authority_digest", "does not bind final-authority evidence")

    signature = _object(
        root["signature"],
        "$.signature",
        {"algorithm", "key_id", "signed_manifest_root", "signed_final_seal_digest", "value"},
    )
    if signature["algorithm"] != expectations.signature_algorithm or signature["key_id"] != expectations.signature_key_id:
        _fail("$.signature", "signature algorithm/key is not trusted")
    final_seal_digest = canonical_digest(final_seal)
    if signature["signed_manifest_root"] != manifest_root:
        _fail("$.signature.signed_manifest_root", "signature does not bind the manifest root")
    if signature["signed_final_seal_digest"] != final_seal_digest:
        _fail("$.signature.signed_final_seal_digest", "signature does not bind the final seal")
    _string(signature["value"], "$.signature.value")
    try:
        verified = signature_verifier(
            _canonical({"final_seal_digest": final_seal_digest, "manifest_root": manifest_root}),
            signature,
        )
    except Exception as exc:
        raise EvidenceValidationError(f"$.signature: trusted verifier failed: {exc}") from exc
    if verified is not True:
        _fail("$.signature", "trusted signature verifier rejected the manifest")

    return ValidatedEvidenceManifest(
        manifest_root=manifest_root,
        ancestry_root=ancestry_root,
        first_generation=first_generation,
        last_generation=last_generation,
        terminal_seal_digest=previous_seal,
    )
