from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.domain.models import OnboardingState, now_utc_iso


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _normalize_status(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"draft", "started", "in_progress", "ready_for_brief", "completed"}:
        return raw
    return "draft"


class PostgresOnboardingStateRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = str(database_url or "").strip()
        if not self._database_url:
            raise ValueError("database_url is required for PostgresOnboardingStateRepository")
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for postgres onboarding backend") from exc
        return psycopg.connect(self._database_url, autocommit=True)

    def _json_value(self, value: dict[str, object] | list[str]):  # type: ignore[no-untyped-def]
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
                CREATE TABLE IF NOT EXISTS onboarding_states (
                        onboarding_id TEXT PRIMARY KEY,
                        principal_id TEXT NOT NULL UNIQUE,
                        workspace_name TEXT NOT NULL DEFAULT '',
                        workspace_mode TEXT NOT NULL DEFAULT 'personal',
                        region TEXT NOT NULL DEFAULT '',
                        language TEXT NOT NULL DEFAULT '',
                        timezone TEXT NOT NULL DEFAULT '',
                        selected_channels_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        property_search_preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        privacy_preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        channel_preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        brief_preview_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        status TEXT NOT NULL DEFAULT 'draft',
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE onboarding_states ADD COLUMN IF NOT EXISTS property_search_preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb")
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_onboarding_states_updated
                    ON onboarding_states(updated_at DESC)
                    """
                )

    def _from_row(self, row: tuple[Any, ...]) -> OnboardingState:
        (
            onboarding_id,
            principal_id,
            workspace_name,
            workspace_mode,
            region,
            language,
            timezone,
            selected_channels_json,
            property_search_preferences_json,
            privacy_preferences_json,
            channel_preferences_json,
            brief_preview_json,
            status,
            created_at,
            updated_at,
        ) = row
        return OnboardingState(
            onboarding_id=str(onboarding_id),
            principal_id=str(principal_id),
            workspace_name=str(workspace_name or ""),
            workspace_mode=str(workspace_mode or "personal"),
            region=str(region or ""),
            language=str(language or ""),
            timezone=str(timezone or ""),
            selected_channels=tuple(str(v).strip().lower() for v in (selected_channels_json or []) if str(v).strip()),
            property_search_preferences_json=dict(property_search_preferences_json or {}),
            privacy_preferences_json=dict(privacy_preferences_json or {}),
            channel_preferences_json=dict(channel_preferences_json or {}),
            brief_preview_json=dict(brief_preview_json or {}),
            status=str(status or "draft"),
            created_at=_to_iso(created_at),
            updated_at=_to_iso(updated_at),
        )

    def upsert_state(
        self,
        *,
        principal_id: str,
        onboarding_id: str | None = None,
        workspace_name: str = "",
        workspace_mode: str = "personal",
        region: str = "",
        language: str = "",
        timezone: str = "",
        selected_channels: tuple[str, ...] = (),
        property_search_preferences_json: dict[str, object] | None = None,
        privacy_preferences_json: dict[str, object] | None = None,
        channel_preferences_json: dict[str, object] | None = None,
        brief_preview_json: dict[str, object] | None = None,
        status: str = "draft",
    ) -> OnboardingState:
        principal = str(principal_id or "").strip()
        if not principal:
            raise ValueError("principal_id_required")
        existing = self.get_for_principal(principal)
        row = OnboardingState(
            onboarding_id=str(onboarding_id or (existing.onboarding_id if existing else "")).strip() or str(uuid.uuid4()),
            principal_id=principal,
            workspace_name=str(workspace_name if workspace_name != "" else (existing.workspace_name if existing else "")).strip(),
            workspace_mode=str(
                workspace_mode if workspace_mode != "" else (existing.workspace_mode if existing else "personal")
            ).strip()
            or "personal",
            region=str(region if region != "" else (existing.region if existing else "")).strip(),
            language=str(language if language != "" else (existing.language if existing else "")).strip(),
            timezone=str(timezone if timezone != "" else (existing.timezone if existing else "")).strip(),
            selected_channels=tuple(str(v).strip().lower() for v in selected_channels if str(v).strip())
            if selected_channels
            else (existing.selected_channels if existing else ()),
            property_search_preferences_json=dict(
                property_search_preferences_json
                if property_search_preferences_json is not None
                else (existing.property_search_preferences_json if existing else {})
            ),
            privacy_preferences_json=dict(
                privacy_preferences_json
                if privacy_preferences_json is not None
                else (existing.privacy_preferences_json if existing else {})
            ),
            channel_preferences_json=dict(
                channel_preferences_json
                if channel_preferences_json is not None
                else (existing.channel_preferences_json if existing else {})
            ),
            brief_preview_json=dict(
                brief_preview_json if brief_preview_json is not None else (existing.brief_preview_json if existing else {})
            ),
            status=_normalize_status(status if status != "" else (existing.status if existing else "draft")),
            created_at=existing.created_at if existing else now_utc_iso(),
            updated_at=now_utc_iso(),
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO onboarding_states
                (onboarding_id, principal_id, workspace_name, workspace_mode, region, language, timezone,
                     selected_channels_json, property_search_preferences_json, privacy_preferences_json, channel_preferences_json, brief_preview_json,
                     status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (principal_id) DO UPDATE
                SET workspace_name = EXCLUDED.workspace_name,
                    workspace_mode = EXCLUDED.workspace_mode,
                    region = EXCLUDED.region,
                    language = EXCLUDED.language,
                    timezone = EXCLUDED.timezone,
                    selected_channels_json = EXCLUDED.selected_channels_json,
                    property_search_preferences_json = EXCLUDED.property_search_preferences_json,
                    privacy_preferences_json = EXCLUDED.privacy_preferences_json,
                    channel_preferences_json = EXCLUDED.channel_preferences_json,
                    brief_preview_json = EXCLUDED.brief_preview_json,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                RETURNING onboarding_id, principal_id, workspace_name, workspace_mode, region, language, timezone,
                              selected_channels_json, property_search_preferences_json, privacy_preferences_json, channel_preferences_json, brief_preview_json,
                              status, created_at, updated_at
                    """,
                    (
                        row.onboarding_id,
                        row.principal_id,
                        row.workspace_name,
                        row.workspace_mode,
                        row.region,
                        row.language,
                        row.timezone,
                        self._json_value(list(row.selected_channels)),
                        self._json_value(dict(row.property_search_preferences_json)),
                        self._json_value(dict(row.privacy_preferences_json)),
                        self._json_value(dict(row.channel_preferences_json)),
                        self._json_value(dict(row.brief_preview_json)),
                        row.status,
                        row.created_at,
                        row.updated_at,
                    ),
                )
                out = cur.fetchone()
        return self._from_row(out) if out else row

    def get_for_principal(self, principal_id: str) -> OnboardingState | None:
        principal = str(principal_id or "").strip()
        if not principal:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT onboarding_id, principal_id, workspace_name, workspace_mode, region, language, timezone,
                           selected_channels_json, property_search_preferences_json, privacy_preferences_json, channel_preferences_json, brief_preview_json,
                           status, created_at, updated_at
                    FROM onboarding_states
                    WHERE principal_id = %s
                    """,
                    (principal,),
                )
                row = cur.fetchone()
        return self._from_row(row) if row else None

    def list_states(self, *, limit: int = 1000) -> tuple[OnboardingState, ...]:
        normalized_limit = max(int(limit or 0), 1)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT onboarding_id, principal_id, workspace_name, workspace_mode, region, language, timezone,
                           selected_channels_json, property_search_preferences_json, privacy_preferences_json, channel_preferences_json, brief_preview_json,
                           status, created_at, updated_at
                    FROM onboarding_states
                    ORDER BY updated_at DESC, principal_id ASC
                    LIMIT %s
                    """,
                    (normalized_limit,),
                )
                rows = cur.fetchall() or []
        return tuple(self._from_row(row) for row in rows)

    def list_enabled_property_search_agent_principals(self, *, limit: int = 1000) -> tuple[str, ...]:
        """Return scheduler principals without hydrating onboarding documents."""

        normalized_limit = max(int(limit or 0), 1)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_id
                    FROM onboarding_states
                    WHERE CASE
                        WHEN jsonb_typeof(property_search_preferences_json -> 'search_agents') = 'array'
                        THEN EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                property_search_preferences_json -> 'search_agents'
                            ) AS saved_agent(value)
                            WHERE jsonb_typeof(saved_agent.value) = 'object'
                              AND lower(
                                  btrim(
                                      COALESCE(
                                          CASE
                                              WHEN saved_agent.value ? 'enabled'
                                                   AND jsonb_typeof(saved_agent.value -> 'enabled') <> 'null'
                                              THEN saved_agent.value ->> 'enabled'
                                              WHEN saved_agent.value ? 'search_agent_enabled'
                                              THEN saved_agent.value ->> 'search_agent_enabled'
                                              ELSE property_search_preferences_json ->> 'search_agent_enabled'
                                          END,
                                          ''
                                      )
                                  )
                              ) IN ('1', 'active', 'enabled', 'on', 'true', 'y', 'yes')
                        )
                        ELSE lower(
                            btrim(
                                COALESCE(
                                    CASE
                                        WHEN property_search_preferences_json ? 'enabled'
                                             AND jsonb_typeof(
                                                 property_search_preferences_json -> 'enabled'
                                             ) <> 'null'
                                        THEN property_search_preferences_json ->> 'enabled'
                                        ELSE property_search_preferences_json ->> 'search_agent_enabled'
                                    END,
                                    ''
                                )
                            )
                        ) IN ('1', 'active', 'enabled', 'on', 'true', 'y', 'yes')
                    END
                    ORDER BY updated_at DESC, principal_id ASC
                    LIMIT %s
                    """,
                    (normalized_limit,),
                )
                rows = cur.fetchall() or []
        return tuple(
            dict.fromkeys(
                principal_id
                for row in rows
                if row
                for principal_id in (str(row[0] or "").strip(),)
                if principal_id
            )
        )

    def erase_principal(self, principal_id: str) -> bool:
        principal = str(principal_id or "").strip()
        if not principal:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM onboarding_states WHERE principal_id = %s", (principal,))
                return bool(cur.rowcount)
