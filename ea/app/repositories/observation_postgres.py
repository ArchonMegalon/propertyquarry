from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any

from app.domain.models import ObservationEvent, now_utc_iso
from app.repositories.postgres_schema import (
    add_column_if_missing,
    configure_schema_timeouts,
    create_index_if_missing,
    drop_index_if_present,
)


_DEFAULT_OBSERVATION_PAYLOAD_MAX_BYTES = 512 * 1024


def _observation_payload_max_bytes() -> int:
    raw_value = str(os.getenv("EA_OBSERVATION_MAX_PAYLOAD_BYTES") or "").strip()
    if not raw_value:
        return _DEFAULT_OBSERVATION_PAYLOAD_MAX_BYTES
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_OBSERVATION_PAYLOAD_MAX_BYTES
    return max(16 * 1024, min(parsed, 8 * 1024 * 1024))


def _bounded_observation_payload(
    payload: dict[str, object],
    *,
    raw_payload_uri: str,
) -> dict[str, object]:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    limit_bytes = _observation_payload_max_bytes()
    if len(serialized) <= limit_bytes:
        return dict(payload)
    normalized_uri = str(raw_payload_uri or "").strip()
    if not normalized_uri:
        raise ValueError("observation_payload_too_large_raw_payload_uri_required")
    return {
        "payload_retention_status": "external_pointer_only",
        "payload_original_bytes": len(serialized),
        "payload_limit_bytes": limit_bytes,
        "payload_sha256": hashlib.sha256(serialized).hexdigest(),
        "raw_payload_uri": normalized_uri,
    }


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class PostgresObservationEventRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = str(database_url or "").strip()
        if not self._database_url:
            raise ValueError("database_url is required for PostgresObservationEventRepository")
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError("psycopg is required for postgres backend") from exc
        return psycopg.connect(self._database_url, autocommit=True)

    def _json_value(self, value: dict[str, Any]):  # type: ignore[no-untyped-def]
        from psycopg.types.json import Json

        return Json(value)

    def _ensure_schema(self) -> None:
        from app.repositories.postgres_schema import repository_schema_ddl_enabled

        if not repository_schema_ddl_enabled():
            return
        with self._connect() as conn:
            configure_schema_timeouts(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS observation_events (
                        observation_id TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        source_id TEXT NOT NULL DEFAULT '',
                        external_id TEXT NOT NULL DEFAULT '',
                        dedupe_key TEXT NOT NULL DEFAULT '',
                        auth_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        raw_payload_uri TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                add_column_if_missing(
                    cur,
                    "observation_events",
                    "source_id",
                    "ALTER TABLE observation_events ADD COLUMN IF NOT EXISTS source_id TEXT NOT NULL DEFAULT ''",
                )
                add_column_if_missing(
                    cur,
                    "observation_events",
                    "external_id",
                    "ALTER TABLE observation_events ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT ''",
                )
                add_column_if_missing(
                    cur,
                    "observation_events",
                    "dedupe_key",
                    "ALTER TABLE observation_events ADD COLUMN IF NOT EXISTS dedupe_key TEXT NOT NULL DEFAULT ''",
                )
                add_column_if_missing(
                    cur,
                    "observation_events",
                    "auth_context_json",
                    "ALTER TABLE observation_events ADD COLUMN IF NOT EXISTS auth_context_json JSONB NOT NULL DEFAULT '{}'::jsonb"
                )
                add_column_if_missing(
                    cur,
                    "observation_events",
                    "raw_payload_uri",
                    "ALTER TABLE observation_events ADD COLUMN IF NOT EXISTS raw_payload_uri TEXT NOT NULL DEFAULT ''",
                )
                create_index_if_missing(
                    cur,
                    "idx_observation_events_created",
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_events_created
                    ON observation_events(created_at DESC)
                    """,
                )
                drop_index_if_present(
                    cur,
                    "idx_observation_events_dedupe_key_unique",
                    "DROP INDEX IF EXISTS idx_observation_events_dedupe_key_unique",
                )
                create_index_if_missing(
                    cur,
                    "idx_observation_events_principal_dedupe_unique",
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_observation_events_principal_dedupe_unique
                    ON observation_events(principal_id, dedupe_key)
                    WHERE dedupe_key <> ''
                    """,
                )
                create_index_if_missing(
                    cur,
                    "idx_observation_events_source_external",
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_events_source_external
                    ON observation_events(source_id, external_id, created_at DESC)
                    """,
                )
                create_index_if_missing(
                    cur,
                    "idx_observation_events_principal_created",
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_events_principal_created
                    ON observation_events(principal_id, created_at DESC)
                    """,
                )

    def append(
        self,
        principal_id: str,
        channel: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        source_id: str = "",
        external_id: str = "",
        dedupe_key: str = "",
        auth_context_json: dict[str, object] | None = None,
        raw_payload_uri: str = "",
    ) -> ObservationEvent:
        principal = str(principal_id or "").strip()
        dedupe = str(dedupe_key or "").strip()
        if principal and dedupe:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT observation_id, principal_id, channel, event_type, payload_json, created_at,
                               source_id, external_id, dedupe_key, auth_context_json, raw_payload_uri
                        FROM observation_events
                        WHERE principal_id = %s AND dedupe_key = %s
                        LIMIT 1
                        """,
                        (principal, dedupe),
                    )
                    found = cur.fetchone()
            if found:
                (
                    observation_id,
                    found_principal,
                    found_channel,
                    found_event_type,
                    payload_json,
                    created_at,
                    found_source,
                    found_external,
                    found_dedupe,
                    found_auth_context,
                    found_raw_uri,
                ) = found
                return ObservationEvent(
                    observation_id=str(observation_id),
                    principal_id=str(found_principal),
                    channel=str(found_channel),
                    event_type=str(found_event_type),
                    payload=dict(payload_json or {}),
                    created_at=_to_iso(created_at),
                    source_id=str(found_source or ""),
                    external_id=str(found_external or ""),
                    dedupe_key=str(found_dedupe or ""),
                    auth_context_json=dict(found_auth_context or {}),
                    raw_payload_uri=str(found_raw_uri or ""),
                )
        normalized_raw_payload_uri = str(raw_payload_uri or "").strip()
        bounded_payload = _bounded_observation_payload(
            dict(payload or {}),
            raw_payload_uri=normalized_raw_payload_uri,
        )
        row = ObservationEvent(
            observation_id=str(uuid.uuid4()),
            principal_id=principal,
            channel=str(channel or "unknown").strip(),
            event_type=str(event_type or "unknown").strip(),
            payload=bounded_payload,
            created_at=now_utc_iso(),
            source_id=str(source_id or "").strip(),
            external_id=str(external_id or "").strip(),
            dedupe_key=dedupe,
            auth_context_json=dict(auth_context_json or {}),
            raw_payload_uri=normalized_raw_payload_uri,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO observation_events
                    (observation_id, principal_id, channel, event_type, payload_json, created_at,
                     source_id, external_id, dedupe_key, auth_context_json, raw_payload_uri)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row.observation_id,
                        row.principal_id,
                        row.channel,
                        row.event_type,
                        self._json_value(row.payload),
                        row.created_at,
                        row.source_id,
                        row.external_id,
                        row.dedupe_key,
                        self._json_value(row.auth_context_json),
                        row.raw_payload_uri,
                    ),
                )
        return row

    def list_recent(self, limit: int = 50, *, principal_id: str | None = None) -> list[ObservationEvent]:
        n = max(1, min(5000, int(limit or 50)))
        normalized_principal = str(principal_id or "").strip()
        query = """
            SELECT observation_id, principal_id, channel, event_type, payload_json, created_at,
                   source_id, external_id, dedupe_key, auth_context_json, raw_payload_uri
            FROM observation_events
        """
        params: list[Any] = []
        if normalized_principal:
            query += " WHERE principal_id = %s"
            params.append(normalized_principal)
        query += " ORDER BY created_at DESC, observation_id DESC LIMIT %s"
        params.append(n)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
        return [
            ObservationEvent(
                observation_id=str(observation_id),
                principal_id=str(principal_id),
                channel=str(channel),
                event_type=str(event_type),
                payload=dict(payload_json or {}),
                created_at=_to_iso(created_at),
                source_id=str(source_id or ""),
                external_id=str(external_id or ""),
                dedupe_key=str(dedupe_key or ""),
                auth_context_json=dict(auth_context_json or {}),
                raw_payload_uri=str(raw_payload_uri or ""),
            )
            for (
                observation_id,
                principal_id,
                channel,
                event_type,
                payload_json,
                created_at,
                source_id,
                external_id,
                dedupe_key,
                auth_context_json,
                raw_payload_uri,
            ) in rows
        ]

    def exists_recent(
        self,
        *,
        principal_id: str | None = None,
        channel: str,
        event_type: str,
        source_id: str = "",
        external_id: str = "",
        dedupe_key: str = "",
        limit: int = 1000,
    ) -> bool:
        n = max(1, min(5000, int(limit or 1000)))
        normalized_principal = str(principal_id or "").strip()
        query = """
            WITH recent AS (
                SELECT channel, event_type, source_id, external_id, dedupe_key
                FROM observation_events
        """
        params: list[Any] = []
        if normalized_principal:
            query += " WHERE principal_id = %s"
            params.append(normalized_principal)
        query += """
                ORDER BY created_at DESC, observation_id DESC
                LIMIT %s
            )
            SELECT EXISTS (
                SELECT 1
                FROM recent
                WHERE channel = %s AND event_type = %s
        """
        params.extend((n, str(channel or "").strip(), str(event_type or "").strip()))
        for column, value in (
            ("source_id", source_id),
            ("external_id", external_id),
            ("dedupe_key", dedupe_key),
        ):
            normalized_value = str(value or "").strip()
            if normalized_value:
                query += f" AND {column} = %s"
                params.append(normalized_value)
        query += ")"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
        return bool(row and row[0])

    def list_recent_matching(
        self,
        limit: int = 50,
        *,
        principal_id: str | None = None,
        channel: str = "",
        event_types: tuple[str, ...] = (),
    ) -> list[ObservationEvent]:
        n = max(1, min(5000, int(limit or 50)))
        normalized_principal = str(principal_id or "").strip()
        normalized_channel = str(channel or "").strip()
        normalized_types = tuple(
            dict.fromkeys(str(value or "").strip() for value in event_types if str(value or "").strip())
        )
        query = """
            SELECT observation_id, principal_id, channel, event_type, payload_json,
                   created_at, source_id, external_id, dedupe_key, auth_context_json,
                   raw_payload_uri
            FROM observation_events
            WHERE TRUE
        """
        params: list[Any] = []
        if normalized_principal:
            query += " AND principal_id = %s"
            params.append(normalized_principal)
        if normalized_channel:
            query += " AND channel = %s"
            params.append(normalized_channel)
        if normalized_types:
            query += " AND event_type = ANY(%s)"
            params.append(list(normalized_types))
        query += " ORDER BY created_at DESC, observation_id DESC LIMIT %s"
        params.append(n)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
        return [
            ObservationEvent(
                observation_id=str(observation_id),
                principal_id=str(row_principal_id),
                channel=str(row_channel),
                event_type=str(row_event_type),
                payload=dict(payload_json or {}),
                created_at=_to_iso(created_at),
                source_id=str(source_id or ""),
                external_id=str(external_id or ""),
                dedupe_key=str(dedupe_key or ""),
                auth_context_json=dict(auth_context_json or {}),
                raw_payload_uri=str(raw_payload_uri or ""),
            )
            for (
                observation_id,
                row_principal_id,
                row_channel,
                row_event_type,
                payload_json,
                created_at,
                source_id,
                external_id,
                dedupe_key,
                auth_context_json,
                raw_payload_uri,
            ) in rows
        ]

    def get_by_dedupe(self, dedupe_key: str, *, principal_id: str | None = None) -> ObservationEvent | None:
        key = str(dedupe_key or "").strip()
        if not key:
            return None
        normalized_principal = str(principal_id or "").strip()
        query = """
            SELECT observation_id, principal_id, channel, event_type, payload_json, created_at,
                   source_id, external_id, dedupe_key, auth_context_json, raw_payload_uri
            FROM observation_events
            WHERE dedupe_key = %s
        """
        params: list[Any] = [key]
        if normalized_principal:
            query += " AND principal_id = %s"
            params.append(normalized_principal)
        query += " LIMIT 1"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
        if not row:
            return None
        (
            observation_id,
            principal_id,
            channel,
            event_type,
            payload_json,
            created_at,
            source_id,
            external_id,
            found_dedupe_key,
            auth_context_json,
            raw_payload_uri,
        ) = row
        return ObservationEvent(
            observation_id=str(observation_id),
            principal_id=str(principal_id),
            channel=str(channel),
            event_type=str(event_type),
            payload=dict(payload_json or {}),
            created_at=_to_iso(created_at),
            source_id=str(source_id or ""),
            external_id=str(external_id or ""),
            dedupe_key=str(found_dedupe_key or ""),
            auth_context_json=dict(auth_context_json or {}),
            raw_payload_uri=str(raw_payload_uri or ""),
        )

    def count_recent_for_principal(self, principal_id: str, *, since: str) -> int:
        principal = str(principal_id or "").strip()
        cutoff = str(since or "").strip()
        if not principal or not cutoff:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM observation_events
                    WHERE principal_id = %s
                      AND created_at >= %s
                    """,
                    (principal, cutoff),
                )
                row = cur.fetchone()
        return int(row[0] or 0) if row else 0

    def list_for_principal(self, principal_id: str, *, limit: int = 5000) -> list[ObservationEvent]:
        principal = str(principal_id or "").strip()
        n = max(1, min(50_000, int(limit or 5000)))
        if not principal:
            return []
        return self.list_recent(limit=n, principal_id=principal)

    def erase_principal(self, principal_id: str) -> int:
        principal = str(principal_id or "").strip()
        if not principal:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM observation_events WHERE principal_id = %s", (principal,))
                return int(cur.rowcount or 0)
