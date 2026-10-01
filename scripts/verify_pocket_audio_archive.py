#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ARCHIVED_STATUSES = {"archived", "already_archived"}
DISMISSED_STATUSES = {"dismissed"}
COMPLETION_EVENTS = ("pocket_recording_backfill_completed", "pocket_recording_sync_completed")


def _compact_failure(value: str, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: str(item.get("created_at") or "")):
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            continue
        by_source[source_id] = dict(row)
    return list(by_source.values())


def summarize_archive_files(archive_root: Path) -> dict[str, int | str | bool]:
    audio_suffixes = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".webm"}
    audio_total = 0
    metadata_total = 0
    if archive_root.exists():
        for path in archive_root.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in audio_suffixes:
                audio_total += 1
            elif suffix == ".json":
                metadata_total += 1
    return {
        "archive_root": archive_root.as_posix(),
        "archive_root_exists": archive_root.exists(),
        "audio_file_total": audio_total,
        "metadata_json_total": metadata_total,
    }


def build_receipt(
    *,
    archive_root: Path,
    index_rows: list[dict[str, Any]],
    completion_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = latest_rows(index_rows)
    archived_rows = [row for row in latest if str(row.get("archive_status") or "").strip() in ARCHIVED_STATUSES]
    dismissed_rows = [row for row in latest if str(row.get("archive_status") or "").strip() in DISMISSED_STATUSES]
    failed_rows = [row for row in latest if str(row.get("archive_status") or "").strip() == "failed"]
    missing_transcript_rows = [
        row
        for row in latest
        if str(row.get("archive_status") or "").strip() not in DISMISSED_STATUSES
        and _int_value(row.get("transcript_length")) <= 0
    ]
    archived_missing_path = [row for row in archived_rows if not str(row.get("archive_path") or "").strip()]
    archived_missing_audio = [
        row for row in archived_rows if str(row.get("archive_path") or "").strip() and not Path(str(row["archive_path"])).is_file()
    ]
    archived_missing_metadata = []
    for row in archived_rows:
        archive_path = str(row.get("archive_path") or "").strip()
        if archive_path and not Path(archive_path).with_suffix(".json").is_file():
            archived_missing_metadata.append(row)

    backfills = [row for row in completion_rows if str(row.get("event_type") or "") == "pocket_recording_backfill_completed"]
    latest_backfill = backfills[0] if backfills else {}
    latest_completion = completion_rows[0] if completion_rows else {}
    file_summary = summarize_archive_files(archive_root)

    failures: list[str] = []
    if not file_summary["archive_root_exists"]:
        failures.append(f"archive_root_missing:{archive_root}")
    if not latest_backfill:
        failures.append("missing_full_backfill_completion")
    else:
        if _bool_value(latest_backfill.get("scan_truncated")):
            failures.append("latest_backfill_scan_truncated")
        if _int_value(latest_backfill.get("failed_total")):
            failures.append(f"latest_backfill_failed_total:{latest_backfill.get('failed_total')}")
        if _int_value(latest_backfill.get("archive_failed_total")):
            failures.append(f"latest_backfill_archive_failed_total:{latest_backfill.get('archive_failed_total')}")
        if str(latest_backfill.get("teable_index_status") or "").strip() != "synced":
            failures.append(f"latest_backfill_teable_status:{latest_backfill.get('teable_index_status') or 'missing'}")
        if not _bool_value(latest_backfill.get("teable_index_sync_attempted")):
            failures.append("latest_backfill_teable_sync_not_attempted")
        expected_teable_rows = _int_value(latest_backfill.get("archived_total")) + _int_value(
            latest_backfill.get("archive_dismissed_total")
        )
        if _int_value(latest_backfill.get("teable_index_row_total")) != expected_teable_rows:
            failures.append(
                "latest_backfill_teable_row_total_mismatch:"
                f"{latest_backfill.get('teable_index_row_total')}!={expected_teable_rows}"
            )
    if latest_completion:
        if _bool_value(latest_completion.get("scan_truncated")):
            failures.append("latest_completion_scan_truncated")
        if _int_value(latest_completion.get("failed_total")):
            failures.append(f"latest_completion_failed_total:{latest_completion.get('failed_total')}")
        if _int_value(latest_completion.get("archive_failed_total")):
            failures.append(f"latest_completion_archive_failed_total:{latest_completion.get('archive_failed_total')}")
        if str(latest_completion.get("teable_index_status") or "").strip() == "blocked":
            failures.append(
                "latest_completion_teable_blocked:"
                + _compact_failure(str(latest_completion.get("teable_index_blocked_reason") or "unknown"))
            )
    if failed_rows:
        failures.append(f"latest_index_failed_archive_rows:{len(failed_rows)}")
    if archived_missing_path:
        failures.append(f"archived_rows_missing_archive_path:{len(archived_missing_path)}")
    if archived_missing_audio:
        failures.append(f"archived_rows_missing_audio_file:{len(archived_missing_audio)}")
    if archived_missing_metadata:
        failures.append(f"archived_rows_missing_metadata_file:{len(archived_missing_metadata)}")
    if missing_transcript_rows:
        failures.append(f"non_dismissed_rows_missing_transcript:{len(missing_transcript_rows)}")

    return {
        "contract_name": "ea.verify_pocket_audio_archive",
        "status": "pass" if not failures else "fail",
        "archive_files": file_summary,
        "database_index": {
            "latest_distinct_recording_total": len(latest),
            "latest_archived_total": len(archived_rows),
            "latest_dismissed_total": len(dismissed_rows),
            "latest_failed_total": len(failed_rows),
            "latest_non_dismissed_missing_transcript_total": len(missing_transcript_rows),
            "latest_archived_missing_path_total": len(archived_missing_path),
            "latest_archived_missing_audio_file_total": len(archived_missing_audio),
            "latest_archived_missing_metadata_file_total": len(archived_missing_metadata),
        },
        "latest_backfill": {
            key: latest_backfill.get(key)
            for key in (
                "created_at",
                "recording_total",
                "archived_total",
                "archive_dismissed_total",
                "archive_failed_total",
                "failed_total",
                "scan_truncated",
                "teable_index_status",
                "teable_index_row_total",
                "teable_index_sync_attempted",
            )
            if latest_backfill
        },
        "latest_completion": {
            key: latest_completion.get(key)
            for key in (
                "created_at",
                "event_type",
                "recording_total",
                "archived_total",
                "archive_dismissed_total",
                "archive_failed_total",
                "failed_total",
                "scan_truncated",
                "teable_index_status",
                "teable_index_row_total",
            )
            if latest_completion
        },
        "failures": failures,
    }


def _run_psql_json(*, container: str, user: str, database: str, sql: str) -> list[dict[str, Any]]:
    command = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-t",
        "-A",
        "-c",
        sql,
    ]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    text = result.stdout.strip()
    if not text:
        return []
    payload = json.loads(text)
    return list(payload or []) if isinstance(payload, list) else []


def load_index_rows(*, container: str, user: str, database: str) -> list[dict[str, Any]]:
    return _run_psql_json(
        container=container,
        user=user,
        database=database,
        sql="""
SELECT COALESCE(jsonb_agg(row_to_json(q) ORDER BY q.created_at), '[]'::jsonb)::text
FROM (
  SELECT
    source_id,
    created_at::text AS created_at,
    payload_json->>'recording_id' AS recording_id,
    payload_json->>'archive_status' AS archive_status,
    payload_json->>'archive_path' AS archive_path,
    length(coalesce(payload_json->>'transcript_text','')) AS transcript_length
  FROM observation_events
  WHERE channel='product' AND event_type='pocket_recording_archive_indexed'
) q;
""",
    )


def load_completion_rows(*, container: str, user: str, database: str) -> list[dict[str, Any]]:
    event_list = ",".join(f"'{event}'" for event in COMPLETION_EVENTS)
    return _run_psql_json(
        container=container,
        user=user,
        database=database,
        sql=f"""
SELECT COALESCE(jsonb_agg(row_to_json(q) ORDER BY q.created_at DESC), '[]'::jsonb)::text
FROM (
  SELECT
    event_type,
    created_at::text AS created_at,
    payload_json->>'recording_total' AS recording_total,
    payload_json->>'archived_total' AS archived_total,
    payload_json->>'archive_dismissed_total' AS archive_dismissed_total,
    payload_json->>'archive_failed_total' AS archive_failed_total,
    payload_json->>'failed_total' AS failed_total,
    payload_json->>'scan_truncated' AS scan_truncated,
    payload_json->>'teable_index_status' AS teable_index_status,
    payload_json->>'teable_index_row_total' AS teable_index_row_total,
    payload_json->>'teable_index_sync_attempted' AS teable_index_sync_attempted,
    payload_json->>'teable_index_blocked_reason' AS teable_index_blocked_reason
  FROM observation_events
  WHERE channel='product' AND event_type IN ({event_list})
  ORDER BY created_at DESC
) q;
""",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Pocket AI audio archive, transcript index, and Teable sync gate.")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=Path(os.environ.get("EA_POCKET_AUDIO_ARCHIVE_ROOT") or "/mnt/pcloud/EA/pocket-ai-audio"),
    )
    parser.add_argument("--postgres-container", default=os.environ.get("EA_POSTGRES_CONTAINER") or "ea-db")
    parser.add_argument("--postgres-user", default=os.environ.get("POSTGRES_USER") or "postgres")
    parser.add_argument("--postgres-db", default=os.environ.get("POSTGRES_DB") or "ea_smoke_runtime")
    args = parser.parse_args()

    receipt = build_receipt(
        archive_root=args.archive_root,
        index_rows=load_index_rows(container=args.postgres_container, user=args.postgres_user, database=args.postgres_db),
        completion_rows=load_completion_rows(
            container=args.postgres_container,
            user=args.postgres_user,
            database=args.postgres_db,
        ),
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
