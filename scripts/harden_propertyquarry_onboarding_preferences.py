#!/usr/bin/env python3
"""Safely compact legacy nested PropertyQuarry onboarding preferences.

This is deliberately a *production operator* tool, rather than an application
migration.  It is dry-run unless ``--apply`` is supplied and it never writes a
principal identifier, database URL, or preference value to stdout/stderr.

Only the legacy copies in a ``raw_preferences`` chain are deduplicated.  The
outer (current) preference object is otherwise retained exactly.  Every apply
is optimistic (the original jsonb value is part of the UPDATE predicate), is
locked, and creates a private recovery record before changing the row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BACKUP_SCHEMA = "propertyquarry.onboarding-preferences-compactor.backup.v1"
RECEIPT_SCHEMA = "propertyquarry.onboarding-preferences-compactor.receipt.v1"
RAW_PREFERENCES_MAX_DEPTH = 512
RESERVED_RAW_STRUCTURAL_KEYS = frozenset(
    {"raw_preferences", "saved_shortlist_candidates", "search_agents"}
)
DEFAULT_MINIMUM_BYTES = 131_072
DEFAULT_DATABASE_ENV = "PROPERTYQUARRY_COMPACTION_DATABASE_URL"
TIMEOUT_SQL = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '30s'",
    "SET LOCAL idle_in_transaction_session_timeout = '30s'",
)


class CompactorError(RuntimeError):
    """An intentionally non-sensitive operator-facing failure."""


class PartialApplyError(CompactorError):
    """A rerunnable per-row operation stopped after a redacted partial receipt."""

    def __init__(self, reason: str, receipt: Mapping[str, object]) -> None:
        super().__init__(reason)
        self.receipt = dict(receipt)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def principal_digest(principal_id: object) -> str:
    return _sha256(str(principal_id or "").encode("utf-8"))


def _is_digest(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def raw_preferences_depth(preferences: Mapping[str, object]) -> int:
    """Return structural raw depth, rejecting malformed/cyclic over-depth data."""
    current: object = preferences.get("raw_preferences")
    seen: set[int] = set()
    depth = 0
    while isinstance(current, dict):
        marker = id(current)
        if marker in seen:
            raise CompactorError("raw_preferences_cycle")
        seen.add(marker)
        depth += 1
        if depth > RAW_PREFERENCES_MAX_DEPTH:
            raise CompactorError("raw_preferences_depth_limit_exceeded")
        current = current.get("raw_preferences")
    return depth


def compact_preferences(preferences: Mapping[str, object]) -> dict[str, object]:
    """Flatten only legacy raw structural copies, preserving outer semantics."""
    root = dict(preferences)
    depth = raw_preferences_depth(root)
    if depth < 2:
        return root

    layers: list[dict[str, object]] = []
    current: object = root.get("raw_preferences")
    for _ in range(depth):
        assert isinstance(current, dict)  # established by raw_preferences_depth
        layer = dict(current)
        current = layer.pop("raw_preferences", None)
        layers.append(layer)

    # Merge deepest first.  A newer raw snapshot wins over its predecessor.
    compact_raw: dict[str, object] = {}
    for layer in reversed(layers):
        for key, value in layer.items():
            if key not in RESERVED_RAW_STRUCTURAL_KEYS:
                compact_raw[key] = value
    compacted = dict(root)
    compacted["raw_preferences"] = compact_raw

    # These checks make the narrow write set explicit, rather than implicit in
    # a transform implementation.
    original_outer = dict(root)
    original_outer.pop("raw_preferences", None)
    result_outer = dict(compacted)
    result_outer.pop("raw_preferences", None)
    if result_outer != original_outer:
        raise CompactorError("top_level_semantic_value_changed")
    if raw_preferences_depth(compacted) != 1:
        raise CompactorError("compacted_raw_preferences_depth_invalid")
    return compacted


@dataclass(frozen=True)
class Candidate:
    principal_id: str
    principal_sha256: str
    preferences: dict[str, object]
    stored_bytes: int
    full_row: dict[str, object] | None = None

    @property
    def before_sha256(self) -> str:
        return _sha256(_canonical_json(self.preferences))

    @property
    def compacted(self) -> dict[str, object]:
        return compact_preferences(self.preferences)

    @property
    def after_sha256(self) -> str:
        return _sha256(_canonical_json(self.compacted))

    @property
    def changed(self) -> bool:
        return self.before_sha256 != self.after_sha256


DISCOVERY_SQL = """
SELECT principal_id,
       pg_column_size(property_search_preferences_json)
FROM onboarding_states
WHERE jsonb_typeof(property_search_preferences_json) = 'object'
  AND jsonb_typeof(property_search_preferences_json -> 'raw_preferences') = 'object'
  AND jsonb_typeof(property_search_preferences_json -> 'raw_preferences' -> 'raw_preferences') = 'object'
  AND pg_column_size(property_search_preferences_json) >= %s
ORDER BY principal_id
"""
SINGLE_ROW_SQL = """
SELECT principal_id,
       property_search_preferences_json,
       pg_column_size(property_search_preferences_json),
       to_jsonb(onboarding_states)
FROM onboarding_states
WHERE principal_id = %s
"""
LOCKED_ROW_SQL = """
SELECT principal_id,
       property_search_preferences_json,
       pg_column_size(property_search_preferences_json),
       to_jsonb(onboarding_states)
FROM onboarding_states
WHERE principal_id = %s
FOR UPDATE
"""


def _candidate_from_row(row: Sequence[object]) -> Candidate:
    if len(row) == 2:
        identifier = str(row[0] or "")
        if not identifier:
            raise CompactorError("onboarding_principal_missing")
        return Candidate(identifier, principal_digest(identifier), {}, int(row[1] or 0), None)
    if len(row) < 4 or not isinstance(row[1], dict) or not isinstance(row[3], dict):
        raise CompactorError("onboarding_row_invalid")
    identifier = str(row[0] or "")
    if not identifier:
        raise CompactorError("onboarding_principal_missing")
    return Candidate(
        principal_id=identifier,
        principal_sha256=principal_digest(identifier),
        preferences=dict(row[1]),
        stored_bytes=int(row[2] or 0),
        full_row=dict(row[3]),
    )


def discover_candidates(cursor: Any, *, minimum_bytes: int) -> list[Candidate]:
    if minimum_bytes < 1:
        raise CompactorError("minimum_bytes_invalid")
    cursor.execute(DISCOVERY_SQL, (minimum_bytes,))
    rows = cursor.fetchall() or []
    return [_candidate_from_row(row) for row in rows]


def _load_candidate(cursor: Any, principal_id: str, *, lock: bool) -> Candidate:
    cursor.execute(LOCKED_ROW_SQL if lock else SINGLE_ROW_SQL, (principal_id,))
    row = cursor.fetchone()
    if row is None:
        raise CompactorError("locked_candidate_missing" if lock else "candidate_missing")
    return _candidate_from_row(row)


def validate_scope(
    candidates: Iterable[Candidate], *, expected_count: int | None, expected_principal_digests: Iterable[str]
) -> tuple[Candidate, ...]:
    if expected_count is None or expected_count < 0:
        raise CompactorError("expected_count_required")
    expected = frozenset(str(item).strip() for item in expected_principal_digests)
    if not expected or any(not _is_digest(item) for item in expected):
        raise CompactorError("expected_principal_digest_set_required")
    materialized = tuple(candidates)
    actual = frozenset(candidate.principal_sha256 for candidate in materialized)
    if len(materialized) != expected_count:
        raise CompactorError("candidate_count_mismatch")
    if len(actual) != len(materialized) or actual != expected:
        raise CompactorError("candidate_digest_set_mismatch")
    return materialized


def _open_secure_backup_directory(backup_dir: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(backup_dir, flags)
    except OSError as error:
        raise CompactorError("backup_directory_missing") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise CompactorError("backup_directory_not_real_directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise CompactorError("backup_directory_mode_must_be_0700")
    return descriptor


def _assert_backup_directory(backup_dir: Path) -> None:
    descriptor = _open_secure_backup_directory(backup_dir)
    os.close(descriptor)


def _backup_without_hash(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.pop("backup_content_sha256", None)
    return result


def _backup_payload(candidate: Candidate) -> dict[str, object]:
    if candidate.full_row is None:
        raise CompactorError("full_row_recovery_record_missing")
    payload: dict[str, object] = {
        "schema": BACKUP_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "principal_sha256": candidate.principal_sha256,
        "before_preferences_sha256": candidate.before_sha256,
        "after_preferences_sha256": candidate.after_sha256,
        "row": candidate.full_row,
    }
    payload["backup_content_sha256"] = _sha256(_canonical_json(payload))
    return payload


def write_private_backup(backup_dir: Path, candidate: Candidate) -> tuple[Path, str, int]:
    """Create a non-overwritable, mode-0600 private recovery record."""
    payload = _backup_payload(candidate)
    raw = _canonical_json(payload)
    content_digest = str(payload["backup_content_sha256"])
    target_name = f"onboarding-preferences-{candidate.principal_sha256}-{candidate.before_sha256}.json"
    directory_fd = _open_secure_backup_directory(backup_dir)
    descriptor = -1
    temporary_name: str | None = None
    try:
        temporary_name = f".pending-{os.urandom(16).hex()}"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        # link(2) is exclusive: unlike replace it can never overwrite a prior
        # recovery record. Both files share the fully fsync'd inode.
        os.link(temporary_name, target_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        target_fd = os.open(target_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            target_mode = stat.S_IMODE(os.fstat(target_fd).st_mode)
        finally:
            os.close(target_fd)
    except FileExistsError as error:
        raise CompactorError("backup_target_already_exists") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    if target_mode != 0o600:
        raise CompactorError("backup_file_mode_invalid")
    return backup_dir / target_name, content_digest, len(raw)


def _candidate_is_eligible(candidate: Candidate, *, minimum_bytes: int) -> bool:
    try:
        return (
            candidate.stored_bytes >= minimum_bytes
            and raw_preferences_depth(candidate.preferences) >= 2
            and candidate.changed
        )
    except CompactorError:
        return False


def _candidate_summary(candidate: Candidate) -> dict[str, object]:
    return {
        "principal_sha256": candidate.principal_sha256,
        "stored_bytes": candidate.stored_bytes,
        "json_before_bytes": len(_canonical_json(candidate.preferences)),
        "json_after_bytes": len(_canonical_json(candidate.compacted)),
        "changed": candidate.changed,
    }


def _redacted_receipt(
    *,
    mode: str,
    summaries: Sequence[Mapping[str, object]],
    scope_principal_sha256: Sequence[str],
    backups: Sequence[tuple[str, str, int]] = (),
) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "mode": mode,
        "candidate_count": len(scope_principal_sha256),
        "candidate_principal_sha256": sorted(scope_principal_sha256),
        "processed_count": len(summaries),
        "before_bytes_total": sum(int(item["stored_bytes"]) for item in summaries),
        "json_before_bytes_total": sum(int(item["json_before_bytes"]) for item in summaries),
        "json_after_bytes_total": sum(int(item["json_after_bytes"]) for item in summaries),
        "changed_count": sum(1 for item in summaries if bool(item["changed"])),
        "backups": [
            {
                "principal_sha256": principal_sha256,
                "backup_content_sha256": digest,
                "backup_bytes": byte_count,
            }
            for principal_sha256, digest, byte_count in backups
        ],
    }


def dry_run(
    connection: Any, *, minimum_bytes: int, expected_count: int | None, expected_principal_digests: Iterable[str]
) -> dict[str, object]:
    with connection.cursor() as cursor:
        metadata = validate_scope(
            discover_candidates(cursor, minimum_bytes=minimum_bytes),
            expected_count=expected_count,
            expected_principal_digests=expected_principal_digests,
        )
        summaries: list[dict[str, object]] = []
        for candidate in metadata:
            loaded = _load_candidate(cursor, candidate.principal_id, lock=False)
            summaries.append(_candidate_summary(loaded))
            del loaded
    return _redacted_receipt(
        mode="dry_run",
        summaries=summaries,
        scope_principal_sha256=[item.principal_sha256 for item in metadata],
    )


def _set_timeouts(cursor: Any) -> None:
    for statement in TIMEOUT_SQL:
        cursor.execute(statement)


def _begin_explicit_transaction(connection: Any) -> object | None:
    original = getattr(connection, "autocommit", None)
    if original is not None:
        try:
            connection.autocommit = False
        except Exception as error:
            raise CompactorError("explicit_transaction_required") from error
    return original


def _finish_explicit_transaction(connection: Any, original_autocommit: object | None) -> None:
    if original_autocommit is not None:
        connection.autocommit = original_autocommit


def apply(
    connection: Any,
    *,
    backup_dir: Path,
    minimum_bytes: int,
    expected_count: int | None,
    expected_principal_digests: Iterable[str],
) -> dict[str, object]:
    _assert_backup_directory(backup_dir)
    # The initial snapshot is a mandatory scope gate. No locks/writes occur
    # before both count and full expected digest set match exactly.
    with connection.cursor() as cursor:
        first_scope = validate_scope(
            discover_candidates(cursor, minimum_bytes=minimum_bytes),
            expected_count=expected_count,
            expected_principal_digests=expected_principal_digests,
        )
        # Re-read immediately before beginning any per-row transaction.  The
        # operator's reviewed set must remain exact, not merely be a subset.
        candidates = validate_scope(
            discover_candidates(cursor, minimum_bytes=minimum_bytes),
            expected_count=expected_count,
            expected_principal_digests=expected_principal_digests,
        )
    if tuple(item.principal_sha256 for item in first_scope) != tuple(item.principal_sha256 for item in candidates):
        raise CompactorError("candidate_scope_changed_before_apply")
    backups: list[tuple[str, str, int]] = []
    summaries: list[dict[str, object]] = []
    for expected in candidates:
        original_autocommit = _begin_explicit_transaction(connection)
        try:
            with connection.cursor() as cursor:
                _set_timeouts(cursor)
                locked = _load_candidate(cursor, expected.principal_id, lock=True)
                if locked.principal_sha256 != expected.principal_sha256 or not _candidate_is_eligible(
                    locked, minimum_bytes=minimum_bytes
                ):
                    raise CompactorError("candidate_changed_before_lock")
                backup_path, backup_digest, backup_bytes = write_private_backup(backup_dir, locked)
                cursor.execute(
                    "UPDATE onboarding_states SET property_search_preferences_json = %s::jsonb "
                    "WHERE principal_id = %s AND property_search_preferences_json = %s::jsonb "
                    "RETURNING property_search_preferences_json, pg_column_size(property_search_preferences_json)",
                    (
                        _canonical_json(locked.compacted).decode("utf-8"),
                        locked.principal_id,
                        _canonical_json(locked.preferences).decode("utf-8"),
                    ),
                )
                updated = cursor.fetchone()
                if updated is None or not isinstance(updated[0], dict):
                    raise CompactorError("update_compare_and_swap_failed")
                verified = Candidate(
                    principal_id=locked.principal_id,
                    principal_sha256=locked.principal_sha256,
                    preferences=dict(updated[0]),
                    stored_bytes=int(updated[1] or 0),
                )
                if verified.before_sha256 != locked.after_sha256:
                    raise CompactorError("post_update_hash_mismatch")
                if raw_preferences_depth(verified.preferences) != 1:
                    raise CompactorError("post_update_depth_mismatch")
                if _list_count(verified.preferences.get("saved_shortlist_candidates")) != _list_count(
                    locked.preferences.get("saved_shortlist_candidates")
                ) or _list_count(verified.preferences.get("search_agents")) != _list_count(
                    locked.preferences.get("search_agents")
                ):
                    raise CompactorError("post_update_structural_count_mismatch")
            connection.commit()
            summaries.append(_candidate_summary(locked))
            backups.append((locked.principal_sha256, backup_digest, backup_bytes))
            del verified
            del locked
        except Exception as error:
            connection.rollback()
            if backups:
                raise PartialApplyError(
                    "partial_apply_stopped",
                    _redacted_receipt(
                        mode="partial_apply",
                        summaries=summaries,
                        scope_principal_sha256=[item.principal_sha256 for item in candidates],
                        backups=backups,
                    ),
                ) from error
            raise
        finally:
            _finish_explicit_transaction(connection, original_autocommit)
    return _redacted_receipt(
        mode="apply",
        summaries=summaries,
        scope_principal_sha256=[item.principal_sha256 for item in candidates],
        backups=backups,
    )


def _load_backup(path: Path, *, expected_file_sha256: str) -> dict[str, object]:
    if not _is_digest(expected_file_sha256):
        raise CompactorError("restore_backup_file_sha256_required")
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except OSError as error:
        raise CompactorError("backup_file_invalid") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CompactorError("backup_file_invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CompactorError("backup_file_mode_invalid")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1_048_576):
            chunks.append(block)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
        os.close(parent_fd)
    if _sha256(raw) != expected_file_sha256:
        raise CompactorError("restore_backup_file_sha256_mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompactorError("backup_json_invalid") from error
    if not isinstance(payload, dict) or _canonical_json(payload) != raw:
        raise CompactorError("backup_canonical_encoding_invalid")
    if payload.get("schema") != BACKUP_SCHEMA or not isinstance(payload.get("row"), dict):
        raise CompactorError("backup_schema_invalid")
    recorded = str(payload.get("backup_content_sha256") or "")
    if not _is_digest(recorded) or recorded != _sha256(_canonical_json(_backup_without_hash(payload))):
        raise CompactorError("backup_file_hash_invalid")
    row = dict(payload["row"])
    identifier = str(row.get("principal_id") or "")
    if not identifier or principal_digest(identifier) != payload.get("principal_sha256"):
        raise CompactorError("backup_principal_digest_invalid")
    preferences = row.get("property_search_preferences_json")
    if not isinstance(preferences, dict) or _sha256(_canonical_json(preferences)) != payload.get("before_preferences_sha256"):
        raise CompactorError("backup_preferences_hash_invalid")
    return payload


def restore(connection: Any, *, backup_path: Path, expected_file_sha256: str) -> dict[str, object]:
    payload = _load_backup(backup_path, expected_file_sha256=expected_file_sha256)
    row = dict(payload["row"])
    original = dict(row["property_search_preferences_json"])
    identifier = str(row["principal_id"])
    expected_after = str(payload["after_preferences_sha256"])
    original_autocommit = _begin_explicit_transaction(connection)
    try:
        with connection.cursor() as cursor:
            _set_timeouts(cursor)
            locked = _load_candidate(cursor, identifier, lock=True)
            # A restore is only allowed when the current value is exactly the
            # compactor output. A later human/application edit is never erased.
            if locked.principal_sha256 != payload["principal_sha256"] or locked.before_sha256 != expected_after:
                raise CompactorError("restore_compare_and_swap_mismatch")
            cursor.execute(
                "UPDATE onboarding_states SET property_search_preferences_json = %s::jsonb "
                "WHERE principal_id = %s AND property_search_preferences_json = %s::jsonb "
                "RETURNING property_search_preferences_json",
                (
                    _canonical_json(original).decode("utf-8"),
                    identifier,
                    _canonical_json(locked.preferences).decode("utf-8"),
                ),
            )
            restored = cursor.fetchone()
            if restored is None or not isinstance(restored[0], dict) or _canonical_json(restored[0]) != _canonical_json(original):
                raise CompactorError("restore_post_update_hash_mismatch")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        _finish_explicit_transaction(connection, original_autocommit)
    return {
        "schema": RECEIPT_SCHEMA,
        "mode": "restore",
        "candidate_count": 1,
        "candidate_principal_sha256": [str(payload["principal_sha256"])],
        "backup_content_sha256": str(payload["backup_content_sha256"]),
    }


def _connect(database_url: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as error:
        raise CompactorError("psycopg_unavailable") from error
    return psycopg.connect(database_url, autocommit=False)


def _database_url_from_env(environment_name: object) -> str:
    """Read the connection secret only from a deliberately named environment key."""
    name = str(environment_name or "").strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
        raise CompactorError("database_environment_name_invalid")
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise CompactorError("database_environment_missing")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run-safe PropertyQuarry onboarding preference compactor")
    parser.add_argument(
        "--database-env",
        default=DEFAULT_DATABASE_ENV,
        metavar="ENV_NAME",
        help="Environment variable containing the database connection URL (default: %(default)s)",
    )
    parser.add_argument("--minimum-bytes", type=int, default=DEFAULT_MINIMUM_BYTES)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--principal-digest", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--restore-backup", type=Path)
    parser.add_argument("--restore-backup-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database_url = _database_url_from_env(args.database_env)
        if args.restore_backup and args.apply:
            raise CompactorError("restore_and_apply_are_mutually_exclusive")
        if args.restore_backup:
            if not args.restore_backup_sha256:
                raise CompactorError("restore_backup_file_sha256_required")
            connection = _connect(database_url)
            try:
                receipt = restore(
                    connection,
                    backup_path=args.restore_backup,
                    expected_file_sha256=args.restore_backup_sha256,
                )
            finally:
                connection.close()
        else:
            connection = _connect(database_url)
            try:
                if args.apply:
                    if args.backup_dir is None:
                        raise CompactorError("backup_directory_required_for_apply")
                    receipt = apply(
                        connection,
                        backup_dir=args.backup_dir,
                        minimum_bytes=args.minimum_bytes,
                        expected_count=args.expected_count,
                        expected_principal_digests=args.principal_digest,
                    )
                else:
                    receipt = dry_run(
                        connection,
                        minimum_bytes=args.minimum_bytes,
                        expected_count=args.expected_count,
                        expected_principal_digests=args.principal_digest,
                    )
            finally:
                connection.close()
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except PartialApplyError as error:
        receipt = dict(error.receipt)
        receipt.update({"status": "partial", "reason": str(error)})
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2
    except CompactorError as error:
        print(json.dumps({"schema": RECEIPT_SCHEMA, "status": "error", "reason": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as error:
        print(
            json.dumps(
                {"schema": RECEIPT_SCHEMA, "status": "error", "reason": "unexpected_runtime_failure", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
