from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.domain.models import Artifact, normalize_artifact, now_utc_iso


_DEFAULT_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_MAX_BYTES = 1024 * 1024 * 1024
_MAX_ARTIFACT_MIN_FREE_BYTES = 1024 * 1024 * 1024 * 1024


def _bounded_artifact_storage_int(name: str, *, default: int, maximum: int) -> int:
    raw_value = str(os.environ.get(name) or "").strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(0, min(parsed, maximum))


def _validate_artifact_storage_admission(
    root: Path,
    content_bytes: bytes,
    *,
    existing_size_bytes: int = 0,
) -> None:
    """Reject unsafe growth without deleting or rewriting durable artifacts."""

    max_bytes = _bounded_artifact_storage_int(
        "EA_ARTIFACT_MAX_BYTES",
        default=_DEFAULT_ARTIFACT_MAX_BYTES,
        maximum=_MAX_ARTIFACT_MAX_BYTES,
    )
    if len(content_bytes) > max_bytes:
        raise ValueError(
            f"artifact_payload_too_large:{len(content_bytes)}>{max_bytes}"
        )
    minimum_free_bytes = _bounded_artifact_storage_int(
        "EA_ARTIFACTS_MIN_FREE_BYTES",
        default=0,
        maximum=_MAX_ARTIFACT_MIN_FREE_BYTES,
    )
    growth_bytes = max(0, len(content_bytes) - max(0, int(existing_size_bytes)))
    if minimum_free_bytes <= 0 or growth_bytes <= 0:
        return
    filesystem = os.statvfs(root)
    free_bytes = int(filesystem.f_bavail) * int(filesystem.f_frsize)
    if free_bytes - growth_bytes < minimum_free_bytes:
        raise RuntimeError(
            "artifact_storage_low_water:"
            f"free={free_bytes}:growth={growth_bytes}:minimum={minimum_free_bytes}"
        )


def _file_uri(path: Path) -> str:
    return f"file://{path.resolve()}"


def _path_from_uri(uri: str) -> Path:
    text = str(uri or "").strip()
    if text.startswith("file://"):
        return Path(text[7:])
    return Path(text)


class PostgresArtifactRepository:
    def __init__(self, database_url: str, artifacts_dir: str, tenant_id: str = "default") -> None:
        self._database_url = str(database_url or "").strip()
        if not self._database_url:
            raise ValueError("database_url is required for PostgresArtifactRepository")
        self._artifacts_dir = Path(str(artifacts_dir or "").strip() or "/tmp/ea_artifacts")
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._tenant_id = str(tenant_id or "default").strip() or "default"
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError("psycopg is required for postgres artifact backend") from exc
        return psycopg.connect(self._database_url, autocommit=True)

    def _json_value(self, value: dict[str, Any]):  # type: ignore[no-untyped-def]
        from psycopg.types.json import Json

        return Json(value)

    def _ensure_schema(self) -> None:
        from app.repositories.postgres_schema import repository_schema_ddl_enabled

        if not repository_schema_ddl_enabled():
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL,
                        artifact_type TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        storage_uri TEXT NOT NULL,
                        metadata_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_artifacts_session_created
                    ON artifacts(session_id, created_at DESC)
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE artifacts
                    ADD COLUMN IF NOT EXISTS principal_id TEXT
                    """
                )
                cur.execute(
                    """
                    UPDATE artifacts
                    SET principal_id = COALESCE(NULLIF(principal_id, ''), COALESCE(metadata_json->>'principal_id', ''), '')
                    WHERE principal_id IS NULL OR principal_id = ''
                    """
                )
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF to_regclass('public.execution_sessions') IS NOT NULL THEN
                            UPDATE artifacts AS a
                            SET principal_id = COALESCE(NULLIF(a.principal_id, ''), COALESCE(es.intent_json->>'principal_id', ''))
                            FROM execution_sessions AS es
                            WHERE a.session_id = es.session_id::text
                              AND COALESCE(a.principal_id, '') = '';
                        END IF;
                    END
                    $$;
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_artifacts_principal_created
                    ON artifacts(principal_id, created_at DESC)
                    """
                )

    def _path_for(self, artifact_id: str) -> Path:
        return self._artifacts_dir / f"{artifact_id}.txt"

    def save(self, artifact: Artifact) -> None:
        normalized = normalize_artifact(artifact)
        path = self._path_for(normalized.artifact_id)
        content_bytes = normalized.content.encode("utf-8")
        try:
            existing_size_bytes = path.stat().st_size
        except FileNotFoundError:
            existing_size_bytes = 0
        _validate_artifact_storage_admission(
            self._artifacts_dir,
            content_bytes,
            existing_size_bytes=existing_size_bytes,
        )
        path.write_bytes(content_bytes)
        stamp = now_utc_iso()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO artifacts
                    (artifact_id, tenant_id, session_id, principal_id, artifact_type, mime_type, storage_uri, metadata_json, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (artifact_id) DO UPDATE
                    SET session_id = EXCLUDED.session_id,
                        principal_id = EXCLUDED.principal_id,
                        artifact_type = EXCLUDED.artifact_type,
                        mime_type = EXCLUDED.mime_type,
                        storage_uri = EXCLUDED.storage_uri,
                        metadata_json = EXCLUDED.metadata_json,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        normalized.artifact_id,
                        self._tenant_id,
                        normalized.execution_session_id,
                        normalized.principal_id,
                        normalized.kind,
                        normalized.mime_type,
                        _file_uri(path),
                        self._json_value(
                            {
                                "execution_session_id": normalized.execution_session_id,
                                "principal_id": normalized.principal_id,
                                "artifact_kind": normalized.kind,
                                "preview_text": normalized.preview_text,
                                "storage_handle": normalized.storage_handle,
                                "body_ref": _file_uri(path),
                                "structured_output_json": dict(normalized.structured_output_json or {}),
                                "attachments_json": dict(normalized.attachments_json or {}),
                            }
                        ),
                        stamp,
                        stamp,
                    ),
                )

    def get(self, artifact_id: str) -> Artifact | None:
        aid = str(artifact_id or "").strip()
        if not aid:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT artifact_id, session_id, principal_id, artifact_type, mime_type, storage_uri, metadata_json
                    FROM artifacts
                    WHERE artifact_id = %s AND tenant_id = %s
                    """,
                    (aid, self._tenant_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        found_id, session_id, principal_id, artifact_type, mime_type, storage_uri, metadata_json = row
        path = _path_from_uri(str(storage_uri or ""))
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        metadata = dict(metadata_json or {})
        return normalize_artifact(Artifact(
            artifact_id=str(found_id),
            kind=str(artifact_type),
            content=content,
            execution_session_id=str(session_id),
            principal_id=str(principal_id or ""),
            mime_type=str(mime_type or "text/plain") or "text/plain",
            preview_text=str(metadata.get("preview_text") or ""),
            storage_handle=str(metadata.get("storage_handle") or ""),
            body_ref=str(metadata.get("body_ref") or str(storage_uri or "")),
            structured_output_json=dict(metadata.get("structured_output_json") or {}),
            attachments_json=dict(metadata.get("attachments_json") or {}),
        ))

    def list_for_session(self, session_id: str) -> list[Artifact]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT artifact_id, session_id, principal_id, artifact_type, mime_type, storage_uri, metadata_json
                    FROM artifacts
                    WHERE session_id = %s AND tenant_id = %s
                    ORDER BY created_at ASC, artifact_id ASC
                    """,
                    (sid, self._tenant_id),
                )
                rows = cur.fetchall()
        out: list[Artifact] = []
        for found_id, found_session_id, principal_id, artifact_type, mime_type, storage_uri, metadata_json in rows:
            path = _path_from_uri(str(storage_uri or ""))
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            metadata = dict(metadata_json or {})
            out.append(
                normalize_artifact(Artifact(
                    artifact_id=str(found_id),
                    kind=str(artifact_type),
                    content=content,
                    execution_session_id=str(found_session_id),
                    principal_id=str(principal_id or ""),
                    mime_type=str(mime_type or "text/plain") or "text/plain",
                    preview_text=str(metadata.get("preview_text") or ""),
                    storage_handle=str(metadata.get("storage_handle") or ""),
                    body_ref=str(metadata.get("body_ref") or str(storage_uri or "")),
                    structured_output_json=dict(metadata.get("structured_output_json") or {}),
                    attachments_json=dict(metadata.get("attachments_json") or {}),
                ))
            )
        return out
