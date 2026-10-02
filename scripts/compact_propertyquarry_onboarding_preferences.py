#!/usr/bin/env python3
"""Safely compact recursively nested PropertyQuarry onboarding preferences.

The command is deliberately dry-run by default.  An apply run requires an
explicit, previously unused backup path and writes a mode-0600 JSON backup of
the complete onboarding row before updating PostgreSQL.

No principal id, database URL, preference value, listing URL, or other user
payload is emitted in the report.  The principal is represented by a short
SHA-256 digest only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence


BACKUP_SCHEMA = "propertyquarry.onboarding_preferences_backup.v1"
REPORT_SCHEMA = "propertyquarry.onboarding_preferences_compaction.v1"
DEFAULT_PRINCIPAL_ENV = "PROPERTYQUARRY_LIVE_PRINCIPAL_ID"
DEFAULT_DATABASE_ENV = "DATABASE_URL"
RAW_RESERVED_KEYS = frozenset(
    {
        "raw_preferences",
        "saved_shortlist_candidates",
        "search_agents",
    }
)
RAW_PREFERENCES_MAX_DEPTH = 256
ONBOARDING_COLUMNS = (
    "onboarding_id",
    "principal_id",
    "workspace_name",
    "workspace_mode",
    "region",
    "language",
    "timezone",
    "selected_channels_json",
    "property_search_preferences_json",
    "privacy_preferences_json",
    "channel_preferences_json",
    "brief_preview_json",
    "status",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class CompactionPlan:
    compacted: dict[str, object]
    before_sha256: str
    after_sha256: str
    before_json_bytes: int
    after_json_bytes: int
    raw_nesting_depth: int
    top_saved_shortlist_count: int
    top_search_agent_count: int
    raw_only_key_count: int
    changed: bool


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported_json_type:{type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _principal_digest(principal_id: str) -> str:
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16]


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _raw_nesting_depth(preferences: dict[str, object], *, limit: int = 10_000) -> int:
    depth = 0
    current: object = preferences
    seen: set[int] = set()
    while isinstance(current, dict) and isinstance(current.get("raw_preferences"), dict):
        object_id = id(current)
        if object_id in seen:
            raise ValueError("raw_preferences_object_cycle")
        seen.add(object_id)
        depth += 1
        if depth > limit:
            raise ValueError("raw_preferences_depth_limit_exceeded")
        current = current["raw_preferences"]
    return depth


def _flatten_raw_preferences_chain(
    value: dict[str, object] | None,
    *,
    max_depth: int = RAW_PREFERENCES_MAX_DEPTH,
) -> dict[str, object]:
    outer = dict(value or {})
    nested: object = outer.pop("raw_preferences", None)
    layers: list[dict[str, object]] = [outer]
    seen: set[int] = set()
    bounded_depth = max(0, int(max_depth or 0))
    depth = 0
    while isinstance(nested, dict) and depth < bounded_depth:
        object_id = id(nested)
        if object_id in seen:
            raise ValueError("raw_preferences_object_cycle")
        seen.add(object_id)
        layer = dict(nested)
        nested = layer.pop("raw_preferences", None)
        layers.append(layer)
        depth += 1
    if isinstance(nested, dict):
        raise ValueError("raw_preferences_depth_limit_exceeded")
    flattened: dict[str, object] = {}
    for layer in reversed(layers):
        flattened.update(layer)
    flattened.pop("raw_preferences", None)
    return flattened


def build_compaction_plan(preferences: dict[str, object]) -> CompactionPlan:
    """Build and verify a loss-bounded compaction plan in memory.

    The current top-level preferences are canonical.  They are preserved
    byte-for-byte at the value level, including the current shortlist and
    agents.  Every bounded legacy raw layer contributes unpromoted user fields
    with newer outer values taking precedence, while structural and heavy
    collection copies are removed.
    """

    root = dict(preferences or {})
    immediate_raw_value = root.get("raw_preferences")
    immediate_raw = dict(immediate_raw_value) if isinstance(immediate_raw_value, dict) else {}
    flattened_raw = _flatten_raw_preferences_chain(immediate_raw)
    compact_raw = {
        str(key): value
        for key, value in flattened_raw.items()
        if str(key) not in RAW_RESERVED_KEYS
    }
    compacted = dict(root)
    compacted["raw_preferences"] = compact_raw

    root_without_raw = dict(root)
    root_without_raw.pop("raw_preferences", None)
    compacted_without_raw = dict(compacted)
    compacted_without_raw.pop("raw_preferences", None)
    if root_without_raw != compacted_without_raw:
        raise RuntimeError("top_level_preferences_changed")
    if compacted.get("saved_shortlist_candidates") != root.get("saved_shortlist_candidates"):
        raise RuntimeError("saved_shortlist_changed")
    if compacted.get("search_agents") != root.get("search_agents"):
        raise RuntimeError("search_agents_changed")
    if compacted.get("raw_preferences") != compact_raw:
        raise RuntimeError("flattened_raw_preferences_changed")
    if any(key in compact_raw for key in RAW_RESERVED_KEYS):
        raise RuntimeError("reserved_raw_preferences_key_retained")

    before_bytes = _canonical_json_bytes(root)
    after_bytes = _canonical_json_bytes(compacted)
    raw_only_keys = set(flattened_raw).difference(root).difference(RAW_RESERVED_KEYS)
    return CompactionPlan(
        compacted=compacted,
        before_sha256=_sha256_bytes(before_bytes),
        after_sha256=_sha256_bytes(after_bytes),
        before_json_bytes=len(before_bytes),
        after_json_bytes=len(after_bytes),
        raw_nesting_depth=_raw_nesting_depth(root),
        top_saved_shortlist_count=_list_count(root.get("saved_shortlist_candidates")),
        top_search_agent_count=_list_count(root.get("search_agents")),
        raw_only_key_count=len(raw_only_keys),
        changed=before_bytes != after_bytes,
    )


def _backup_payload(row: dict[str, object], *, principal_digest: str) -> dict[str, object]:
    return {
        "schema": BACKUP_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "principal_sha256_prefix": principal_digest,
        "row": {key: row.get(key) for key in ONBOARDING_COLUMNS},
    }


def write_backup_file(path: Path, payload: dict[str, object]) -> tuple[str, int]:
    """Atomically write an exclusive mode-0600 backup and return hash/size."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("backup_path_exists")
    encoded = _canonical_json_bytes(payload) + b"\n"
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError("backup_path_exists") from exc
        temporary.unlink()
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return _sha256_bytes(encoded), len(encoded)


def _fetch_onboarding_row(cursor: Any, *, principal_id: str, lock: bool) -> dict[str, object] | None:
    lock_sql = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"SELECT {', '.join(ONBOARDING_COLUMNS)}, "
        "pg_column_size(property_search_preferences_json) "
        "FROM onboarding_states WHERE principal_id = %s" + lock_sql,
        (principal_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    result = {column: row[index] for index, column in enumerate(ONBOARDING_COLUMNS)}
    result["stored_preferences_bytes"] = int(row[len(ONBOARDING_COLUMNS)] or 0)
    return result


def _search_run_storage_snapshot(cursor: Any, *, principal_id: str) -> dict[str, int]:
    cursor.execute(
        "SELECT count(*), "
        "COALESCE(sum(pg_column_size(payload_json)), 0), "
        "COALESCE(sum(pg_column_size(compact_json)), 0), "
        "count(*) FILTER (WHERE COALESCE(payload_json->>'payload_retention_status', '') <> 'compact_only') "
        "FROM property_search_runs WHERE principal_id = %s",
        (principal_id,),
    )
    row = cursor.fetchone() or (0, 0, 0, 0)
    return {
        "row_count": int(row[0] or 0),
        "payload_stored_bytes": int(row[1] or 0),
        "compact_stored_bytes": int(row[2] or 0),
        "full_payload_row_count": int(row[3] or 0),
    }


def _public_report(
    *,
    plan: CompactionPlan,
    principal_digest: str,
    stored_before_bytes: int,
    stored_after_bytes: int | None,
    mode: str,
    search_runs: dict[str, int],
    backup_sha256: str = "",
    backup_bytes: int = 0,
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "applied" if mode == "apply" else "dry_run",
        "mode": mode,
        "principal_sha256_prefix": principal_digest,
        "changed": plan.changed,
        "raw_nesting_depth_before": plan.raw_nesting_depth,
        "raw_nesting_depth_after": 1,
        "top_saved_shortlist_count_preserved": plan.top_saved_shortlist_count,
        "top_search_agent_count_preserved": plan.top_search_agent_count,
        "raw_only_key_count_preserved": plan.raw_only_key_count,
        "canonical_sha256_before": plan.before_sha256,
        "canonical_sha256_after": plan.after_sha256,
        "logical_json_bytes_before": plan.before_json_bytes,
        "logical_json_bytes_after": plan.after_json_bytes,
        "stored_preferences_bytes_before": stored_before_bytes,
        "search_runs": search_runs,
        "search_run_retention_action": "diagnostic_only",
        "search_run_retention_note": (
            "Full-run pruning is intentionally not performed: compact projections can truncate "
            "historical candidates, so candidate-link preservation must be indexed first."
        ),
    }
    if stored_after_bytes is not None:
        report["stored_preferences_bytes_after"] = stored_after_bytes
        report["vacuum_analyze_recommended"] = bool(plan.changed)
    if backup_sha256:
        report["backup_sha256"] = backup_sha256
        report["backup_bytes"] = backup_bytes
        report["backup_permissions"] = "0600"
    return report


def run_compaction(
    connection: Any,
    *,
    principal_id: str,
    apply: bool,
    backup_path: Path | None,
) -> dict[str, object]:
    if not principal_id.strip():
        raise ValueError("principal_id_required")
    if apply and backup_path is None:
        raise ValueError("backup_path_required_for_apply")

    with connection.cursor() as cursor:
        row = _fetch_onboarding_row(cursor, principal_id=principal_id, lock=apply)
        if row is None:
            raise LookupError("onboarding_state_not_found")
        preferences = row.get("property_search_preferences_json")
        if not isinstance(preferences, dict):
            raise TypeError("property_search_preferences_json_not_object")
        plan = build_compaction_plan(preferences)
        search_runs = _search_run_storage_snapshot(cursor, principal_id=principal_id)
        principal_digest = _principal_digest(principal_id)
        if not apply:
            return _public_report(
                plan=plan,
                principal_digest=principal_digest,
                stored_before_bytes=int(row.get("stored_preferences_bytes") or 0),
                stored_after_bytes=None,
                mode="dry_run",
                search_runs=search_runs,
            )

        assert backup_path is not None
        backup_sha256, backup_bytes = write_backup_file(
            backup_path,
            _backup_payload(row, principal_digest=principal_digest),
        )
        cursor.execute(
            "UPDATE onboarding_states "
            "SET property_search_preferences_json = %s::jsonb "
            "WHERE principal_id = %s "
            "RETURNING property_search_preferences_json, "
            "pg_column_size(property_search_preferences_json)",
            (_canonical_json_bytes(plan.compacted).decode("utf-8"), principal_id),
        )
        updated = cursor.fetchone()
        if updated is None or not isinstance(updated[0], dict):
            raise RuntimeError("onboarding_compaction_update_failed")
        verified = build_compaction_plan(dict(updated[0]))
        if verified.before_sha256 != plan.after_sha256:
            raise RuntimeError("onboarding_compaction_hash_verification_failed")
        if verified.raw_nesting_depth != 1:
            raise RuntimeError("onboarding_compaction_depth_verification_failed")
        stored_after_bytes = int(updated[1] or 0)
    connection.commit()
    return _public_report(
        plan=plan,
        principal_digest=principal_digest,
        stored_before_bytes=int(row.get("stored_preferences_bytes") or 0),
        stored_after_bytes=stored_after_bytes,
        mode="apply",
        search_runs=search_runs,
        backup_sha256=backup_sha256,
        backup_bytes=backup_bytes,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-env",
        default=DEFAULT_DATABASE_ENV,
        help="Environment variable containing the PostgreSQL URL (default: DATABASE_URL).",
    )
    parser.add_argument(
        "--principal-env",
        default=DEFAULT_PRINCIPAL_ENV,
        help=(
            "Environment variable containing the principal id "
            "(default: PROPERTYQUARRY_LIVE_PRINCIPAL_ID)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the compaction. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        help="Required with --apply; must not already exist.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.backup_path is None:
        parser.error("--backup-path is required with --apply")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = str(os.environ.get(str(args.database_env)) or "").strip()
    principal_id = str(os.environ.get(str(args.principal_env)) or "").strip()
    if not database_url:
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "error", "reason": "database_url_missing"}))
        return 2
    if not principal_id:
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "error", "reason": "principal_id_missing"}))
        return 2
    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            report = run_compaction(
                connection,
                principal_id=principal_id,
                apply=bool(args.apply),
                backup_path=args.backup_path,
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": "error",
                    "reason": str(exc) if str(exc) in {
                        "backup_path_exists",
                        "backup_path_required_for_apply",
                        "onboarding_state_not_found",
                        "property_search_preferences_json_not_object",
                        "onboarding_compaction_update_failed",
                        "onboarding_compaction_hash_verification_failed",
                        "onboarding_compaction_depth_verification_failed",
                    } else "compaction_failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
