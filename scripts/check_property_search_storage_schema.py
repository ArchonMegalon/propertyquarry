#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
APP_SOURCE_ROOT = EA_ROOT if (EA_ROOT / "app").is_dir() else ROOT
for source_root in (ROOT, ROOT / "scripts", APP_SOURCE_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

STORAGE_SOURCE = APP_SOURCE_ROOT / "app" / "product" / "property_search_storage.py"
QUEUE_SOURCE = APP_SOURCE_ROOT / "app" / "product" / "property_search_work_queue.py"
SCHEMA_SOURCE = APP_SOURCE_ROOT / "app" / "product" / "property_search_schema.py"
DELIVERY_OUTBOX_SOURCE = (
    APP_SOURCE_ROOT / "app" / "repositories" / "delivery_outbox_postgres.py"
)
CONTENT_LEDGER_SOURCE = (
    APP_SOURCE_ROOT / "app" / "services" / "property_content_job_ledger.py"
)
SERVICE_SOURCE = APP_SOURCE_ROOT / "app" / "product" / "service.py"
PACKET_LINK_SOURCE = (
    APP_SOURCE_ROOT / "app" / "product" / "property_research_packet_links.py"
)
PRIVACY_STORAGE_SOURCE = (
    APP_SOURCE_ROOT / "app" / "product" / "privacy_lifecycle_storage.py"
)
from app.product.property_research_packet_fleet_proof import (  # noqa: E402
    PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT,
    PROPERTY_RESEARCH_PACKET_WRITER_READY_STATUSES,
    parse_property_research_packet_proof_timestamp,
    property_research_packet_fleet_proof_sha256,
    validate_property_research_packet_fleet_proof,
)


FLEET_PROOF_CONTRACT = PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT
_HEARTBEAT_COMMON_KEYS = frozenset(
    {
        "instance_id",
        "started_at_epoch",
        "role",
        "status",
        "writer_ready",
        "epoch",
        "observed_at",
        "pid",
        "profile",
        "property_search_writer_contract",
    }
)
_HEARTBEAT_DELIVERY_OUTBOX_KEYS = frozenset(
    {
        "queued",
        "claimed",
        "claim_conflicts",
        "sent",
        "retried",
        "dead_lettered",
        "failed",
    }
)
_HEARTBEAT_ROLE_KEYS = {
    "api": frozenset(),
    "worker": frozenset({"property_search_work_queue"}),
    "scheduler": frozenset({"delivery_outbox"}),
}
_HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_KEYS = {
    False: frozenset({"observed"}),
    True: frozenset({"observed", "depth", "oldest_item_age_seconds"}),
}
_HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_MAX_DEPTH = (2**63) - 1
_HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60


def _validate_property_search_work_queue_heartbeat(value: object) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("writer_heartbeat_property_search_work_queue_invalid")
    observed = value.get("observed")
    if type(observed) is not bool:
        raise RuntimeError("writer_heartbeat_property_search_work_queue_invalid")
    if frozenset(value) != _HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_KEYS[observed]:
        raise RuntimeError("writer_heartbeat_property_search_work_queue_invalid")
    if not observed:
        return
    depth = value.get("depth")
    oldest_item_age_seconds = value.get("oldest_item_age_seconds")
    if (
        type(depth) is not int
        or depth < 0
        or depth > _HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_MAX_DEPTH
        or isinstance(oldest_item_age_seconds, bool)
        or not isinstance(oldest_item_age_seconds, (int, float))
        or not math.isfinite(float(oldest_item_age_seconds))
        or oldest_item_age_seconds < 0
        or oldest_item_age_seconds
        > _HEARTBEAT_PROPERTY_SEARCH_WORK_QUEUE_MAX_AGE_SECONDS
    ):
        raise RuntimeError("writer_heartbeat_property_search_work_queue_invalid")


def _declared_migration_contracts(source: str) -> tuple[tuple[int, str], ...]:
    """Read the version/name ledger without depending on source formatting."""

    tree = ast.parse(source, filename=str(SCHEMA_SOURCE))
    declaration: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "PROPERTY_SEARCH_MIGRATIONS":
                declaration = node.value
                break
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "PROPERTY_SEARCH_MIGRATIONS"
            for target in node.targets
        ):
            declaration = node.value
            break
    if not isinstance(declaration, (ast.Tuple, ast.List)):
        raise RuntimeError("migration_contract_declaration_missing")

    contracts: list[tuple[int, str]] = []
    for item in declaration.elts:
        if (
            not isinstance(item, ast.Call)
            or not isinstance(item.func, ast.Name)
            or item.func.id != "PropertySearchMigration"
            or len(item.args) != 3
            or item.keywords
            or not isinstance(item.args[0], ast.Constant)
            or type(item.args[0].value) is not int
            or not isinstance(item.args[1], ast.Constant)
            or type(item.args[1].value) is not str
        ):
            raise RuntimeError("migration_contract_entry_invalid")
        contracts.append((item.args[0].value, item.args[1].value))
    return tuple(contracts)


def _check_source_contracts() -> None:
    storage = STORAGE_SOURCE.read_text(encoding="utf-8")
    queue = QUEUE_SOURCE.read_text(encoding="utf-8")
    schema = SCHEMA_SOURCE.read_text(encoding="utf-8")
    delivery_outbox = DELIVERY_OUTBOX_SOURCE.read_text(encoding="utf-8")
    content_ledger = CONTENT_LEDGER_SOURCE.read_text(encoding="utf-8")
    service = SERVICE_SOURCE.read_text(encoding="utf-8")
    packet_links = PACKET_LINK_SOURCE.read_text(encoding="utf-8")
    privacy_storage = PRIVACY_STORAGE_SOURCE.read_text(encoding="utf-8")

    required_storage_fragments = (
        "ON CONFLICT (principal_id, run_id) DO UPDATE",
        "WHERE run_id = %s AND principal_id = %s",
        "DELETE FROM property_search_runs WHERE run_id = %s AND principal_id = %s",
        "payload_retention_status",
        "compact_only",
        "UPDATE property_search_runs AS runs",
        "COALESCE(NULLIF(compact_json, '{{}}'::jsonb)",
        "if not normalized_principal_id and not admin:\n        return ()",
        "def _require_property_search_run_schema()",
        "require_property_search_schema_ready(database_url)",
        "project_property_research_packet_links(normalized_record)",
        "upsert_property_research_packet_links(cur, packet_links)",
        "def _load_property_research_packet_link(",
        "propertyquarry.property_search_writer_contract",
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "propertyquarry.property_search_erasure_key_id",
        "hmac.new(",
        "def _record_property_search_erasure_fence(",
        "SELECT property_search_assert_erasure_key()",
        "INSERT INTO property_search_erasure_fences",
        "DELETE FROM property_search_work_jobs",
        "principal_ids: tuple[str, ...]",
    )
    for fragment in required_storage_fragments:
        if fragment not in storage:
            raise RuntimeError(f"missing_storage_contract:{fragment[:80]}")

    forbidden_storage_fragments = (
        "ON CONFLICT (run_id)",
        "SET principal_id = EXCLUDED.principal_id",
        "SELECT payload_json FROM property_search_runs WHERE run_id = %s\"",
        "DELETE FROM property_search_runs WHERE run_id = %s\"",
        "(payload_json->>'status') = ANY(%s)",
    )
    for fragment in forbidden_storage_fragments:
        if fragment in storage:
            raise RuntimeError(f"forbidden_storage_contract:{fragment}")

    for forbidden_ddl in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX"):
        if (
            forbidden_ddl in storage.upper()
            or forbidden_ddl in queue.upper()
            or forbidden_ddl in delivery_outbox.upper()
            or forbidden_ddl in content_ledger.upper()
            or forbidden_ddl in privacy_storage.upper()
        ):
            raise RuntimeError(f"runtime_schema_ddl_forbidden:{forbidden_ddl}")

    for fragment in (
        "def resolve_privacy_lifecycle_storage_backend(",
        "propertyquarry_privacy_postgres_required",
        "propertyquarry_privacy_storage_backend_required",
        "INSERT INTO property_account_privacy_requests",
    ):
        if fragment not in privacy_storage:
            raise RuntimeError(f"missing_privacy_storage_contract:{fragment[:80]}")
    if "def _ensure_schema(" in privacy_storage:
        raise RuntimeError("runtime_privacy_schema_ddl_forbidden")

    required_queue_fragments = (
        "class PostgresPropertySearchWorkQueue",
        "require_property_search_schema_ready(self._database_url)",
        "def _set_writer_contract(cursor: object) -> None:",
        "_set_property_search_writer_contract(cursor)",
        "self._set_writer_contract(cur)",
        "_CLAIM_CANDIDATE_SCAN_LIMIT = 32",
        "def _nonlocking_job_identity(",
        "def _nonlocking_claim_candidate_job_ids(",
        "self._acquire_principal_write_authority(",
        "project_property_research_packet_links(normalized)",
        "upsert_property_research_packet_links(cur, packet_links)",
        "sync_property_research_packet_run_memberships(",
        "refresh_property_research_packet_links_for_refs(",
        "jobs.principal_id = %s",
        "jobs.run_id = %s",
        "property_search_work_jobs",
        "principal_key = _property_search_principal_key(principal_id)",
    )
    for fragment in required_queue_fragments:
        if fragment not in queue:
            raise RuntimeError(f"missing_queue_contract:{fragment[:80]}")
    if "FOR UPDATE SKIP LOCKED" in queue:
        raise RuntimeError("forbidden_queue_contract:FOR UPDATE SKIP LOCKED")

    required_schema_fragments = (
        "SCHEMA_LEDGER_TABLE = \"propertyquarry_schema_migrations\"",
        "pg_advisory_xact_lock",
        "checksum_sha256",
        "property_search_migration_checksum_drift",
        "required_relation_missing",
        "property_search_runs_writer_contract_guard",
        "property_search_work_jobs_erasure_fence_guard",
        "property_search_account_erased",
        "property_search_erasure_key_state",
        "property_search_assert_erasure_key",
        "property_search_erasure_key_mismatch",
        "property_search_erasure_key_state_immutable_guard",
        "durable_property_search_erasure_fences",
        "property_content_account_ownership_fence",
        "property_content_polymorphic_authority_trigger_fix",
        "property_research_packet_erasure_trigger_split",
        "durable_property_account_privacy_lifecycle",
        "distributed_request_admission_control",
        "property_account_privacy_requests",
        "propertyquarry_admission_quota_buckets",
        "propertyquarry_admission_leases",
        "property_content_enforce_account_authority",
        "embedded_row := to_jsonb(NEW)->'row_json'",
        "property_content_jobs_account_authority_guard",
        "property_content_job_events_account_authority_guard",
        "property_content_webhook_account_authority_guard",
        "property_research_packet_links_enforce_erasure_fence",
        "property_research_packet_memberships_enforce_erasure_fence",
        "DROP FUNCTION IF EXISTS property_research_packets_enforce_erasure_fence()",
        "required_trigger_missing",
    )
    for fragment in required_schema_fragments:
        if fragment not in schema:
            raise RuntimeError(f"missing_migration_contract:{fragment[:80]}")

    link_trigger_body = schema.split(
        "CREATE OR REPLACE FUNCTION "
        "property_research_packet_links_enforce_erasure_fence()",
        1,
    )[1].split(
        "$property_research_packet_links_erasure_fence_function$;",
        1,
    )[0]
    if "NEW.run_id" in link_trigger_body or "OLD.run_id" in link_trigger_body:
        raise RuntimeError("packet_link_trigger_references_absent_run_id")
    if "NEW.last_run_id" not in link_trigger_body:
        raise RuntimeError("packet_link_trigger_missing_last_run_id_authority")

    membership_trigger_body = schema.split(
        "CREATE OR REPLACE FUNCTION "
        "property_research_packet_memberships_enforce_erasure_fence()",
        1,
    )[1].split(
        "$property_research_packet_memberships_erasure_fence_function$;",
        1,
    )[0]
    for fragment in (
        "NEW.run_id IS DISTINCT FROM OLD.run_id",
        "NEW.run_id",
        "property_search_assert_principal_write_allowed",
    ):
        if fragment not in membership_trigger_body:
            raise RuntimeError(
                f"packet_membership_trigger_authority_missing:{fragment}"
            )
    expected_migrations = (
        (1, "property_search_runs_tenant_schema"),
        (2, "property_search_durable_work_queue"),
        (3, "property_source_listing_cache"),
        (4, "replica_safe_delivery_outbox"),
        (5, "durable_property_content_job_ledger"),
        (6, "bounded_run_delivery_projection"),
        (7, "tenant_scoped_delivery_outbox_idempotency"),
        (8, "property_evidence_overlay_cached_read_model"),
        (9, "property_evidence_overlay_staged_snapshot_activation"),
        (10, "tenant_scoped_property_research_packet_links"),
        (11, "durable_property_search_erasure_fences"),
        (12, "property_content_account_ownership_fence"),
        (13, "property_content_polymorphic_authority_trigger_fix"),
        (14, "property_research_packet_erasure_trigger_split"),
        (15, "durable_property_account_privacy_lifecycle"),
        (16, "distributed_request_admission_control"),
        (17, "bounded_admission_capacity_state"),
    )
    declared_migrations = _declared_migration_contracts(schema)
    if declared_migrations != expected_migrations:
        raise RuntimeError(
            "property_search_migration_contract_drift:"
            f"expected={expected_migrations!r}:actual={declared_migrations!r}"
        )

    for fragment in (
        "pg_try_advisory_xact_lock",
        "FOR UPDATE SKIP LOCKED",
        "status = 'dispatching'",
        "delivery_outcome_unknown_after_lease_expiry",
        "require_property_search_schema_ready(self._database_url)",
    ):
        if fragment not in delivery_outbox:
            raise RuntimeError(f"missing_delivery_outbox_contract:{fragment[:80]}")

    for fragment in (
        "class _PostgresPropertyContentRepository",
        "require_property_search_schema_ready(self.database_url)",
        "pg_try_advisory_xact_lock",
        "FOR UPDATE SKIP LOCKED",
        "property_content_webhook_events",
        "PropertyContentLedgerCorruptionError",
        "fcntl.flock",
    ):
        if fragment not in content_ledger:
            raise RuntimeError(f"missing_content_ledger_contract:{fragment[:80]}")

    required_service_fragments = (
        "def list_property_search_runs(",
        "if not normalized_principal:\n            return []",
        "str(record.get(\"principal_id\") or \"\").strip() != normalized_principal",
        "def clear_property_search_runs(",
        "principal_ids=aliases",
        '"work_jobs_deleted": work_jobs_deleted',
    )
    for fragment in required_service_fragments:
        if fragment not in service:
            raise RuntimeError(f"missing_service_contract:{fragment[:80]}")

    for fragment in (
        "PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION = 3",
        "PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES",
        "packet_canonical_json",
        "packet_size_bytes",
        "sync_property_research_packet_run_memberships",
        "packet_candidate_ref_mismatch",
        "packet_property_url_sha256_mismatch",
    ):
        if fragment not in packet_links:
            raise RuntimeError(f"missing_packet_link_contract:{fragment[:80]}")


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expected_writer_instances(raw: str) -> tuple[tuple[str, str], ...]:
    expected: list[tuple[str, str]] = []
    for item in str(raw or "").split(","):
        role, separator, instance_id = item.strip().partition(":")
        normalized_role = role.strip().lower()
        normalized_instance = instance_id.strip()
        if not separator or normalized_role not in {"api", "worker", "scheduler"} or not normalized_instance:
            raise RuntimeError("writer_instance_manifest_invalid")
        identity = (normalized_role, normalized_instance)
        if identity in expected:
            raise RuntimeError("writer_instance_manifest_duplicate")
        expected.append(identity)
    if not expected:
        raise RuntimeError("writer_instance_manifest_required")
    return tuple(sorted(expected))


def _canonical_sha256(value: dict[str, object]) -> str:
    return property_research_packet_fleet_proof_sha256(value)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_private_json(path: Path) -> dict[str, object]:
    if path.stat().st_mode & 0o077:
        raise RuntimeError("fleet_proof_permissions_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("fleet_proof_not_object")
    return dict(payload)


def _validate_writer_fleet(
    *,
    heartbeat_dir: Path,
    expected_instances: tuple[tuple[str, str], ...],
    not_before: datetime,
    max_age_seconds: int,
) -> list[dict[str, object]]:
    from app.product.property_research_packet_links import (
        PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    )
    from app.product.property_search_schema import LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    from app.product.property_search_storage import _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION

    now = datetime.now(timezone.utc)
    expected_contract = {
        "compact_schema_version": _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "research_packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        "property_search_schema_version": LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
    }
    observed: dict[tuple[str, str], dict[str, object]] = {}
    if not heartbeat_dir.is_dir():
        raise RuntimeError("writer_heartbeat_directory_missing")
    for path in sorted(heartbeat_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("writer_heartbeat_invalid_json") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("writer_heartbeat_not_object")
        role = payload.get("role")
        if not isinstance(role, str) or role not in _HEARTBEAT_ROLE_KEYS:
            raise RuntimeError("writer_heartbeat_role_invalid")
        expected_keys = _HEARTBEAT_COMMON_KEYS | _HEARTBEAT_ROLE_KEYS[role]
        if frozenset(payload) != expected_keys:
            raise RuntimeError("writer_heartbeat_schema_invalid")
        if role == "scheduler":
            delivery_outbox = payload.get("delivery_outbox")
            if (
                not isinstance(delivery_outbox, dict)
                or frozenset(delivery_outbox) != _HEARTBEAT_DELIVERY_OUTBOX_KEYS
                or any(
                    type(value) is not int or value < 0
                    for value in delivery_outbox.values()
                )
            ):
                raise RuntimeError("writer_heartbeat_delivery_outbox_invalid")
        if role == "worker":
            _validate_property_search_work_queue_heartbeat(
                payload.get("property_search_work_queue")
            )
        instance_id = payload.get("instance_id")
        if (
            not isinstance(instance_id, str)
            or not instance_id
            or instance_id != instance_id.strip()
            or len(instance_id) > 256
        ):
            raise RuntimeError("writer_heartbeat_instance_invalid")
        observed_at = _parse_timestamp(payload.get("observed_at"))
        observed_at_raw = payload.get("observed_at")
        if observed_at is None:
            raise RuntimeError("writer_heartbeat_timestamp_invalid")
        canonical_observed_at = (
            observed_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        if observed_at_raw != canonical_observed_at:
            raise RuntimeError("writer_heartbeat_timestamp_not_canonical")
        started_at_epoch = payload.get("started_at_epoch")
        heartbeat_epoch = payload.get("epoch")
        if (
            isinstance(started_at_epoch, bool)
            or not isinstance(started_at_epoch, (int, float))
            or not math.isfinite(float(started_at_epoch))
            or isinstance(heartbeat_epoch, bool)
            or not isinstance(heartbeat_epoch, (int, float))
            or not math.isfinite(float(heartbeat_epoch))
        ):
            raise RuntimeError("writer_heartbeat_epoch_invalid")
        try:
            started_at = datetime.fromtimestamp(float(started_at_epoch), timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise RuntimeError("writer_heartbeat_epoch_invalid") from None
        if abs(float(heartbeat_epoch) - observed_at.timestamp()) >= 1.0:
            raise RuntimeError("writer_heartbeat_epoch_mismatch")
        if observed_at > now + timedelta(seconds=5):
            raise RuntimeError("writer_heartbeat_timestamp_in_future")
        if (now - observed_at).total_seconds() > max_age_seconds:
            continue
        identity = (role, instance_id)
        if identity not in expected_instances:
            raise RuntimeError("unexpected_live_writer_instance")
        if identity in observed:
            raise RuntimeError("duplicate_live_writer_heartbeat")
        if started_at < not_before:
            raise RuntimeError("writer_started_before_coordinated_restart")
        if started_at > observed_at:
            raise RuntimeError("writer_heartbeat_started_after_observation")
        status = payload.get("status")
        expected_ready = (
            isinstance(status, str)
            and status in PROPERTY_RESEARCH_PACKET_WRITER_READY_STATUSES[role]
        )
        if payload.get("writer_ready") is not True or not expected_ready:
            raise RuntimeError("writer_heartbeat_not_live")
        pid = payload.get("pid")
        profile = payload.get("profile")
        if type(pid) is not int or pid <= 0:
            raise RuntimeError("writer_heartbeat_pid_invalid")
        if (
            not isinstance(profile, str)
            or len(profile) > 256
            or profile != profile.strip().lower()
        ):
            raise RuntimeError("writer_heartbeat_profile_invalid")
        if role != "scheduler" and profile:
            raise RuntimeError("writer_heartbeat_profile_invalid")
        writer_contract = payload.get("property_search_writer_contract")
        if (
            not isinstance(writer_contract, dict)
            or frozenset(writer_contract) != frozenset(expected_contract)
            or any(type(value) is not int for value in writer_contract.values())
            or writer_contract != expected_contract
        ):
            raise RuntimeError("writer_contract_mismatch")
        observed[identity] = dict(payload)
    missing = set(expected_instances) - set(observed)
    if missing:
        raise RuntimeError("expected_writer_heartbeat_missing")
    return [
        {
            "role": role,
            "instance_id": instance_id,
            "started_at_epoch": observed[(role, instance_id)]["started_at_epoch"],
        }
        for role, instance_id in expected_instances
    ]


def _validate_activation_fleet_proof(
    proof: dict[str, object],
    *,
    observed_instances: list[dict[str, object]],
    not_before: datetime,
) -> dict[str, object]:
    from app.product.property_research_packet_links import (
        PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    )
    from app.product.property_search_schema import LATEST_PROPERTY_SEARCH_SCHEMA_VERSION

    try:
        payload = validate_property_research_packet_fleet_proof(
            proof,
            property_search_schema_version=LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            writer_contract_version=PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
            packet_schema_version=PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
            require_unexpired=False,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if payload["expected_instances"] != observed_instances:
        raise RuntimeError("fleet_proof_current_instances_mismatch")
    proof_not_before = parse_property_research_packet_proof_timestamp(
        payload.get("rollout_not_before")
    )
    if proof_not_before != not_before.astimezone(timezone.utc):
        raise RuntimeError("fleet_proof_rollout_not_before_mismatch")
    return payload


def _inspect_index_coverage(database_url: str) -> dict[str, object]:
    import psycopg

    connection = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT coverage_status,
                       writer_contract_version,
                       packet_schema_version,
                       expected_membership_rows,
                       verified_membership_rows,
                       expected_distinct_tenant_refs,
                       verified_distinct_tenant_refs,
                       fleet_proof_sha256
                FROM property_research_packet_index_state
                WHERE singleton = TRUE
                """
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        raise RuntimeError("packet_index_state_missing")
    return {
        "coverage_status": str(row[0] or ""),
        "writer_contract_version": int(row[1] or 0),
        "packet_schema_version": int(row[2] or 0),
        "expected_membership_rows": int(row[3] or 0),
        "verified_membership_rows": int(row[4] or 0),
        "expected_distinct_tenant_refs": int(row[5] or 0),
        "verified_distinct_tenant_refs": int(row[6] or 0),
        "fleet_proof_sha256": str(row[7] or "").strip(),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check PropertyQuarry storage and rollout contracts.")
    parser.add_argument(
        "--phase",
        choices=("source", "pre-backfill", "activate"),
        default=str(os.environ.get("EA_PROPERTY_SEARCH_ROLLOUT_PHASE") or "source"),
    )
    parser.add_argument("--require-live-db", action="store_true")
    parser.add_argument("--heartbeat-dir", type=Path)
    parser.add_argument("--expected-writers", default="")
    parser.add_argument("--not-before", default="")
    parser.add_argument("--heartbeat-max-age-seconds", type=int, default=120)
    parser.add_argument("--fleet-proof-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _check_source_contracts()
    require_live_db = bool(
        args.require_live_db
        or args.phase != "source"
        or _truthy(os.environ.get("EA_PROPERTY_SEARCH_LIVE_DB_REQUIRED"))
    )
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        if require_live_db:
            raise RuntimeError("database_url_required_for_live_schema_check")
        print(
            "property search storage source contracts look ready; "
            "DATABASE_URL is not set, skipping optional live schema check."
        )
        return 0

    from app.product.property_research_packet_links import (
        PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    )
    from app.product.property_search_schema import (
        LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
        inspect_property_search_schema,
    )

    status = inspect_property_search_schema(database_url)
    if not status.ready:
        raise RuntimeError(f"property_search_schema_not_ready:{status.reason}")
    if args.phase == "source":
        print(f"property search storage schema looks ready at version {status.current_version}")
        return 0

    expected_raw = str(
        args.expected_writers
        or os.environ.get("EA_PROPERTY_SEARCH_EXPECTED_WRITER_INSTANCES")
        or ""
    )
    expected_instances = _expected_writer_instances(expected_raw)
    not_before = _parse_timestamp(
        args.not_before or os.environ.get("EA_PROPERTY_SEARCH_ROLLOUT_NOT_BEFORE")
    )
    if not_before is None:
        raise RuntimeError("rollout_not_before_required")
    heartbeat_dir = args.heartbeat_dir or Path(
        str(
            os.environ.get("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR")
            or "/data/artifacts/propertyquarry-writer-heartbeats"
        )
    )
    observed_instances = _validate_writer_fleet(
        heartbeat_dir=heartbeat_dir,
        expected_instances=expected_instances,
        not_before=not_before,
        max_age_seconds=max(15, min(int(args.heartbeat_max_age_seconds), 600)),
    )
    if args.fleet_proof_path is None:
        raise RuntimeError("fleet_proof_path_required")
    if args.phase == "pre-backfill":
        now = datetime.now(timezone.utc)
        proof = {
            "contract": FLEET_PROOF_CONTRACT,
            "status": "ready",
            "generated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "rollout_not_before": not_before.isoformat(),
            "property_search_schema_version": LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
            "packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
            "expected_instances": observed_instances,
        }
        try:
            proof = validate_property_research_packet_fleet_proof(
                proof,
                property_search_schema_version=LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                writer_contract_version=PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
                packet_schema_version=PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        _write_private_json(args.fleet_proof_path, proof)
        print(json.dumps({"status": "ready", "fleet_proof_sha256": _canonical_sha256(proof)}, sort_keys=True))
        return 0

    proof = _read_private_json(args.fleet_proof_path)
    proof = _validate_activation_fleet_proof(
        proof,
        observed_instances=observed_instances,
        not_before=not_before,
    )
    coverage = _inspect_index_coverage(database_url)
    if coverage["coverage_status"] != "complete":
        raise RuntimeError("packet_index_coverage_not_complete")
    if coverage["writer_contract_version"] != PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION:
        raise RuntimeError("packet_index_writer_contract_mismatch")
    if coverage["packet_schema_version"] != PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION:
        raise RuntimeError("packet_index_schema_mismatch")
    if coverage["expected_membership_rows"] != coverage["verified_membership_rows"]:
        raise RuntimeError("packet_index_membership_coverage_mismatch")
    if coverage["expected_distinct_tenant_refs"] != coverage["verified_distinct_tenant_refs"]:
        raise RuntimeError("packet_index_ref_coverage_mismatch")
    if coverage["fleet_proof_sha256"] != _canonical_sha256(proof):
        raise RuntimeError("packet_index_fleet_proof_mismatch")
    print(json.dumps({"status": "activation_ready", **coverage}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
