#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.product.property_research_packet_links import (  # noqa: E402
    PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES,
    PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
    PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    project_property_research_packet_links,
    sync_property_research_packet_run_memberships,
    upsert_property_research_packet_links,
)
from app.product.property_search_schema import (  # noqa: E402
    LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
)
from app.product.property_research_packet_fleet_proof import (  # noqa: E402
    PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT,
    property_research_packet_fleet_proof_sha256,
    validate_property_research_packet_fleet_proof,
)


BACKFILL_RECEIPT_CONTRACT = "property_research_packet_link_coverage_v2"
BACKFILL_CHECKPOINT_CONTRACT = "property_research_packet_link_checkpoint_v1"
FLEET_PROOF_CONTRACT = PROPERTY_RESEARCH_PACKET_FLEET_PROOF_CONTRACT
DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 100
DEFAULT_MAX_BATCH_BYTES = 32 * 1024 * 1024
MIN_MAX_BATCH_BYTES = PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES
MAX_MAX_BATCH_BYTES = 64 * 1024 * 1024
BACKFILL_ADVISORY_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"propertyquarry:research-packet-backfill:v2").digest()[:8],
    byteorder="big",
    signed=True,
)

_BATCH_SELECT_BASE = """
    SELECT principal_id, run_id, payload_json, created_at, updated_at
    FROM property_search_runs
    WHERE updated_at <= %s::timestamptz
      AND (principal_id, run_id) > (%s, %s)
    ORDER BY principal_id ASC, run_id ASC
    LIMIT %s
"""
BACKFILL_AUDIT_BATCH_SQL = _BATCH_SELECT_BASE + " FOR SHARE"
BACKFILL_APPLY_BATCH_SQL = _BATCH_SELECT_BASE + " FOR UPDATE"
BACKFILL_SOURCE_COUNT_SQL = """
    SELECT COUNT(*)
    FROM property_search_runs
    WHERE updated_at <= %s::timestamptz
"""
BACKFILL_VERIFICATION_COUNT_SQL = """
    SELECT COUNT(*),
           COUNT(DISTINCT (memberships.principal_id, memberships.candidate_ref))
    FROM property_research_packet_run_memberships AS memberships
    JOIN property_search_runs AS runs
      ON runs.principal_id = memberships.principal_id
     AND runs.run_id = memberships.run_id
    WHERE runs.updated_at <= %s::timestamptz
"""
BACKFILL_VERIFICATION_IDENTITIES_SQL = """
    SELECT memberships.principal_id,
           memberships.run_id,
           memberships.candidate_ref,
           memberships.packet_sha256,
           memberships.packet_size_bytes
    FROM property_research_packet_run_memberships AS memberships
    JOIN property_search_runs AS runs
      ON runs.principal_id = memberships.principal_id
     AND runs.run_id = memberships.run_id
    WHERE runs.updated_at <= %s::timestamptz
    ORDER BY memberships.principal_id ASC,
             memberships.run_id ASC,
             memberships.candidate_ref ASC
"""
BACKFILL_LINK_COVERAGE_SQL = """
    SELECT
        (
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT memberships.principal_id, memberships.candidate_ref
                FROM property_research_packet_run_memberships AS memberships
                JOIN property_search_runs AS runs
                  ON runs.principal_id = memberships.principal_id
                 AND runs.run_id = memberships.run_id
                WHERE runs.updated_at <= %s::timestamptz
            ) AS expected_refs
            WHERE NOT EXISTS (
                SELECT 1
                FROM property_research_packet_links AS links
                WHERE links.principal_id = expected_refs.principal_id
                  AND links.candidate_ref = expected_refs.candidate_ref
            )
        ) AS memberships_without_links,
        (
            SELECT COUNT(*)
            FROM property_research_packet_links AS links
            WHERE links.retention_state <> 'legal_hold'
              AND NOT EXISTS (
                  SELECT 1
                  FROM property_research_packet_run_memberships AS memberships
                  WHERE memberships.principal_id = links.principal_id
                    AND memberships.candidate_ref = links.candidate_ref
              )
        ) AS non_hold_links_without_memberships
"""

_EMPTY_STREAM_DIGEST = hashlib.sha256(b"").hexdigest()
_CHECKPOINT_KEYS = frozenset(
    {
        "contract",
        "mode",
        "cutoff_at",
        "boundary_principal_id",
        "boundary_run_id",
        "expected_ref_digest_sha256",
        "receipt_counters",
        "updated_at",
    }
)
_CHECKPOINT_COUNTER_KEYS = frozenset(
    {
        "batches_completed",
        "run_rows_scanned",
        "compact_only_run_rows",
        "zero_projection_run_rows",
        "expected_membership_rows",
        "links_upserted",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _bounded_batch_size(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch_size_invalid") from exc
    if parsed < 1 or parsed > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size_out_of_range:1..{MAX_BATCH_SIZE}")
    return parsed


def _bounded_batch_bytes(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_batch_bytes_invalid") from exc
    if parsed < MIN_MAX_BATCH_BYTES or parsed > MAX_MAX_BATCH_BYTES:
        raise ValueError(
            f"max_batch_bytes_out_of_range:{MIN_MAX_BATCH_BYTES}..{MAX_MAX_BATCH_BYTES}"
        )
    return parsed


def _identity_digest(*values: str) -> str:
    if not any(values):
        return ""
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _stream_identity_digest(current_digest: str, *values: object) -> str:
    if (
        not isinstance(current_digest, str)
        or len(current_digest) != 64
        or any(character not in "0123456789abcdef" for character in current_digest)
    ):
        raise ValueError("membership_identity_digest_invalid")
    identity = "\0".join(str(value) for value in values).encode("utf-8")
    return hashlib.sha256(bytes.fromhex(current_digest) + b"\0" + identity).hexdigest()


def _membership_identity_values(
    *,
    principal_id: object,
    run_id: object,
    candidate_ref: object,
    packet_sha256: object,
    packet_size_bytes: object,
) -> tuple[object, ...]:
    return (
        str(principal_id or "").strip(),
        str(run_id or "").strip(),
        str(candidate_ref or "").strip(),
        str(packet_sha256 or "").strip(),
        int(packet_size_bytes or 0),
    )


def _stream_verification_identity_digest(
    connection: Any,
    *,
    cutoff_at: str,
) -> tuple[str, int]:
    digest = _EMPTY_STREAM_DIGEST
    count = 0
    with connection.transaction():
        # A named psycopg cursor is server-side: neither Python nor libpq holds
        # the complete identity set while the digest is built.
        with connection.cursor(name="packet_backfill_identity_verification") as cursor:
            cursor.execute(BACKFILL_VERIFICATION_IDENTITIES_SQL, (cutoff_at,))
            while True:
                rows = list(cursor.fetchmany(1000) or [])
                if not rows:
                    break
                for row in rows:
                    digest = _stream_identity_digest(
                        digest,
                        *_membership_identity_values(
                            principal_id=row[0],
                            run_id=row[1],
                            candidate_ref=row[2],
                            packet_sha256=row[3],
                            packet_size_bytes=row[4],
                        ),
                    )
                    count += 1
    return digest, count


def _run_payload(row: tuple[object, ...]) -> dict[str, object]:
    principal_id, run_id, raw_payload, created_at, updated_at = row
    payload = dict(raw_payload or {}) if isinstance(raw_payload, dict) else {}
    payload["principal_id"] = str(principal_id or "").strip()
    payload["run_id"] = str(run_id or "").strip()
    if not str(payload.get("created_at") or "").strip():
        payload["created_at"] = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or "")
        )
    if not str(payload.get("updated_at") or "").strip():
        payload["updated_at"] = (
            updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "")
        )
    return payload


def _validate_fleet_proof(proof: Mapping[str, object] | None) -> str:
    payload = validate_property_research_packet_fleet_proof(
        proof,
        property_search_schema_version=LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
        writer_contract_version=PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        packet_schema_version=PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
    )
    return property_research_packet_fleet_proof_sha256(payload)


def _new_receipt(
    *,
    apply: bool,
    batch_size: int,
    max_batches: int,
    max_batch_bytes: int,
) -> dict[str, object]:
    return {
        "contract": BACKFILL_RECEIPT_CONTRACT,
        "mode": "apply" if apply else "dry_run",
        "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        "packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        "batch_size": batch_size,
        "max_batch_bytes": max_batch_bytes,
        "max_batches": max_batches,
        "batches_completed": 0,
        "run_rows_scanned": 0,
        "source_run_rows_at_cutoff": 0,
        "source_run_rows_verified": 0,
        "compact_only_run_rows": 0,
        "zero_projection_run_rows": 0,
        "expected_membership_rows": 0,
        "expected_distinct_tenant_refs": 0,
        "verified_membership_rows": 0,
        "verified_distinct_tenant_refs": 0,
        "expected_ref_digest_sha256": _EMPTY_STREAM_DIGEST,
        "verified_ref_digest_sha256": "",
        "ref_digest_set_verified": False,
        "memberships_without_links": 0,
        "non_hold_links_without_memberships": 0,
        "link_coverage_verified": False,
        "links_upserted": 0,
        "projection_failures": 0,
        "verification_failures": 0,
        "failures_total": 0,
        "idempotent_verified": False,
        "scan_complete": False,
        "coverage_complete": False,
        "resume_token": {},
        "fleet_proof_sha256": "",
        "started_at": _utc_now(),
        "completed_at": "",
        "status": "running",
        "error_code": "",
    }


def _checkpoint_payload(
    *,
    cutoff_at: str,
    boundary: tuple[str, str],
    receipt: Mapping[str, object],
    expected_ref_digest_sha256: str,
) -> dict[str, object]:
    return {
        "contract": BACKFILL_CHECKPOINT_CONTRACT,
        "mode": str(receipt.get("mode") or ""),
        "cutoff_at": cutoff_at,
        "boundary_principal_id": boundary[0],
        "boundary_run_id": boundary[1],
        "expected_ref_digest_sha256": expected_ref_digest_sha256,
        "receipt_counters": {
            key: receipt.get(key)
            for key in (
                "batches_completed",
                "run_rows_scanned",
                "compact_only_run_rows",
                "zero_projection_run_rows",
                "expected_membership_rows",
                "links_upserted",
            )
        },
        "updated_at": _utc_now(),
    }


def _public_resume_token(cutoff_at: str, boundary: tuple[str, str]) -> dict[str, object]:
    return {
        "cutoff_at": cutoff_at,
        "keyset_digest": _identity_digest(*boundary)[:24],
        "checkpoint_required": True,
    }


def _load_private_json(path: Path) -> dict[str, object]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("private_file_permissions_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("private_file_not_object")
    return dict(payload)


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore_checkpoint(
    receipt: dict[str, object],
    checkpoint: Mapping[str, object],
) -> tuple[str, tuple[str, str], str]:
    payload = dict(checkpoint)
    if frozenset(payload) != _CHECKPOINT_KEYS:
        raise ValueError("checkpoint_schema_invalid")
    if payload.get("contract") != BACKFILL_CHECKPOINT_CONTRACT:
        raise ValueError("checkpoint_contract_invalid")
    if str(payload.get("mode") or "") != str(receipt.get("mode") or ""):
        raise ValueError("checkpoint_mode_mismatch")
    cutoff_at = str(payload.get("cutoff_at") or "").strip()
    if _parse_timestamp(cutoff_at) is None:
        raise ValueError("checkpoint_cutoff_invalid")
    boundary = (
        str(payload.get("boundary_principal_id") or ""),
        str(payload.get("boundary_run_id") or ""),
    )
    counters = payload.get("receipt_counters")
    if not isinstance(counters, dict) or frozenset(counters) != _CHECKPOINT_COUNTER_KEYS:
        raise ValueError("checkpoint_counters_invalid")
    for key in (
        "batches_completed",
        "run_rows_scanned",
        "compact_only_run_rows",
        "zero_projection_run_rows",
        "expected_membership_rows",
        "links_upserted",
    ):
        receipt[key] = max(0, int(counters.get(key) or 0))
    digest = payload.get("expected_ref_digest_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("checkpoint_ref_digest_invalid")
    receipt["expected_ref_digest_sha256"] = digest
    return cutoff_at, boundary, digest


def _select_batch(
    cursor: Any,
    *,
    cutoff_at: str,
    boundary: tuple[str, str],
    batch_size: int,
    apply: bool,
) -> list[tuple[object, ...]]:
    cursor.execute(
        BACKFILL_APPLY_BATCH_SQL if apply else BACKFILL_AUDIT_BATCH_SQL,
        (cutoff_at, boundary[0], boundary[1], batch_size),
    )
    return list(cursor.fetchall() or [])


def _verify_run_memberships(
    cursor: Any,
    *,
    principal_id: str,
    run_id: str,
    links: tuple[dict[str, object], ...],
) -> bool:
    cursor.execute(
        """
        SELECT candidate_ref, packet_sha256, packet_size_bytes
        FROM property_research_packet_run_memberships
        WHERE principal_id = %s AND run_id = %s
        ORDER BY candidate_ref ASC
        """,
        (principal_id, run_id),
    )
    actual = {
        str(row[0] or "").strip(): (str(row[1] or "").strip(), int(row[2] or 0))
        for row in list(cursor.fetchall() or [])
    }
    expected = {
        str(link["candidate_ref"]): (
            str(link["packet_sha256"]),
            int(link["packet_size_bytes"]),
        )
        for link in links
    }
    return actual == expected


def _mark_index_state(
    cursor: Any,
    *,
    status: str,
    receipt: Mapping[str, object],
    cutoff_at: str,
    fleet_proof_sha256: str,
) -> None:
    cursor.execute(
        """
        UPDATE property_research_packet_index_state
        SET coverage_status = %s,
            writer_contract_version = %s,
            packet_schema_version = %s,
            cutoff_at = %s::timestamptz,
            source_run_rows = %s,
            expected_membership_rows = %s,
            expected_distinct_tenant_refs = %s,
            verified_membership_rows = %s,
            verified_distinct_tenant_refs = %s,
            zero_projection_run_rows = %s,
            fleet_proof_sha256 = %s,
            completed_at = CASE WHEN %s = 'complete' THEN NOW() ELSE NULL END,
            updated_at = NOW()
        WHERE singleton = TRUE
        """,
        (
            status,
            PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
            PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
            cutoff_at,
            int(receipt.get("source_run_rows_at_cutoff") or 0),
            int(receipt.get("expected_membership_rows") or 0),
            int(receipt.get("expected_distinct_tenant_refs") or 0),
            int(receipt.get("verified_membership_rows") or 0),
            int(receipt.get("verified_distinct_tenant_refs") or 0),
            int(receipt.get("zero_projection_run_rows") or 0),
            fleet_proof_sha256 or None,
            status,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise RuntimeError("packet_index_state_row_missing")


def run_backfill(
    connection: Any,
    *,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 0,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    checkpoint: Mapping[str, object] | None = None,
    checkpoint_path: Path | None = None,
    fleet_proof: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized_batch_size = _bounded_batch_size(batch_size)
    normalized_max_batches = max(0, int(max_batches or 0))
    normalized_max_batch_bytes = _bounded_batch_bytes(max_batch_bytes)
    receipt = _new_receipt(
        apply=apply,
        batch_size=normalized_batch_size,
        max_batches=normalized_max_batches,
        max_batch_bytes=normalized_max_batch_bytes,
    )
    cutoff_at = str(receipt["started_at"])
    boundary = ("", "")
    expected_ref_digest_sha256 = _EMPTY_STREAM_DIGEST
    lock_acquired = False
    fleet_proof_sha256 = ""
    try:
        if checkpoint is not None:
            cutoff_at, boundary, expected_ref_digest_sha256 = _restore_checkpoint(
                receipt, checkpoint
            )
        if apply:
            fleet_proof_sha256 = _validate_fleet_proof(fleet_proof)
            receipt["fleet_proof_sha256"] = fleet_proof_sha256
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (BACKFILL_ADVISORY_LOCK_ID,))
            lock_row = cursor.fetchone()
            lock_acquired = bool(lock_row and lock_row[0])
            if not lock_acquired:
                raise RuntimeError("packet_backfill_already_running")
            cursor.execute(BACKFILL_SOURCE_COUNT_SQL, (cutoff_at,))
            count_row = cursor.fetchone()
            receipt["source_run_rows_at_cutoff"] = max(0, int(count_row[0] if count_row else 0))
            if apply:
                _mark_index_state(
                    cursor,
                    status="running",
                    receipt=receipt,
                    cutoff_at=cutoff_at,
                    fleet_proof_sha256=fleet_proof_sha256,
                )

        invocation_batches = 0
        while not normalized_max_batches or invocation_batches < normalized_max_batches:
            transaction = connection.transaction() if apply else _NullContext()
            with transaction:
                with connection.cursor() as cursor:
                    if apply:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                        cursor.execute("SET LOCAL statement_timeout = '60s'")
                    rows = _select_batch(
                        cursor,
                        cutoff_at=cutoff_at,
                        boundary=boundary,
                        batch_size=normalized_batch_size,
                        apply=apply,
                    )
                    if not rows:
                        receipt["scan_complete"] = True
                        break
                    projected: list[tuple[tuple[object, ...], tuple[dict[str, object], ...]]] = []
                    batch_bytes = 0
                    for row in rows:
                        links = tuple(project_property_research_packet_links(_run_payload(row)))
                        row_bytes = sum(int(link["packet_size_bytes"]) for link in links)
                        if projected and batch_bytes + row_bytes > normalized_max_batch_bytes:
                            break
                        if row_bytes > normalized_max_batch_bytes:
                            raise ValueError("single_run_exceeds_backfill_batch_bytes")
                        projected.append((row, links))
                        batch_bytes += row_bytes
                    if not projected:
                        raise RuntimeError("backfill_batch_made_no_progress")

                    for row, links in projected:
                        payload = _run_payload(row)
                        principal_id = str(payload["principal_id"])
                        run_id = str(payload["run_id"])
                        if apply:
                            receipt["links_upserted"] = int(receipt["links_upserted"]) + upsert_property_research_packet_links(
                                cursor,
                                links,
                            )
                            sync_property_research_packet_run_memberships(
                                cursor,
                                principal_id=principal_id,
                                run_id=run_id,
                                links=links,
                            )
                            if not _verify_run_memberships(
                                cursor,
                                principal_id=principal_id,
                                run_id=run_id,
                                links=links,
                            ):
                                raise RuntimeError("packet_membership_verification_failed")
                        if not links:
                            receipt["zero_projection_run_rows"] = int(receipt["zero_projection_run_rows"]) + 1
                        receipt["expected_membership_rows"] = int(receipt["expected_membership_rows"]) + len(links)
                        for link in sorted(
                            links,
                            key=lambda item: str(item["candidate_ref"]),
                        ):
                            expected_ref_digest_sha256 = _stream_identity_digest(
                                expected_ref_digest_sha256,
                                *_membership_identity_values(
                                    principal_id=principal_id,
                                    run_id=run_id,
                                    candidate_ref=link["candidate_ref"],
                                    packet_sha256=link["packet_sha256"],
                                    packet_size_bytes=link["packet_size_bytes"],
                                ),
                            )
                        if str(payload.get("payload_retention_status") or "").strip().lower() == "compact_only":
                            receipt["compact_only_run_rows"] = int(receipt["compact_only_run_rows"]) + 1

            processed_rows = [item[0] for item in projected]
            receipt["run_rows_scanned"] = int(receipt["run_rows_scanned"]) + len(processed_rows)
            receipt["batches_completed"] = int(receipt["batches_completed"]) + 1
            invocation_batches += 1
            boundary = (
                str(processed_rows[-1][0] or ""),
                str(processed_rows[-1][1] or ""),
            )
            receipt["expected_ref_digest_sha256"] = expected_ref_digest_sha256
            receipt["resume_token"] = _public_resume_token(cutoff_at, boundary)
            if checkpoint_path is not None:
                _write_private_json(
                    checkpoint_path,
                    _checkpoint_payload(
                        cutoff_at=cutoff_at,
                        boundary=boundary,
                        receipt=receipt,
                        expected_ref_digest_sha256=expected_ref_digest_sha256,
                    ),
                )

        if receipt["scan_complete"]:
            with connection.cursor() as cursor:
                cursor.execute(BACKFILL_SOURCE_COUNT_SQL, (cutoff_at,))
                final_source_count = cursor.fetchone()
                receipt["source_run_rows_verified"] = max(
                    0,
                    int(final_source_count[0] if final_source_count else 0),
                )
                if apply:
                    cursor.execute(BACKFILL_VERIFICATION_COUNT_SQL, (cutoff_at,))
                    verification_row = cursor.fetchone() or (0, 0)
                    receipt["verified_membership_rows"] = max(0, int(verification_row[0] or 0))
                    receipt["verified_distinct_tenant_refs"] = max(0, int(verification_row[1] or 0))
            if apply:
                verified_digest, streamed_membership_rows = (
                    _stream_verification_identity_digest(
                        connection,
                        cutoff_at=cutoff_at,
                    )
                )
                receipt["verified_ref_digest_sha256"] = verified_digest
                receipt["ref_digest_set_verified"] = bool(
                    streamed_membership_rows == receipt["verified_membership_rows"]
                    and verified_digest == expected_ref_digest_sha256
                )
                with connection.cursor() as cursor:
                    cursor.execute(BACKFILL_LINK_COVERAGE_SQL, (cutoff_at,))
                    link_coverage_row = cursor.fetchone() or (1, 1)
                    receipt["memberships_without_links"] = max(
                        0, int(link_coverage_row[0] or 0)
                    )
                    receipt["non_hold_links_without_memberships"] = max(
                        0, int(link_coverage_row[1] or 0)
                    )
                    receipt["link_coverage_verified"] = bool(
                        not receipt["memberships_without_links"]
                        and not receipt["non_hold_links_without_memberships"]
                    )
                if receipt["ref_digest_set_verified"]:
                    receipt["expected_distinct_tenant_refs"] = receipt[
                        "verified_distinct_tenant_refs"
                    ]
        receipt["expected_ref_digest_sha256"] = expected_ref_digest_sha256
        receipt["idempotent_verified"] = bool(
            apply
            and receipt["scan_complete"]
            and receipt["verified_membership_rows"] == receipt["expected_membership_rows"]
            and receipt["verified_distinct_tenant_refs"] == receipt["expected_distinct_tenant_refs"]
            and receipt["ref_digest_set_verified"]
            and receipt["link_coverage_verified"]
        )
        receipt["coverage_complete"] = bool(
            apply
            and receipt["scan_complete"]
            and receipt["run_rows_scanned"] == receipt["source_run_rows_at_cutoff"]
            and receipt["source_run_rows_verified"] == receipt["source_run_rows_at_cutoff"]
            and not receipt["compact_only_run_rows"]
            and not receipt["projection_failures"]
            and not receipt["verification_failures"]
            and receipt["idempotent_verified"]
        )
        receipt["status"] = (
            "complete"
            if receipt["coverage_complete"]
            else "partial" if not receipt["scan_complete"] else "failed"
        )
        if apply:
            with connection.cursor() as cursor:
                _mark_index_state(
                    cursor,
                    status="complete" if receipt["coverage_complete"] else "failed",
                    receipt=receipt,
                    cutoff_at=cutoff_at,
                    fleet_proof_sha256=fleet_proof_sha256,
                )
    except Exception as exc:
        receipt["failures_total"] = int(receipt["failures_total"]) + 1
        if "packet_" in str(exc) or "candidate_ref_" in str(exc):
            receipt["projection_failures"] = int(receipt["projection_failures"]) + 1
        if "verification" in str(exc):
            receipt["verification_failures"] = int(receipt["verification_failures"]) + 1
        receipt["status"] = "failed"
        receipt["coverage_complete"] = False
        receipt["error_code"] = str(exc or type(exc).__name__).splitlines()[0].partition(":")[0][:80]
        if apply and lock_acquired:
            try:
                with connection.cursor() as cursor:
                    _mark_index_state(
                        cursor,
                        status="failed",
                        receipt=receipt,
                        cutoff_at=cutoff_at,
                        fleet_proof_sha256=fleet_proof_sha256,
                    )
            except Exception:
                receipt["failures_total"] = int(receipt["failures_total"]) + 1
    finally:
        if lock_acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (BACKFILL_ADVISORY_LOCK_ID,))
            except Exception:
                receipt["failures_total"] = int(receipt["failures_total"]) + 1
                receipt["status"] = "failed"
                receipt["coverage_complete"] = False
                receipt["error_code"] = receipt["error_code"] or "packet_backfill_unlock_failed"
    receipt["resume_token"] = _public_resume_token(cutoff_at, boundary)
    receipt["completed_at"] = _utc_now()
    if checkpoint_path is not None:
        _write_private_json(
            checkpoint_path,
            _checkpoint_payload(
                cutoff_at=cutoff_at,
                boundary=boundary,
                receipt=receipt,
                expected_ref_digest_sha256=expected_ref_digest_sha256,
            ),
        )
    return receipt


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    _write_private_json(path, receipt)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or backfill tenant-scoped PropertyQuarry research packet links."
    )
    parser.add_argument("--apply", action="store_true", help="Commit index writes; default is audit.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batch-bytes", type=int, default=DEFAULT_MAX_BATCH_BYTES)
    parser.add_argument("--max-batches", type=int, default=0, help="Zero scans to exhaustion.")
    parser.add_argument("--receipt-path", type=Path)
    parser.add_argument("--checkpoint-path", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fleet-proof-path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    if args.resume and args.checkpoint_path is None:
        raise SystemExit("--resume requires --checkpoint-path")
    if args.apply and args.fleet_proof_path is None:
        raise SystemExit("--apply requires --fleet-proof-path")
    checkpoint = (
        _load_private_json(args.checkpoint_path)
        if args.resume and args.checkpoint_path is not None
        else None
    )
    fleet_proof = (
        _load_private_json(args.fleet_proof_path)
        if args.fleet_proof_path is not None
        else None
    )
    import psycopg

    connection = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        receipt = run_backfill(
            connection,
            apply=bool(args.apply),
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            max_batch_bytes=args.max_batch_bytes,
            checkpoint=checkpoint,
            checkpoint_path=args.checkpoint_path,
            fleet_proof=fleet_proof,
        )
    finally:
        connection.close()
    if args.receipt_path:
        _write_receipt(args.receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt.get("coverage_complete") is True and receipt.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
