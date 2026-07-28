from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Mapping


PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT = (
    "property_research_packet_writer_fleet_proof_v1"
)
PROPERTY_RESEARCH_PACKET_WRITER_READY_STATUSES = {
    "api": frozenset({"serving"}),
    "worker": frozenset({"loop"}),
    "scheduler": frozenset(
        {
            "loop",
            "idle",
            "property_results_finalize_running",
            "property_scout_running",
            "property_search_recovery_running",
        }
    ),
}
_FLEET_PROOF_KEYS = frozenset(
    {
        "contract",
        "status",
        "generated_at",
        "expires_at",
        "rollout_not_before",
        "property_search_schema_version",
        "writer_contract_version",
        "packet_schema_version",
        "expected_instances",
    }
)
_FLEET_PROOF_INSTANCE_KEYS = frozenset(
    {"role", "instance_id", "started_at_epoch"}
)
_WRITER_ROLES = frozenset({"api", "worker", "scheduler"})
_MAX_PROOF_LIFETIME = timedelta(minutes=15)
_MAX_CLOCK_SKEW = timedelta(seconds=5)


def parse_property_research_packet_proof_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def property_research_packet_fleet_proof_sha256(
    proof: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        dict(proof),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_property_research_packet_fleet_proof(
    proof: Mapping[str, object] | None,
    *,
    property_search_schema_version: int,
    writer_contract_version: int,
    packet_schema_version: int,
    now: datetime | None = None,
    require_unexpired: bool = True,
) -> dict[str, object]:
    """Validate the complete, closed rollout-fleet attestation contract."""

    if not isinstance(proof, Mapping):
        raise ValueError("fleet_proof_missing_or_invalid")
    payload = dict(proof)
    if frozenset(payload) != _FLEET_PROOF_KEYS:
        raise ValueError("fleet_proof_schema_invalid")
    if (
        payload.get("contract") != PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT
        or payload.get("status") != "ready"
    ):
        raise ValueError("fleet_proof_missing_or_invalid")
    version_contracts = (
        ("writer_contract_version", writer_contract_version, "fleet_proof_writer_contract_mismatch"),
        ("packet_schema_version", packet_schema_version, "fleet_proof_packet_schema_mismatch"),
        (
            "property_search_schema_version",
            property_search_schema_version,
            "fleet_proof_database_schema_mismatch",
        ),
    )
    for key, expected, error in version_contracts:
        if type(payload.get(key)) is not int or payload[key] != expected:
            raise ValueError(error)

    generated_at = parse_property_research_packet_proof_timestamp(
        payload.get("generated_at")
    )
    expires_at = parse_property_research_packet_proof_timestamp(payload.get("expires_at"))
    rollout_not_before = parse_property_research_packet_proof_timestamp(
        payload.get("rollout_not_before")
    )
    if generated_at is None or expires_at is None or rollout_not_before is None:
        raise ValueError("fleet_proof_timestamp_invalid")
    if rollout_not_before > generated_at:
        raise ValueError("fleet_proof_rollout_timestamp_invalid")
    if expires_at <= generated_at or expires_at - generated_at > _MAX_PROOF_LIFETIME:
        raise ValueError("fleet_proof_lifetime_invalid")
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if generated_at > observed_now + _MAX_CLOCK_SKEW:
        raise ValueError("fleet_proof_generated_in_future")
    if require_unexpired and expires_at <= observed_now:
        raise ValueError("fleet_proof_expired")

    expected_instances = payload.get("expected_instances")
    if not isinstance(expected_instances, list) or not expected_instances:
        raise ValueError("fleet_proof_expected_instances_missing")
    identities: list[tuple[str, str]] = []
    for raw_row in expected_instances:
        if not isinstance(raw_row, Mapping):
            raise ValueError("fleet_proof_instance_schema_invalid")
        row = dict(raw_row)
        if frozenset(row) != _FLEET_PROOF_INSTANCE_KEYS:
            raise ValueError("fleet_proof_instance_schema_invalid")
        role = row.get("role")
        instance_id = row.get("instance_id")
        if (
            not isinstance(role, str)
            or role not in _WRITER_ROLES
            or not isinstance(instance_id, str)
            or not instance_id.strip()
            or instance_id != instance_id.strip()
            or len(instance_id) > 256
        ):
            raise ValueError("fleet_proof_instance_identity_invalid")
        started_at_epoch = row.get("started_at_epoch")
        if (
            isinstance(started_at_epoch, bool)
            or not isinstance(started_at_epoch, (int, float))
        ):
            raise ValueError("fleet_proof_instance_timestamp_invalid")
        try:
            started_at = datetime.fromtimestamp(float(started_at_epoch), timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError("fleet_proof_instance_timestamp_invalid") from None
        if (
            started_at < rollout_not_before
            or started_at > generated_at + _MAX_CLOCK_SKEW
        ):
            raise ValueError("fleet_proof_instance_timestamp_invalid")
        identity = (role, instance_id)
        if identity in identities:
            raise ValueError("fleet_proof_instance_duplicate")
        identities.append(identity)
    if identities != sorted(identities):
        raise ValueError("fleet_proof_instances_not_canonical")
    return payload
